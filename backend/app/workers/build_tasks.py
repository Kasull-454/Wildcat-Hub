"""Flatpak manifest builds.

Two halves, two containers:

* **worker** (DB access, no build privileges)
    - :func:`dispatch_manifest_build` turns a ``manifest_*`` source into a
      self-contained build spec and enqueues it on the builder queue;
    - :func:`ingest_built_bundle` takes the finished bundle and runs it
      through the normal import path (parse, signer/quota checks, publish).
* **builder** (``flatpak-builder`` + bubblewrap, no DB, no app secrets)
    - :func:`run_flatpak_build` clones / writes the manifest, builds it,
      exports a ``.flatpak`` bundle to the shared build volume and hands
      the result back to the worker queue.

The builder never sees the database or the signing key: a malicious
manifest can at worst produce a bad bundle, which the worker then
validates like any upload (app ID must match the app it belongs to).

Run the builder with::

    python -m arq app.workers.build_tasks.BuilderSettings
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from arq.connections import RedisSettings
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.logging import configure_logging, get_logger

log = get_logger(__name__)

_LOG_TAIL = 30_000
_SAFE_ID = re.compile(r"^[0-9a-f-]{36}$")


def _work_root() -> Path:
    return Path(settings.builder_work_dir)


# ==========================================================================
# Worker side
# ==========================================================================
def _clone_url(source, token: str | None) -> str:
    from app.services.github_releases import validate_base_url

    provider = source.provider.value
    if source.base_url:
        base = validate_base_url(source.base_url).rstrip("/")  # type: ignore[union-attr]
    else:
        base = {"github": "https://github.com", "gitlab": "https://gitlab.com", "gitea": "https://codeberg.org"}[provider]
    host = base.split("://", 1)[1]
    if token:
        user = {"github": "x-access-token", "gitlab": "oauth2", "gitea": "git"}[provider]
        return f"https://{user}:{token}@{host}/{source.repo}.git"
    return f"{base}/{source.repo}.git"


async def dispatch_manifest_build(db, source, app, *, force: bool = False) -> dict:
    from arq import create_pool

    from app.services.crypto import decrypt

    spec: dict = {
        "source_id": str(source.id),
        "app_id": app.package_name,
        "kind": source.kind,
        "last_built_commit": None if force else source.last_built_commit,
        "runtime_repo": settings.flatpak_runtime_repo,
    }
    if source.kind == "manifest_inline":
        spec["manifest"] = source.manifest_inline or ""
    else:
        spec["clone_url"] = _clone_url(source, decrypt(source.access_token_encrypted))
        spec["git_ref"] = source.git_ref
        spec["manifest_path"] = source.manifest_path
    # One build per source at a time. The marker is cleared by
    # ``ingest_built_bundle``; a builder that died mid-build releases it
    # after the build timeout.
    if source.build_state and source.last_scanned_at is not None:
        age = (datetime.now(UTC) - source.last_scanned_at).total_seconds()
        if age < settings.builder_timeout_s + 600:
            return {"status": "skipped", "reason": "a build for this source is already queued"}
    pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        job = await pool.enqueue_job(
            "run_flatpak_build",
            spec,
            _queue_name=settings.builder_queue,
            # Unique per request: arq keeps finished job ids for a day and
            # would silently drop a re-enqueue under a fixed id.
            _job_id=f"build:{source.id}:{int(datetime.now(UTC).timestamp() * 1000)}",
        )
    finally:
        await pool.close()
    if job is None:
        return {"status": "skipped", "reason": "a build for this source is already queued"}
    source.build_state = "queued"
    source.last_scanned_at = datetime.now(UTC)
    await db.commit()
    return {"status": "queued", "app": app.package_name, "kind": source.kind}


async def import_bundle_for_source(db, *, source, app, owner, tmp_path: Path, label: str, is_beta: bool = False) -> dict:
    """Parse + attach a bundle produced for ``source`` (shared with releases)."""
    from app.api.v1.apks import attach_apk_to_app, parse_or_400
    from app.models.github_source import GithubSourceStatus
    from app.services.apk_eviction import evict_oldest_if_needed
    from app.services.audit import write_event

    try:
        meta = await parse_or_400(tmp_path)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"bundle parsing failed: {getattr(exc, 'detail', str(exc))}"}
    if any(a.ostree_commit == meta.ostree_commit or a.version_code == meta.version_code for a in app.apks):
        source.last_status = GithubSourceStatus.SKIPPED
        source.last_error = f"{label}: commit {meta.ostree_commit[:12]} already imported"
        return {"status": "skipped", "reason": "commit_present"}
    try:
        apk = await attach_apk_to_app(db, app=app, tmp_path=tmp_path, meta=meta, uploader=owner, is_beta=is_beta)
    except Exception as exc:  # noqa: BLE001
        detail = getattr(exc, "detail", None) or str(exc)
        await write_event(
            db, action="github_source.import_failed", actor=None, target_type="app", target_id=app.id,
            summary=f"Import of {label} rejected", payload={"label": label, "error": detail},
        )
        return {"status": "error", "error": f"import rejected: {detail}"}
    source.last_status = GithubSourceStatus.IMPORTED
    source.last_error = None
    await evict_oldest_if_needed(db, app=app, actor_id=owner.id, keep=apk.id)
    await write_event(
        db, action="github_source.imported", actor=None, target_type="app", target_id=app.id,
        summary=f"Imported {label} as {meta.version_name} ({meta.ostree_commit[:12]})",
        payload={"label": label, "ref": meta.flatpak_ref, "commit": meta.ostree_commit,
                 "version_name": meta.version_name},
    )
    return {"status": "imported", "published": apk.status.value == "published",
            "version": meta.version_name, "commit": meta.ostree_commit}


async def ingest_built_bundle(ctx: dict, result: dict) -> dict:
    """Worker job: record a builder result, import its bundle if any."""
    from app.core.database import SessionLocal
    from app.models.app import App
    from app.models.github_source import GithubSource, GithubSourceStatus
    from app.models.user import User
    from app.services.queue import enqueue_reindex

    sid = str(result.get("source_id", ""))
    if not _SAFE_ID.match(sid):
        return {"status": "error", "error": "invalid source id"}
    bundle_name = result.get("bundle")
    bundle = (_work_root() / "out" / bundle_name) if bundle_name else None
    if bundle is not None and (not re.fullmatch(r"[0-9a-f-]{36}-\d+\.flatpak", bundle_name) or not bundle.is_file()):
        bundle = None
    outcome: dict = {"status": result.get("status", "error")}
    try:
        async with SessionLocal() as db:
            source = (await db.execute(
                select(GithubSource)
                .options(selectinload(GithubSource.app).selectinload(App.apks))
                .where(GithubSource.id == uuid.UUID(sid))
            )).scalar_one_or_none()
            if source is None or source.app is None:
                return {"status": "error", "error": "source vanished"}
            app = source.app
            source.build_state = None
            source.last_build_at = datetime.now(UTC)
            source.last_scanned_at = datetime.now(UTC)
            source.last_build_log = (result.get("log") or "")[-_LOG_TAIL:] or source.last_build_log
            status = result.get("status")
            if status == "up_to_date":
                source.last_status = GithubSourceStatus.UP_TO_DATE
                source.last_error = None
                await db.commit()
                return {"status": "up_to_date", "app": app.package_name}
            if status != "built" or bundle is None:
                source.last_status = GithubSourceStatus.ERROR
                source.last_error = (result.get("error") or "build failed")[:2000]
                await db.commit()
                return {"status": "error", "app": app.package_name, "error": source.last_error}
            owner = (await db.execute(select(User).where(User.id == app.owner_id))).scalar_one_or_none() if app.owner_id else None
            if owner is None:
                source.last_status = GithubSourceStatus.ERROR
                source.last_error = "App has no owner — cannot attribute builds"
                await db.commit()
                return {"status": "error", "error": "app owner missing"}
            fd, tmp_name = tempfile.mkstemp(suffix=".flatpak")
            os.close(fd)
            tmp_path = Path(tmp_name)
            try:
                await asyncio.to_thread(shutil.copyfile, bundle, tmp_path)
                label = f"build of {result.get('revision', '')[:12] or 'manifest'}"
                outcome = await import_bundle_for_source(
                    db, source=source, app=app, owner=owner, tmp_path=tmp_path, label=label
                )
                if outcome["status"] == "error":
                    source.last_status = GithubSourceStatus.ERROR
                    source.last_error = outcome["error"][:2000]
                else:
                    source.last_built_commit = result.get("revision")
                    source.last_release_tag = (result.get("revision") or "")[:255] or None
                await db.commit()
            finally:
                tmp_path.unlink(missing_ok=True)
        if outcome.get("published"):
            await enqueue_reindex()
        return {**outcome, "app": app.package_name}
    finally:
        if bundle is not None:
            bundle.unlink(missing_ok=True)


# ==========================================================================
# Builder side
# ==========================================================================
async def _proc(argv: list[str], *, cwd: Path, log_fh, timeout: float, env: dict) -> int:
    log_fh.write(f"\n$ {' '.join(_redact(a) for a in argv)}\n")
    log_fh.flush()
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd), stdout=log_fh, stderr=asyncio.subprocess.STDOUT, env=env,
        stdin=asyncio.subprocess.DEVNULL,
    )
    try:
        return await asyncio.wait_for(proc.wait(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        log_fh.write(f"\n!! timed out after {timeout}s\n")
        return -9


def _redact(arg: str) -> str:
    return re.sub(r"//[^/@\s]+@", "//***@", arg)


def _builder_env() -> dict:
    home = _work_root() / "home"
    home.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(home),
        "LANG": "C.UTF-8",
        # Persistent user installation: SDKs / runtimes are downloaded once.
        "FLATPAK_USER_DIR": str(_work_root() / "flatpak"),
        "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", "/tmp"),
        "GIT_TERMINAL_PROMPT": "0",
    }


def _manifest_app_id(path: Path) -> str | None:
    text = path.read_text("utf-8", "replace")
    try:
        if path.suffix == ".json":
            data = json.loads(re.sub(r"^\s*//.*$", "", text, flags=re.M))
        else:
            import yaml

            data = yaml.safe_load(text)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    return data.get("app-id") or data.get("id")


def _manifest_branch(path: Path) -> str:
    text = path.read_text("utf-8", "replace")
    m = re.search(r'^["\']?(?:branch|default-branch)["\']?\s*:\s*["\']?([A-Za-z0-9._-]+)', text, re.M)
    return m.group(1) if m else "master"


async def run_flatpak_build(ctx: dict, spec: dict) -> dict:
    from arq import create_pool

    sid = str(spec.get("source_id", ""))
    if not _SAFE_ID.match(sid):
        return {"status": "error", "error": "invalid source id"}
    work = _work_root() / "sources" / sid
    work.mkdir(parents=True, exist_ok=True)
    out_dir = _work_root() / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    env = _builder_env()
    log_path = work / "build.log"
    result: dict = {"source_id": sid, "status": "error"}

    with log_path.open("w", encoding="utf-8", errors="replace") as log_fh:
        try:
            result.update(await _build(spec, work, out_dir, env, log_fh, sid))
        except Exception as exc:  # noqa: BLE001
            log.exception("builder crashed", source=sid)
            result["error"] = f"builder error: {exc}"[:2000]
            log_fh.write(f"\n!! {result['error']}\n")
    result["log"] = _redact(log_path.read_text("utf-8", "replace")[-_LOG_TAIL:])

    pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        await pool.enqueue_job("ingest_built_bundle", result, _job_id=f"ingest:{sid}:{datetime.now(UTC):%Y%m%d%H%M%S}")
    finally:
        await pool.close()
    return {k: v for k, v in result.items() if k != "log"}


async def _build(spec: dict, work: Path, out_dir: Path, env: dict, log_fh, sid: str) -> dict:
    app_id = spec["app_id"]
    src = work / "src"

    # ---- 1. Get the manifest -------------------------------------------
    if spec["kind"] == "manifest_inline":
        text = spec.get("manifest") or ""
        revision = hashlib.sha256(text.encode()).hexdigest()
        if revision == spec.get("last_built_commit"):
            return {"status": "up_to_date", "revision": revision}
        shutil.rmtree(src, ignore_errors=True)
        src.mkdir(parents=True)
        suffix = ".json" if text.lstrip().startswith("{") else ".yml"
        manifest = src / f"{app_id}{suffix}"
        manifest.write_text(text, "utf-8")
    else:
        url, ref = spec["clone_url"], spec.get("git_ref")
        from app.flatpak import cli

        log_fh.write(f"\n$ git ls-remote {_redact(url)} {ref or 'HEAD'}\n")
        try:
            listing = (await cli.run("git", "ls-remote", "--", url, ref or "HEAD", timeout=120)).stdout
        except cli.CommandError as exc:
            log_fh.write(_redact(exc.stderr))
            return {"error": "could not reach the git repository (git ls-remote failed)"}
        log_fh.write(listing)
        heads = [ln.split()[0] for ln in listing.splitlines() if re.match(r"^[0-9a-f]{40}\s", ln)]
        if not heads:
            return {"error": f"ref {ref or 'HEAD'!r} not found in the repository"}
        revision = heads[-1]
        if revision and revision == spec.get("last_built_commit"):
            return {"status": "up_to_date", "revision": revision}
        shutil.rmtree(src, ignore_errors=True)
        clone = ["git", "clone", "--depth", "1", "--recurse-submodules", "--shallow-submodules"]
        if ref:
            clone += ["--branch", ref]
        rc = await _proc([*clone, "--", url, str(src)], cwd=work, log_fh=log_fh, timeout=900, env=env)
        if rc != 0:
            return {"error": "git clone failed", "revision": revision}
        manifest = (src / spec["manifest_path"]).resolve()
        if not manifest.is_relative_to(src.resolve()) or not manifest.is_file():
            return {"error": f"manifest {spec['manifest_path']} not found in the repository", "revision": revision}

    found = _manifest_app_id(manifest)
    if found != app_id:
        return {"error": f"manifest app-id {found!r} does not match the app {app_id!r}", "revision": revision}

    # ---- 2. Build --------------------------------------------------------
    repo = work / "repo"
    await _proc(["flatpak", "remote-add", "--user", "--if-not-exists", "flathub", spec["runtime_repo"]],
                cwd=work, log_fh=log_fh, timeout=120, env=env)
    rc = await _proc(
        [
            "flatpak-builder", "--user", "--install-deps-from=flathub", "--force-clean",
            "--disable-rofiles-fuse", "--ccache",
            f"--state-dir={work / 'state'}", f"--repo={repo}", str(work / "build"), str(manifest),
        ],
        cwd=manifest.parent, log_fh=log_fh, timeout=settings.builder_timeout_s, env=env,
    )
    shutil.rmtree(work / "build", ignore_errors=True)
    if rc != 0:
        return {"error": f"flatpak-builder exited {rc} (see build log)", "revision": revision}

    # ---- 3. Bundle ---------------------------------------------------------
    refs_dir = repo / "refs" / "heads" / "app" / app_id
    branches = sorted(p.name for arch in refs_dir.iterdir() for p in arch.iterdir()) if refs_dir.is_dir() else []
    branch = _manifest_branch(manifest)
    if branch not in branches and branches:
        branch = branches[0]
    bundle_name = f"{sid}-{int(datetime.now(UTC).timestamp())}.flatpak"
    rc = await _proc(
        ["flatpak", "build-bundle", f"--runtime-repo={spec['runtime_repo']}", str(repo),
         str(out_dir / bundle_name), app_id, branch],
        cwd=work, log_fh=log_fh, timeout=1800, env=env,
    )
    if rc != 0:
        return {"error": "flatpak build-bundle failed", "revision": revision}
    # Keep the local build repo small: only the latest commit matters.
    await _proc(["ostree", f"--repo={repo}", "prune", "--refs-only", "--depth=0"],
                cwd=work, log_fh=log_fh, timeout=600, env=env)
    return {"status": "built", "bundle": bundle_name, "revision": revision}


async def _builder_startup(ctx: dict) -> None:
    configure_logging()
    _work_root().mkdir(parents=True, exist_ok=True)
    log.info("flatpak builder starting", queue=settings.builder_queue)


class BuilderSettings:
    functions = [run_flatpak_build]
    queue_name = settings.builder_queue
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    on_startup = _builder_startup
    max_jobs = 1
    job_timeout = settings.builder_timeout_s + 600
    keep_result = 86400
    health_check_interval = 60
    queue_read_limit = 1
