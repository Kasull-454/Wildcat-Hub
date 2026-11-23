"""Flatpak client-facing endpoints.

Mounted at ``/flatpak``:

* ``/flatpak/repo/<ostree path>``          — the OSTree repo (anonymous /
  Basic-auth API key)
* ``/r/<token>/flatpak/repo/<ostree path>`` — same, API key in the path
  (what ``flatpak remote-add`` gets for a private repo: libostree has no
  per-remote auth header, a URL token works everywhere)
* ``/flatpak/<remote>.flatpakrepo`` and ``/flatpak/apps/<id>.flatpakref``
  (+ token twins) — one-click files for GNOME Software / KDE Discover /
  ``flatpak install --from``

The variant a caller gets is decided exactly like the F-Droid index
variants: public, public + NSFW for opted-in users, or the owner's
per-user repo when their key may download private apps. A ``flatpak``
install fetches hundreds of objects, so the (costly) API key check and the
variant decision are cached in-process for a short time.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, PlainTextResponse, Response
from sqlalchemy import select

from app.api.deps import DbSession, _api_key_from_secret, get_api_key_from_basic_auth, is_public_mode
from app.core.client_ip import client_ip, hash_ip
from app.core.config import settings
from app.core.logging import get_logger
from app.flatpak import gpg
from app.flatpak import repo_builder as rb
from app.models.api_key import ApiKey
from app.models.apk import Apk, ApkStatus
from app.models.app import App, AppStatus, AppVisibility
from app.models.audit import DownloadEvent
from app.models.repo_config import RepoConfig
from app.models.user import User

log = get_logger(__name__)

router = APIRouter()
token_router = APIRouter()

# Paths a flatpak / ostree client asks for. Anything else is a 404 before
# touching the filesystem.
_ROOT_FILES = {"config", "summary", "summary.sig", "summary.idx", "summary.idx.sig"}
_OBJECT_RE = re.compile(r"^objects/[0-9a-f]{2}/[0-9a-f]{62}\.(commit|commitmeta|dirtree|dirmeta|filez|file|sig)$")
_SUMMARIES_RE = re.compile(r"^summaries/[0-9a-f]{64}\.(gz|sig)$")
_DELTA_RE = re.compile(r"^(deltas/[A-Za-z0-9_+-]{2}/[A-Za-z0-9_+-]{1,100}(-[A-Za-z0-9_+-]{1,100})?/(superblock|[0-9]{1,6})|delta-indexes/[A-Za-z0-9_+-]{2}/[A-Za-z0-9_+-]{1,100}\.index)$")
_REF_RE = re.compile(r"^refs/heads/(app|appstream|appstream2)(/[A-Za-z0-9._-]{1,255}){1,3}$")
_APP_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*(\.[A-Za-z_][A-Za-z0-9_-]*){2,}$")
_REMOTE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_CACHE_TTL = 30.0
_key_cache: dict[str, tuple[float, str | None]] = {}   # secret -> (expiry, variant or None=invalid)
_public_mode_cache: list[float | bool] = [0.0, True]


def _cache_get(secret: str) -> tuple[bool, str | None]:
    hit = _key_cache.get(secret)
    if hit is None or hit[0] < time.monotonic():
        return False, None
    return True, hit[1]


def _cache_put(secret: str, variant: str | None) -> None:
    if len(_key_cache) > 2048:
        _key_cache.clear()
    _key_cache[secret] = (time.monotonic() + _CACHE_TTL, variant)


async def _public_mode(db) -> bool:
    if _public_mode_cache[0] > time.monotonic():  # type: ignore[operator]
        return bool(_public_mode_cache[1])
    value = await is_public_mode(db)
    _public_mode_cache[:] = [time.monotonic() + _CACHE_TTL, value]
    return value


async def variant_for(db, api_key: ApiKey | None) -> str:
    """Same decision as the F-Droid fork's ``_index_prefix``."""
    if api_key is None or not api_key.can_download_private:
        return rb.PUBLIC
    raw = (await db.execute(select(RepoConfig.private_index_owner_ids).limit(1))).scalar_one_or_none()
    try:
        owners = json.loads(raw or "[]")
    except json.JSONDecodeError:
        owners = []
    if isinstance(owners, list) and str(api_key.user_id) in owners:
        name = rb.user_variant(api_key.user_id)
        if rb.variant_dir(name) is not None:
            return name
    show_nsfw = (await db.execute(select(User.show_nsfw).where(User.id == api_key.user_id))).scalar_one_or_none()
    if show_nsfw and rb.variant_dir(rb.PUBLIC_NSFW) is not None:
        return rb.PUBLIC_NSFW
    return rb.PUBLIC


