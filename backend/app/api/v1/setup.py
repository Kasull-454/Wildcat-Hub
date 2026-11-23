from __future__ import annotations

import base64
from typing import Annotated

from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select

from app.api.deps import DbSession, get_current_admin
from app.core.logging import get_logger
from app.flatpak import gpg
from app.flatpak.cli import CommandError
from app.models.repo_config import RepoConfig
from app.models.user import User
from app.schemas.repo import KeystoreInfo, RepoConfigRead, SetupStatus, SetupWizardRequest

router = APIRouter()
log = get_logger(__name__)


@router.get("/status", response_model=SetupStatus)
async def setup_status(db: DbSession) -> SetupStatus:
    config = (await db.execute(select(RepoConfig).limit(1))).scalar_one_or_none()
    return SetupStatus(
        setup_complete=bool(config and config.setup_complete),
        keystore_present=gpg.key_present(),
        repo_name=config.name if config else None,
        repo_description=config.description if config else None,
        repo_address=config.address if config else None,
        repo_icon_path=config.icon_path if config else None,
        repo_fingerprint=config.keystore_fingerprint_sha256 if config else None,
        public_mode=config.public_mode if config else True,
        upload_max_apk_mb=config.upload_max_apk_mb if config else 200,
        reproducible_builds_enabled=(
            config.reproducible_builds_enabled if config else True
        ),
    )


@router.post("/wizard", response_model=RepoConfigRead)
async def run_setup_wizard(
    payload: SetupWizardRequest,
    db: DbSession,
    admin: Annotated[User, Depends(get_current_admin)],
) -> RepoConfigRead:
    """One-shot setup: repo metadata + create-or-import the GPG signing key.

    ``keystore_mode`` keeps its name from the F-Droid fork: ``generate``
    creates an ed25519 key, ``import`` takes an ASCII-armored secret key
    (``gpg --armor --export-secret-keys``) as base64 in ``keystore_b64``.
    """
    config = (await db.execute(select(RepoConfig).limit(1))).scalar_one()

    config.name = payload.repo_name
    config.description = payload.repo_description
    config.address = str(payload.repo_address).rstrip("/")

    # Replacing the key breaks every client that added the remote: they
    # pinned the old public key and will refuse the next summary.
    has_key = gpg.key_present() or bool(config.keystore_fingerprint_sha256)
    if has_key and config.setup_complete and not payload.confirm_destroy:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Replacing the signing key invalidates the remote for every "
                "Flatpak client already subscribed to this repo. Pass "
                "``confirm_destroy=true`` to acknowledge it, and back up the "
                "keystores volume first."
            ),
        )

    try:
        if payload.keystore_mode == "generate":
            host = urlparse(config.address).hostname or "localhost"
            email = (payload.key_dname or "").strip()
            if "@" not in email:
                email = f"repo@{host}"
            info = await gpg.generate_key(payload.repo_name, email)
        elif payload.keystore_mode == "import":
            if not payload.keystore_b64:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="keystore_b64 is required when keystore_mode=import",
                )
            try:
                content = base64.b64decode(payload.keystore_b64)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="keystore_b64 is not valid base64",
                ) from exc
            info = await gpg.import_key(content)
        else:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid keystore_mode")
    except gpg.GpgError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except CommandError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc

    config.keystore_fingerprint_sha256 = info.fingerprint
    config.setup_complete = True
    await db.flush()
    log.info("setup wizard completed", admin=admin.username,
             keystore_mode=payload.keystore_mode, fingerprint=info.fingerprint)
    return RepoConfigRead.model_validate(config)


@router.get("/keystore", response_model=KeystoreInfo)
async def keystore_info(
    _: Annotated[User, Depends(get_current_admin)],
) -> KeystoreInfo:
    if not gpg.key_present():
        return KeystoreInfo(present=False, fingerprint_sha256=None, alias=None, not_before=None, not_after=None)
    try:
        info = await gpg.read_key_info()
    except (gpg.GpgError, CommandError) as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
    return KeystoreInfo(
        present=True,
        fingerprint_sha256=info.fingerprint,
        alias=info.user_id,
        not_before=info.created_at,
        not_after=None,
    )


@router.get("/public-key")
async def public_key() -> Response:
    """The repo's public key, ASCII-armored (for ``--gpg-import`` by hand)."""
    if not gpg.key_present():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No signing key yet")
    return Response(
        content=await gpg.export_public_key(armor=True),
        media_type="application/pgp-keys",
        headers={"Content-Disposition": 'attachment; filename="wildcat-repo.asc"'},
    )
