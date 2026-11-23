"""Repository signing key (GPG) management.

Flatpak clients pin a remote to one OpenPGP key: every commit and every
``summary`` is signed with it. The key lives in a dedicated GnuPG home on
the ``keystores`` volume and never leaves it — the API only ever exports
the public half (embedded in ``.flatpakrepo`` / ``.flatpakref`` files).
"""
from __future__ import annotations

import asyncio
import base64
import os
import re
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import settings
from app.flatpak import cli


class GpgError(RuntimeError):
    pass


@dataclass
class GpgKeyInfo:
    fingerprint: str
    user_id: str
    created_at: datetime | None
    algorithm: str


def gpg_home() -> Path:
    """The persistent keyring (on the keystores volume)."""
    return Path(settings.gpg_home)


def _fingerprint_of_tree(root: Path) -> tuple:
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.name.startswith("S.") and not p.name.endswith(".lock"):
            st = p.stat()
            out.append((str(p.relative_to(root)), st.st_size, int(st.st_mtime)))
    return tuple(out)


_runtime: dict[str, object] = {}


def runtime_home() -> Path:
    """A private copy of the keyring for this process.

    backend and worker share the keystores volume, but ``gpg-agent`` keeps
    its socket inside GNUPGHOME: an agent started by one container is a
    dead socket for the other. Every process therefore signs from its own
    copy under the temp dir, refreshed whenever the persistent keyring
    changes (key generated / imported / rotated).
    """
    src = gpg_home()
    dst = Path(tempfile.gettempdir()) / f"wildcat-gnupg-{socket.gethostname()[:16]}-{os.getpid()}"
    sig = _fingerprint_of_tree(src) if src.is_dir() else ()
    if _runtime.get("sig") != sig or not dst.is_dir():
        if dst.exists():
            subprocess.run(["gpgconf", "--kill", "all"], env={**os.environ, "GNUPGHOME": str(dst)},
                           check=False, capture_output=True, timeout=30)
            shutil.rmtree(dst, ignore_errors=True)
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("S.*", "*.lock", "random_seed"))
            os.chmod(dst, 0o700)
        _runtime["sig"] = sig
    return dst


def key_present() -> bool:
    home = gpg_home()
    return (home / "pubring.kbx").exists() or (home / "pubring.gpg").exists()


def _prepare_home(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    # No agent sockets on a read-only rootfs: keep them in the home itself
    # and never try to autostart dirmngr / keyboxd over the network.
    # GnuPG >= 2.4 may default to keyboxd; an empty common.conf keeps the
    # classic pubring.kbx file, which survives a plain directory backup.
    (home / "common.conf").write_text("")


async def _gpg(*args: str, stdin: bytes | None = None, home: Path | None = None) -> cli.Result:
    home = home or await asyncio.to_thread(runtime_home)
    return await cli.run(
        "gpg", "--batch", "--no-tty", "--pinentry-mode", "loopback", "--passphrase", "",
        *args, gpg_home=home, stdin=stdin, timeout=120,
    )


async def _info(home: Path | None = None) -> GpgKeyInfo:
    res = await _gpg("--list-secret-keys", "--with-colons", "--fixed-list-mode", home=home)
    fpr = uid = algo = None
    created = None
    for line in res.stdout.splitlines():
        parts = line.split(":")
        if parts[0] == "sec" and algo is None:
            algo = {"1": "RSA", "17": "DSA", "22": "EdDSA", "19": "ECDSA"}.get(parts[3], parts[3])
            if parts[5].isdigit():
                created = datetime.fromtimestamp(int(parts[5]), tz=UTC)
        elif parts[0] == "fpr" and fpr is None:
            fpr = parts[9]
        elif parts[0] == "uid" and uid is None:
            uid = parts[9]
    if not fpr:
        raise GpgError("no secret key in the repository keyring")
    return GpgKeyInfo(fingerprint=fpr.upper(), user_id=uid or "", created_at=created, algorithm=algo or "?")


async def read_key_info() -> GpgKeyInfo:
    if not key_present():
        raise GpgError("repository signing key missing")
    return await _info()


async def fingerprint() -> str:
    return (await read_key_info()).fingerprint


_UID_SAFE = re.compile(r"[^\w .,@()+-]", re.UNICODE)


async def generate_key(name: str, email: str) -> GpgKeyInfo:
    """Create a fresh signing-only ed25519 key, replacing any previous home."""
    home = gpg_home()
    staging = home.with_name(home.name + ".new")
    shutil.rmtree(staging, ignore_errors=True)
    _prepare_home(staging)
    uid = f"{_UID_SAFE.sub('', name).strip() or 'Wildcat Hub'} <{_UID_SAFE.sub('', email).strip() or 'repo@localhost'}>"
    try:
        await _gpg("--quick-gen-key", uid, "ed25519", "sign", "never", home=staging)
        info = await _info(staging)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    await asyncio.to_thread(_swap_home, staging, home)
    return info


async def import_key(armored_secret_key: bytes) -> GpgKeyInfo:
    home = gpg_home()
    staging = home.with_name(home.name + ".new")
    shutil.rmtree(staging, ignore_errors=True)
    _prepare_home(staging)
    try:
        await _gpg("--import", stdin=armored_secret_key, home=staging)
        info = await _info(staging)
    except cli.CommandError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise GpgError(f"could not import key: {exc}") from exc
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    await asyncio.to_thread(_swap_home, staging, home)
    return info


def _swap_home(staging: Path, home: Path) -> None:
    subprocess.run(["gpgconf", "--kill", "all"], env={**os.environ, "GNUPGHOME": str(staging)},
                   check=False, capture_output=True, timeout=30)
    if home.exists():
        ts = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        home.rename(home.with_name(f"{home.name}.bak-{ts}"))
    staging.rename(home)


async def delete_key() -> None:
    shutil.rmtree(gpg_home(), ignore_errors=True)


async def export_public_key(armor: bool = False) -> bytes:
    fpr = await fingerprint()
    args = ["--export", "--export-options", "export-minimal"]
    if armor:
        args.insert(0, "--armor")
    return (await _gpg(*args, fpr)).stdout_bytes


async def public_key_b64() -> str:
    """The base64 form ``GPGKey=`` expects in .flatpakrepo / .flatpakref."""
    return base64.b64encode(await export_public_key()).decode()


def sign_args() -> list[str]:
    """Extra args for ``flatpak build-*`` / ``ostree`` signing calls."""
    return [f"--gpg-homedir={runtime_home()}"]