async def _variant_from_token(token: str, db) -> str:
    cached, variant = _cache_get(token)
    if not cached:
        key = await _api_key_from_secret(token, db)
        variant = await variant_for(db, key) if key is not None else None
        _cache_put(token, variant)
    if variant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return variant


def _cache_control(path: str) -> str:
    if path.startswith(("objects/", "deltas/", "delta-indexes/")) or _SUMMARIES_RE.match(path):
        return "private, max-age=31536000, immutable"
    return "no-cache, must-revalidate"


def _resolve(variant: str, path: str) -> Path:
    if not (path in _ROOT_FILES or _OBJECT_RE.match(path) or _SUMMARIES_RE.match(path)
            or _DELTA_RE.match(path) or _REF_RE.match(path)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    chain = rb.lookup_chain(variant)
    if not chain:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository not built yet: finish setup and publish an app.",
        )
    # Only content-addressed objects fall through to the stores; refs and
    # summaries always come from the variant itself.
    candidates = chain if path.startswith(("objects/", "deltas/", "delta-indexes/")) else chain[:1]
    for base in candidates:
        f = base / path
        if f.is_file():
            return f
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def _file_response(f: Path, path: str) -> Response:
    headers = {"Cache-Control": _cache_control(path)}
    ctype = "application/octet-stream"
    storage_root = Path(settings.local_storage_path).resolve()
    real = f.resolve()
    if settings.x_accel_redirect_enabled and real.is_relative_to(storage_root):
        rel = real.relative_to(storage_root).as_posix()
        headers["X-Accel-Redirect"] = "/_protected/" + quote(rel, safe="/")
        headers["Content-Type"] = ctype
        return Response(status_code=200, headers=headers)
    return FileResponse(str(real), media_type=ctype, headers=headers)


async def _record_install(db, request: Request, commit: str, api_key_id=None, user_id=None) -> None:
    """A ``.commit`` fetch is the first object of an install / update."""
    try:
        apk = (await db.execute(select(Apk).where(Apk.ostree_commit == commit).limit(1))).scalar_one_or_none()
        if apk is None:
            return
        async with db.begin_nested():
            db.add(DownloadEvent(
                apk_id=apk.id, app_id=apk.app_id, user_id=user_id, api_key_id=api_key_id,
                ip_hash=hash_ip(client_ip(request)),
                user_agent=(request.headers.get("user-agent") or "")[:512] or None,
                bytes_served=apk.size_bytes, status_code=200,
            ))
    except Exception as exc:  # noqa: BLE001 — stats never fail a fetch
        log.warning("could not record install", error=str(exc))


async def _serve(variant: str, path: str, request: Request, db, api_key: ApiKey | None = None) -> Response:
    f = _resolve(variant, path)
    if path.endswith(".commit"):
        commit = path[8:10] + path[11:73]
        await _record_install(db, request, commit,
                              api_key_id=api_key.id if api_key else None,
                              user_id=api_key.user_id if api_key else None)
    return _file_response(f, path)


# --------------------------------------------------------------------------
# Repo files
# --------------------------------------------------------------------------
@router.get("/repo/{path:path}")
async def serve_repo(
    path: str,
    request: Request,
    db: DbSession,
    api_key: Annotated[ApiKey | None, Depends(get_api_key_from_basic_auth)] = None,
) -> Response:
    if api_key is None and not await _public_mode(db):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": 'Basic realm="wildcat-hub"'},
        )
    variant = await variant_for(db, api_key) if api_key is not None else rb.PUBLIC
    return await _serve(variant, path, request, db, api_key)


@token_router.get("/{token}/flatpak/repo/{path:path}")
async def serve_token_repo(token: str, path: str, request: Request, db: DbSession) -> Response:
    variant = await _variant_from_token(token, db)
    return await _serve(variant, path, request, db)


# --------------------------------------------------------------------------
# .flatpakrepo / .flatpakref
# --------------------------------------------------------------------------
async def _site_base(db, request: Request) -> str:
    cfg = (await db.execute(select(RepoConfig).limit(1))).scalar_one_or_none()
    addr = (cfg.address or "").rstrip("/") if cfg else ""
    for suffix in ("/fdroid/repo", "/flatpak/repo"):
        if addr.endswith(suffix):
            addr = addr[: -len(suffix)]
    return addr or str(request.base_url).rstrip("/")


def _kv(lines: dict[str, str | None], group: str) -> str:
    body = "\n".join(f"{k}={v}" for k, v in lines.items() if v not in (None, ""))
    return f"[{group}]\n{body}\n"


