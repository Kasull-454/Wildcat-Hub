"""Legacy F-Droid keystore helpers (``app.fdroid.keystore``).

The setup wizard now manages a GPG key instead: see
``test_flatpak_gpg_setup.py`` for the confirm_destroy / backup rules."""
from __future__ import annotations

import base64
import stat
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from fastapi import HTTPException

from app.core.config import settings
from app.fdroid import keystore as keystore_module
from app.fdroid.keystore import KeystoreError, check_signing_keystore, import_keystore
from app.schemas.repo import SetupWizardRequest

STORE_PW = "store-pass-123"
RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _cert(signing_key: Any, public_key: Any) -> x509.Certificate:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    now = datetime.now(UTC)
    return (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(signing_key, hashes.SHA256())
    )


def _p12(
    *, alias: str | None = "repokey", key: Any = RSA_KEY,
    with_key: bool = True, pw: str = STORE_PW,
) -> bytes:
    cert = _cert(key, key.public_key())
    return pkcs12.serialize_key_and_certificates(
        name=alias.encode() if alias else None,
        key=key if with_key else None,
        cert=cert if with_key else None,
        cas=None if with_key else [cert],
        encryption_algorithm=serialization.BestAvailableEncryption(pw.encode()),
    )


def _fingerprint(p12: bytes) -> str:
    loaded = pkcs12.load_pkcs12(p12, STORE_PW.encode())
    return loaded.cert.certificate.fingerprint(hashes.SHA256()).hex()


# --------------------------------------------------------------------------
# What a signing keystore must hold
# --------------------------------------------------------------------------
def test_rsa_key_with_its_certificate_under_the_alias_passes() -> None:
    check_signing_keystore(_p12(), STORE_PW, "repokey")
    check_signing_keystore(_p12(alias="RepoKey"), STORE_PW, "repokey")  # JDK: case-insensitive


@pytest.mark.parametrize(
    ("p12", "message"),
    [
        (lambda: _p12(alias="other"), "alias"),
        (lambda: _p12(alias=None), "alias"),
        (lambda: _p12(key=ec.generate_private_key(ec.SECP256R1())), "RSA"),
        (lambda: _p12(with_key=False), "no private key"),
        (lambda: _p12(pw="something-else"), "parse failed"),
    ],
)
def test_unusable_keystores_are_refused(p12, message: str) -> None:
    with pytest.raises(KeystoreError, match=message):
        check_signing_keystore(p12(), STORE_PW, "repokey")


def test_certificate_of_another_key_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``cryptography`` won't even write such a store; other tools can.
    mismatched = pkcs12.PKCS12KeyAndCertificates(
        RSA_KEY,
        pkcs12.PKCS12Certificate(_cert(OTHER_RSA_KEY, OTHER_RSA_KEY.public_key()), b"repokey"),
        [],
    )
    monkeypatch.setattr(
        keystore_module, "pkcs12", SimpleNamespace(load_pkcs12=lambda data, pwd: mismatched)
    )
    with pytest.raises(KeystoreError, match="does not match"):
        check_signing_keystore(b"p12", STORE_PW, "repokey")


# --------------------------------------------------------------------------
# import_keystore
# --------------------------------------------------------------------------
def _siblings(path: Path) -> list[str]:
    return sorted(p.name for p in path.parent.iterdir())


def _read(path: Path) -> bytes:
    return path.read_bytes()


def _backups(path: Path) -> list[Path]:
    return [p for p in path.parent.iterdir() if p.name.startswith(f"{path.name}.bak-")]


async def test_import_backs_up_and_replaces_atomically(tmp_path: Path) -> None:
    path = tmp_path / "repo.p12"
    path.write_bytes(b"old keystore")
    new = _p12()
    info = await import_keystore(
        path, content=new, keystore_password=STORE_PW, alias="repokey", backup=True
    )
    assert _read(path) == new
    assert info.path == path and info.fingerprint_sha256 == _fingerprint(new)
    (backup,) = _backups(path)
    assert backup.read_bytes() == b"old keystore"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert _siblings(path) == sorted(["repo.p12", backup.name])  # no temp file left


async def test_refused_import_changes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "repo.p12"
    path.write_bytes(b"old keystore")
    with pytest.raises(KeystoreError):
        await import_keystore(
            path, content=_p12(alias="other"), keystore_password=STORE_PW,
            alias="repokey", backup=True,
        )
    assert _read(path) == b"old keystore"
    assert _siblings(path) == ["repo.p12"]
