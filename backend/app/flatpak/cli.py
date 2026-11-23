"""Thin async wrapper around the ``flatpak`` / ``ostree`` / ``gpg`` CLIs.

Everything that touches an OSTree repo goes through these tools rather
than libostree bindings: they are what Flathub itself runs, they ship in
Debian, and they keep the Python side free of GObject introspection.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.core.logging import get_logger

log = get_logger(__name__)


class CommandError(RuntimeError):
    def __init__(self, argv: list[str], code: int, stderr: str) -> None:
        self.argv = argv
        self.code = code
        self.stderr = stderr
        tail = stderr.strip().splitlines()[-3:] if stderr else []
        super().__init__(f"{Path(argv[0]).name} exited {code}: {' | '.join(tail)}"[:600])


@dataclass
class Result:
    stdout: str
    stderr: str
    stdout_bytes: bytes


def _env(gpg_home: str | Path | None = None) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        # flatpak build-* commands never need a user installation, but the
        # CLI still resolves one; keep it on the writable tmpfs.
        "FLATPAK_USER_DIR": os.environ.get("FLATPAK_USER_DIR", "/tmp/flatpak-user"),
        "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", "/tmp"),
        # libostree / flatpak default to /var/tmp for O_TMPFILE scratch
        # files; on a read-only rootfs (Docker read_only) only the temp
        # dir is writable, so point them at it.
        "TMPDIR": tempfile.gettempdir(),
    }
    if gpg_home is not None:
        env["GNUPGHOME"] = str(gpg_home)
    return env


async def run(
    *argv: str | Path,
    timeout: float = 600,
    gpg_home: str | Path | None = None,
    stdin: bytes | None = None,
    cwd: str | Path | None = None,
) -> Result:
    args = [str(a) for a in argv]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_env(gpg_home),
        cwd=str(cwd) if cwd else None,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise CommandError(args, -9, f"timed out after {timeout}s") from None
    stderr = err.decode("utf-8", "replace")
    if proc.returncode != 0:
        log.warning("command failed", argv=args[:4], code=proc.returncode, stderr=stderr[-400:])
        raise CommandError(args, proc.returncode or -1, stderr)
    return Result(stdout=out.decode("utf-8", "replace"), stderr=stderr, stdout_bytes=out)


async def ostree(repo: Path, *args: str | Path, **kw) -> Result:
    return await run("ostree", f"--repo={repo}", *args, **kw)


async def init_archive_repo(path: Path, *, parent: Path | None = None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if not (path / "config").exists():
        await run("ostree", "init", "--mode=archive-z2", f"--repo={path}")
    if parent is not None:
        await ostree(path, "config", "set", "core.parent", str(parent))