def _one_line(value: str | None, limit: int = 300) -> str | None:
    if not value:
        return None
    return re.sub(r"\s+", " ", value).strip()[:limit]


async def _flatpakrepo(db, request: Request, repo_url: str) -> Response:
    cfg = (await db.execute(select(RepoConfig).limit(1))).scalar_one_or_none()
    if cfg is None or not gpg.key_present():
        raise HTTPException(status_code=404, detail="Repository not set up yet")
    base = await _site_base(db, request)
    body = _kv({
        "Title": _one_line(cfg.name, 100),
        "Url": repo_url,
        "Homepage": base + "/",
        "Comment": _one_line(cfg.description),
        "Description": _one_line(cfg.description, 1000),
        "Icon": f"{base}/fdroid/repo/icons/{cfg.icon_path.rsplit('/', 1)[-1]}" if cfg.icon_path else None,
        "GPGKey": await gpg.public_key_b64(),
    }, "Flatpak Repo")
    return PlainTextResponse(body, media_type="application/vnd.flatpak.repo", headers={
        "Content-Disposition": f'attachment; filename="{settings.flatpak_remote_name}.flatpakrepo"',
        "Cache-Control": "no-cache",
    })


async def _flatpakref(db, request: Request, app_id: str, repo_url: str, api_key: ApiKey | None) -> Response:
    if not _APP_ID_RE.match(app_id):
        raise HTTPException(status_code=404, detail="Not found")
    app = (await db.execute(select(App).where(App.package_name == app_id))).scalar_one_or_none()
    if app is None or app.status != AppStatus.PUBLISHED:
        raise HTTPException(status_code=404, detail="App not found")
    if app.visibility == AppVisibility.PRIVATE and not (
        api_key is not None and api_key.can_download_private and api_key.user_id == app.owner_id
    ):
        raise HTTPException(status_code=404, detail="App not found")
    await db.refresh(app, ["apks"])
    refs = rb.select_refs(app)
    if not refs:
        raise HTTPException(status_code=404, detail="App has no published build")
    # Prefer x86_64 + stable-ish branches for the one-click file.
    ref = sorted(refs, key=lambda r: (r.split("/")[2] != "x86_64", r.split("/")[3] not in ("stable", "master")))[0]
    branch = ref.split("/")[3]
    body = _kv({
        "Title": _one_line(app.name, 100),
        "Name": app_id,
        "Branch": branch,
        "Url": repo_url,
        "Homepage": app.website,
        "Comment": _one_line(app.summary),
        "IsRuntime": "false",
        "SuggestRemoteName": settings.flatpak_remote_name,
        "RuntimeRepo": settings.flatpak_runtime_repo,
        "GPGKey": await gpg.public_key_b64(),
    }, "Flatpak Ref")
    return PlainTextResponse(body, media_type="application/vnd.flatpak.ref", headers={
        "Content-Disposition": f'attachment; filename="{app_id}.flatpakref"',
        "Cache-Control": "no-cache",
    })


@router.get("/{remote}.flatpakrepo")
async def public_flatpakrepo(remote: str, request: Request, db: DbSession) -> Response:
    if not _REMOTE_RE.match(remote):
        raise HTTPException(status_code=404, detail="Not found")
    if not await _public_mode(db):
        raise HTTPException(status_code=401, detail="Authentication required")
    return await _flatpakrepo(db, request, (await _site_base(db, request)) + "/flatpak/repo/")


@router.get("/apps/{app_id}.flatpakref")
async def public_flatpakref(app_id: str, request: Request, db: DbSession) -> Response:
    if not await _public_mode(db):
        raise HTTPException(status_code=401, detail="Authentication required")
    return await _flatpakref(db, request, app_id, (await _site_base(db, request)) + "/flatpak/repo/", None)


@token_router.get("/{token}/flatpak/{remote}.flatpakrepo")
async def token_flatpakrepo(token: str, remote: str, request: Request, db: DbSession) -> Response:
    if not _REMOTE_RE.match(remote):
        raise HTTPException(status_code=404, detail="Not found")
    await _variant_from_token(token, db)
    return await _flatpakrepo(db, request, f"{await _site_base(db, request)}/r/{token}/flatpak/repo/")


@token_router.get("/{token}/flatpak/apps/{app_id}.flatpakref")
async def token_flatpakref(token: str, app_id: str, request: Request, db: DbSession) -> Response:
    key = await _api_key_from_secret(token, db)
    if key is None:
        raise HTTPException(status_code=404, detail="Not found")
    return await _flatpakref(db, request, app_id, f"{await _site_base(db, request)}/r/{token}/flatpak/repo/", key)
