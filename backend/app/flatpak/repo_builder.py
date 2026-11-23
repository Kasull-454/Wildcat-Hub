"""Publish the database state as signed OSTree repositories.

Layout under ``settings.flatpak_repo_path``::

    store/public/          archive repo holding every object of public apps
    store/u_<owner>/       private apps of one owner (core.parent → public)
    gen/<variant>-<n>/     one generation of a served repo: refs + summary
                           + appstream, core.parent → the matching store
    variants/<variant>     symlink → the live generation (atomic swap)

The variants mirror the F-Droid fork's audiences:

* ``public``       — PUBLIC + PUBLISHED apps, NSFW hidden
* ``public-nsfw``  — the same with NSFW apps
* ``u_<owner>``    — public apps + that owner's PRIVATE apps

A variant repo stores only its refs, ``summary`` (+ ``.sig``) and the
appstream branches; every app object is looked up in its store chain, both
by OSTree (``core.parent``) and by the serving layer, which answers
``objects/…`` requests from the variant first and then from the stores.
Private objects therefore only ever live in their owner's store and are
never reachable through the public variants.

Each bundle is imported once (``flatpak build-import-bundle``, commit
signed with the repo key) and kept alive by a ``wildcat/keep/<commit>``
ref so older versions stay installable with ``flatpak update --commit``.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import uuid as uuid_module
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.logging import get_logger
from app.flatpak import cli, gpg
from app.models.apk import Apk, ApkStatus
from app.models.app import App, AppStatus, AppVisibility
from app.models.repo_config import RepoConfig
from app.models.user import User
from app.storage import get_storage
from app.storage.local import LocalStorage

log = get_logger(__name__)

PUBLIC = "public"
PUBLIC_NSFW = "public-nsfw"
KEEP_PREFIX = "wildcat/keep/"


def root() -> Path:
    return Path(settings.flatpak_repo_path)


def public_store() -> Path:
    return root() / "store" / "public"


def user_store(owner_id: uuid_module.UUID | str) -> Path:
    return root() / "store" / f"u_{owner_id}"


def user_variant(owner_id: uuid_module.UUID | str) -> str:
    return f"u_{owner_id}"


def variant_link(name: str) -> Path:
    return root() / "variants" / name


def variant_dir(name: str) -> Path | None:
    """Resolved live generation of ``name`` or None when never built."""
    link = variant_link(name)
    try:
        target = link.resolve(strict=True)
    except (FileNotFoundError, RuntimeError):
        return None
    if not (target / "summary").exists():
        return None
    return target


def lookup_chain(name: str) -> list[Path]:
    """Directories an object request for ``name`` is answered from, in order."""
    live = variant_dir(name)
    if live is None:
        return []
    chain = [live]
    if name.startswith("u_"):
        chain.append(user_store(name[2:]))
    chain.append(public_store())
    return chain


# --------------------------------------------------------------------------
# DB loading (same audiences as the F-Droid index builder)
# --------------------------------------------------------------------------
def _published_app_query():
    return (
        select(App)
        .options(selectinload(App.apks), selectinload(App.categories))
        .where(App.status == AppStatus.PUBLISHED)
    )


def _published_apks(app: App) -> list[Apk]:
    return [a for a in app.apks if a.status == ApkStatus.PUBLISHED and a.ostree_commit and a.flatpak_ref]


async def _load_public_apps(db: AsyncSession) -> list[App]:
    res = await db.execute(_published_app_query().where(App.visibility == AppVisibility.PUBLIC))
    return [a for a in res.scalars().unique().all() if _published_apks(a)]


async def _load_private_by_owner(db: AsyncSession) -> dict[uuid_module.UUID, list[App]]:
    res = await db.execute(
        _published_app_query()
        .join(User, User.id == App.owner_id)
        .where(App.visibility == AppVisibility.PRIVATE, User.is_active.is_(True))
    )
    out: dict[uuid_module.UUID, list[App]] = {}
    for app in res.scalars().unique().all():
        if _published_apks(app) and app.owner_id is not None:
            out.setdefault(app.owner_id, []).append(app)
    return out


async def _load_nsfw_users(db: AsyncSession) -> set[uuid_module.UUID]:
    rows = await db.execute(select(User.id).where(User.show_nsfw.is_(True), User.is_active.is_(True)))
    return {r[0] for r in rows.all()}


def select_refs(app: App) -> dict[str, Apk]:
    """``ref -> apk`` served for this app: per (arch, branch) the pinned
    suggested version when it matches, else the newest non-beta build,
    else the newest beta."""
    by_ref: dict[str, list[Apk]] = {}
    for apk in _published_apks(app):
        by_ref.setdefault(apk.flatpak_ref, []).append(apk)
    chosen: dict[str, Apk] = {}
    for ref, apks in by_ref.items():
        apks.sort(key=lambda a: a.version_code, reverse=True)
        pick = next((a for a in apks if a.version_code == app.suggested_version_code), None)
        pick = pick or next((a for a in apks if not a.is_beta), None) or apks[0]
        chosen[ref] = pick
    return chosen


# --------------------------------------------------------------------------
# Store management
# --------------------------------------------------------------------------
def _has_commit(store: Path, commit: str) -> bool:
    return (store / "objects" / commit[:2] / f"{commit[2:]}.commit").exists()


async def _bundle_path(apk: Apk) -> tuple[Path, bool]:
    """Local path of the stored bundle (+ whether it is a temp copy)."""
    storage = get_storage()
    if isinstance(storage, LocalStorage):
        return storage.local_path(apk.storage_key), False
    fd, name = tempfile.mkstemp(suffix=".flatpak")
    os.close(fd)
    path = Path(name)
    with path.open("wb") as fh:
        async for chunk in await storage.open_stream(apk.storage_key):
            fh.write(chunk)
    return path, True


async def ensure_store(store: Path, parent: Path | None = None) -> None:
    await cli.init_archive_repo(store, parent=parent)


async def import_apk(store: Path, apk: Apk, key_fpr: str) -> None:
    if _has_commit(store, apk.ostree_commit):
        return
    path, temp = await _bundle_path(apk)
    try:
        await cli.run(
            "flatpak", "build-import-bundle", "--no-update-summary",
            f"--gpg-sign={key_fpr}", *gpg.sign_args(), store, path, timeout=1800,
        )
    finally:
        if temp:
            path.unlink(missing_ok=True)
    if not _has_commit(store, apk.ostree_commit):
        raise RuntimeError(f"bundle for {apk.file_name} did not yield commit {apk.ostree_commit}")
    # The import also moved app/<id>/... in the store; only keep-refs pin
    # objects there, so drop it again and pin the commit itself.
    await cli.ostree(store, "refs", "--delete", apk.flatpak_ref)
    _write_ref(store, KEEP_PREFIX + apk.ostree_commit, apk.ostree_commit)
    log.info("imported bundle", ref=apk.flatpak_ref, commit=apk.ostree_commit[:12], store=store.name)


def _write_ref(repo: Path, ref: str, commit: str) -> None:
    path = repo / "refs" / "heads" / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(commit + "\n")
    tmp.replace(path)


async def _prune_store(store: Path, wanted: set[str]) -> int:
    heads = store / "refs" / "heads" / KEEP_PREFIX.rstrip("/")
    removed = 0
    if heads.is_dir():
        for ref in heads.iterdir():
            if ref.name not in wanted:
                ref.unlink(missing_ok=True)
                removed += 1
    if removed:
        await cli.ostree(store, "prune", "--refs-only", timeout=3600)
    return removed


# --------------------------------------------------------------------------
# Variant generation
# --------------------------------------------------------------------------
async def _build_variant(
    name: str,
    *,
    refs: dict[str, str],
    parent: Path,
    repo_config: RepoConfig,
    key_fpr: str,
    generation: int,
) -> None:
    gen = root() / "gen" / f"{name}-{generation}"
    shutil.rmtree(gen, ignore_errors=True)
    await cli.init_archive_repo(gen, parent=parent)
    for ref, commit in refs.items():
        _write_ref(gen, ref, commit)
    args = [
        "flatpak", "build-update-repo",
        f"--title={repo_config.name}",
        f"--gpg-sign={key_fpr}", *gpg.sign_args(),
        f"--gpg-import={_pubkey_file()}",
    ]
    if repo_config.description:
        args.append(f"--comment={repo_config.description.splitlines()[0][:200]}")
    if repo_config.address:
        args.append(f"--homepage={repo_config.address}")
    if settings.flatpak_static_deltas:
        args.append("--generate-static-deltas")
    await cli.run(*args, gen, timeout=3600)
    link = variant_link(name)
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp_link = link.with_name(f".{name}.{generation}.lnk")
    tmp_link.unlink(missing_ok=True)
    os.symlink(os.path.relpath(gen, link.parent), tmp_link)
    tmp_link.replace(link)


_PUBKEY_CACHE: dict[str, Path] = {}


def _pubkey_file() -> Path:
    return _PUBKEY_CACHE["path"]


async def _write_pubkey() -> None:
    data = await gpg.export_public_key()
    path = root() / "repo-key.gpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    _PUBKEY_CACHE["path"] = path


def _gc_generations(live: set[str]) -> None:
    gen_root = root() / "gen"
    if not gen_root.is_dir():
        return
    for d in gen_root.iterdir():
        if d.name not in live:
            shutil.rmtree(d, ignore_errors=True)
    vroot = root() / "variants"
    if vroot.is_dir():
        for link in vroot.iterdir():
            if link.is_symlink() and not link.exists():
                link.unlink(missing_ok=True)


async def rebuild_repo_index(db: AsyncSession) -> None:
    """Regenerate every Flatpak repo variant from the database."""
    repo_config = (await db.execute(select(RepoConfig).limit(1))).scalar_one_or_none()
    if repo_config is None or not repo_config.setup_complete:
        log.warning("skipping reindex: setup wizard not completed yet")
        return
    if not gpg.key_present():
        log.warning("skipping reindex: signing key missing", home=settings.gpg_home)
        return

    key_fpr = await gpg.fingerprint()
    await _write_pubkey()
    await ensure_store(public_store())

    public_all = await _load_public_apps(db)
    public_sfw = [a for a in public_all if not a.is_nsfw]
    private_by_owner = await _load_private_by_owner(db)
    nsfw_users = await _load_nsfw_users(db)

    # 1. Import every served bundle into its store.
    wanted: dict[Path, set[str]] = {public_store(): set()}
    for app in public_all:
        for apk in _published_apks(app):
            await import_apk(public_store(), apk, key_fpr)
            wanted[public_store()].add(apk.ostree_commit)
    for owner_id, apps in private_by_owner.items():
        store = user_store(owner_id)
        await ensure_store(store, parent=public_store())
        wanted.setdefault(store, set())
        for app in apps:
            for apk in _published_apks(app):
                await import_apk(store, apk, key_fpr)
                wanted[store].add(apk.ostree_commit)

    def refs_for(apps: list[App]) -> dict[str, str]:
        out: dict[str, str] = {}
        for app in apps:
            for ref, apk in select_refs(app).items():
                out[ref] = apk.ostree_commit
        return out

    generation = (repo_config.last_index_version or 0) + 1
    live: set[str] = set()

    await _build_variant(PUBLIC, refs=refs_for(public_sfw), parent=public_store(),
                         repo_config=repo_config, key_fpr=key_fpr, generation=generation)
    live.add(f"{PUBLIC}-{generation}")
    await _build_variant(PUBLIC_NSFW, refs=refs_for(public_all), parent=public_store(),
                         repo_config=repo_config, key_fpr=key_fpr, generation=generation)
    live.add(f"{PUBLIC_NSFW}-{generation}")

    for owner_id, apps in private_by_owner.items():
        base = public_all if owner_id in nsfw_users else public_sfw
        own = apps if owner_id in nsfw_users else [a for a in apps if not a.is_nsfw]
        name = user_variant(owner_id)
        await _build_variant(name, refs=refs_for(base + own), parent=user_store(owner_id),
                             repo_config=repo_config, key_fpr=key_fpr, generation=generation)
        live.add(f"{name}-{generation}")

    # 2. Retire variants / stores of owners without private apps.
    current = {str(o) for o in private_by_owner}
    try:
        previous = set(json.loads(repo_config.private_index_owner_ids or "[]"))
    except json.JSONDecodeError:
        previous = set()
    for stale in previous - current:
        variant_link(user_variant(stale)).unlink(missing_ok=True)
    _gc_generations(live)

    # 3. Drop objects no served build references any more. Strict on
    #    purpose: an app that went private (or was unpublished) must not
    #    stay fetchable by checksum through the public variants.
    for store, commits in wanted.items():
        await _prune_store(store, commits)
    store_root = root() / "store"
    for d in store_root.iterdir() if store_root.is_dir() else ():
        if d.name.startswith("u_") and d.name[2:] not in current:
            await asyncio.to_thread(shutil.rmtree, d, True)

    repo_config.private_index_owner_ids = json.dumps(sorted(current))
    repo_config.last_index_version = generation
    repo_config.last_indexed_at = datetime.now(UTC)
    await db.flush()
    log.info(
        "flatpak repo rebuilt",
        public_apps=len(public_sfw),
        nsfw_hidden=len(public_all) - len(public_sfw),
        private_variants=len(private_by_owner),
        generation=generation,
    )

