"""Setup wizard on the GPG signing key: generate / import, refusing to
replace a live key without ``confirm_destroy``, and keeping a backup when
it is replaced. Needs the ``gpg`` binary (present in the backend image)."""
from __future__ import annotations

import base64
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from app.api.v1 import setup as setup_api
from app.core.config import settings
from app.flatpak import gpg
from app.schemas.repo import SetupWizardRequest

pytestmark = pytest.mark.skipif(shutil.which("gpg") is None, reason="gpg not installed")


class _Result:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one(self) -> Any:
        return self.value


class FakeDb:
    def __init__(self, config: Any) -> None:
        self.config = config

    async def execute(self, stmt: Any) -> _Result:
        return _Result(self.config)

    async def flush(self) -> None:
        return None


def _config(*, setup_complete: bool, fpr: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        name="r", description=None, address="https://repo.example", setup_complete=setup_complete,
        keystore_fingerprint_sha256=fpr,
    )


def _payload(mode: str, *, key: bytes | None = None, confirm_destroy: bool = False) -> SetupWizardRequest:
    return SetupWizardRequest(
        repo_name="Wildcat", repo_address="https://repo.example/fdroid/repo", keystore_mode=mode,
        keystore_b64=base64.b64encode(key).decode() if key is not None else None,
        confirm_destroy=confirm_destroy,
    )


@pytest.fixture
def gpg_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "gnupg"
    monkeypatch.setattr(settings, "gpg_home", str(home))
    # The fake config row only carries what the wizard touches.
    monkeypatch.setattr(setup_api, "RepoConfigRead", SimpleNamespace(model_validate=lambda c: c))
    return home


async def _armored_secret(tmp_path: Path) -> bytes:
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    (other / "common.conf").write_text("")
    await gpg._gpg("--quick-gen-key", "Other <other@example.org>", "ed25519", "sign", "never", home=other)
    return (await gpg._gpg("--armor", "--export-secret-keys", home=other)).stdout_bytes


async def test_generate_creates_a_signing_key(gpg_home: Path) -> None:
    config = _config(setup_complete=False)
    await setup_api.run_setup_wizard(_payload("generate"), FakeDb(config), SimpleNamespace(username="a"))
    assert gpg.key_present()
    assert config.setup_complete
    assert config.keystore_fingerprint_sha256 == await gpg.fingerprint()
    assert (await gpg.public_key_b64())


async def test_replacing_a_live_key_needs_confirm_destroy(gpg_home: Path, tmp_path: Path) -> None:
    config = _config(setup_complete=False)
    await setup_api.run_setup_wizard(_payload("generate"), FakeDb(config), SimpleNamespace(username="a"))
    before = config.keystore_fingerprint_sha256
    with pytest.raises(HTTPException) as exc:
        await setup_api.run_setup_wizard(
            _payload("import", key=await _armored_secret(tmp_path)), FakeDb(config), SimpleNamespace(username="a"),
        )
    assert exc.value.status_code == 409
    assert await gpg.fingerprint() == before


async def test_confirmed_import_swaps_the_key_and_keeps_a_backup(gpg_home: Path, tmp_path: Path) -> None:
    config = _config(setup_complete=False)
    await setup_api.run_setup_wizard(_payload("generate"), FakeDb(config), SimpleNamespace(username="a"))
    before = config.keystore_fingerprint_sha256
    await setup_api.run_setup_wizard(
        _payload("import", key=await _armored_secret(tmp_path), confirm_destroy=True),
        FakeDb(config), SimpleNamespace(username="a"),
    )
    assert config.keystore_fingerprint_sha256 != before
    assert await gpg.fingerprint() == config.keystore_fingerprint_sha256
    assert list(gpg_home.parent.glob("gnupg.bak-*"))


async def test_garbage_import_is_a_400_and_keeps_the_key(gpg_home: Path) -> None:
    config = _config(setup_complete=False)
    await setup_api.run_setup_wizard(_payload("generate"), FakeDb(config), SimpleNamespace(username="a"))
    before = await gpg.fingerprint()
    with pytest.raises(HTTPException) as exc:
        await setup_api.run_setup_wizard(
            _payload("import", key=b"not a key", confirm_destroy=True), FakeDb(config), SimpleNamespace(username="a"),
        )
    assert exc.value.status_code == 400
    assert await gpg.fingerprint() == before


async def test_a_configured_key_whose_home_vanished_still_needs_confirm(gpg_home: Path) -> None:
    config = _config(setup_complete=True, fpr="AB" * 20)
    with pytest.raises(HTTPException) as exc:
        await setup_api.run_setup_wizard(_payload("generate"), FakeDb(config), SimpleNamespace(username="a"))
    assert exc.value.status_code == 409
