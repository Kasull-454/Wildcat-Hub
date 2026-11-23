"""Read the metadata out of a single-file ``.flatpak`` bundle.

A bundle is an OSTree static delta plus a small header. The robust way to
look inside is to let ``flatpak build-import-bundle`` unpack it into a
throw-away archive repo (OSTree verifies every object checksum on the
way in), then read the commit with ``ostree``:

* ``/metadata``                       — keyfile: runtime, sdk, sandbox
* ``/files/share/app-info/xmls/*``    — composed AppStream (flatpak-builder)
* ``/files/share/metainfo/<id>.xml``  — raw MetaInfo (fallback)
* ``/files/share/app-info/icons`` or hicolor icons

The returned :class:`BundleMetadata` keeps the field names of the old
``ApkMetadata`` so the upload / import pipeline stays shared; the Flatpak
specific bits ride along in the extra fields.
"""
from __future__ import annotations

import configparser
import gzip
import hashlib
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

from app.flatpak import cli

# Bundles are fed through the same tempfile helpers as APKs used to be;
# see ``save_upload_to_temp``. The allowlist keeps the path-injection
# barrier the APK parser had.
_BASENAME_RE = re.compile(r"[A-Za-z0-9_-]{1,128}\.flatpak")
_APP_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*(\.[A-Za-z_][A-Za-z0-9_-]*){2,}$")
_REF_RE = re.compile(r"^app/([^/]+)/([^/]+)/([^/]+)$")
_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
_ICON_MAX = 2 * 1024 * 1024
_XML_MAX = 4 * 1024 * 1024


class BundleParseError(RuntimeError):
    """Raised when a file is not a usable Flatpak application bundle."""


@dataclass
class BundleMetadata:
    package_name: str                    # Flatpak app ID, e.g. org.gnome.Maps
    version_code: int                    # commit timestamp (monotonic per build)
    version_name: str
    min_sdk: int | None = None           # kept for schema compatibility
    target_sdk: int | None = None
    max_sdk: int | None = None
    permissions: list[str] = field(default_factory=list)   # finish-args
    features: list[str] = field(default_factory=list)      # runtime / sdk refs
    native_code: list[str] = field(default_factory=list)   # [arch]
    locales: list[str] = field(default_factory=list)
    signer_sha256: str = ""
    sha256: str = ""
    size_bytes: int = 0
    app_name: str | None = None
    icon_data: bytes | None = None
    icon_extension: str | None = None
    # Flatpak specifics
    flatpak_ref: str = ""
    ostree_commit: str = ""
    arch: str = ""
    branch: str = ""
    runtime: str | None = None
    sdk: str | None = None
    summary: str | None = None
    description: str | None = None
    license: str | None = None
    homepage: str | None = None
    bugtracker: str | None = None
    developer: str | None = None
    categories: list[str] = field(default_factory=list)
    commit_date: datetime | None = None


async def parse_bundle(path: str | Path) -> BundleMetadata:
    basename = os.path.basename(str(path))
    if not _BASENAME_RE.fullmatch(basename):
        raise BundleParseError("bundle basename must be a tempfile-style filename")
    safe_path = Path(os.path.join(tempfile.gettempdir(), basename))
    if not safe_path.is_file():
        raise BundleParseError(f"bundle not found at {safe_path}")
    try:
        return await _parse(safe_path)
    except BundleParseError:
        raise
    except cli.CommandError as exc:
        raise BundleParseError(f"not a valid Flatpak bundle: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise BundleParseError(f"malformed bundle ({type(exc).__name__}): {str(exc)[:200]}") from exc


def _sha256_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


async def _parse(bundle: Path) -> BundleMetadata:
    sha, size = _sha256_file(bundle)
    with tempfile.TemporaryDirectory(prefix="wildcat-bundle-") as tmp:
        repo = Path(tmp) / "repo"
        await cli.init_archive_repo(repo)
        await cli.run(
            "flatpak", "build-import-bundle", "--no-update-summary", repo, bundle, timeout=900
        )
        refs = [r.strip() for r in (await cli.ostree(repo, "refs")).stdout.splitlines() if r.strip()]
        app_refs = [r for r in refs if _REF_RE.match(r)]
        if len(app_refs) != 1:
            raise BundleParseError(
                "bundle must contain exactly one application ref "
                f"(found: {', '.join(refs) or 'none'}); runtimes are not supported"
            )
        ref = app_refs[0]
        app_id, arch, branch = _REF_RE.match(ref).groups()  # type: ignore[union-attr]
        if not _APP_ID_RE.match(app_id) or len(app_id) > 255:
            raise BundleParseError(f"invalid application ID {app_id!r}")
        commit = (await cli.ostree(repo, "rev-parse", ref)).stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{64}", commit):
            raise BundleParseError("could not resolve bundle commit")

        show = (await cli.ostree(repo, "show", commit)).stdout
        commit_date = _parse_commit_date(show)

        metadata_text = (await cli.ostree(repo, "cat", commit, "/metadata")).stdout
        kf = configparser.ConfigParser(interpolation=None, strict=False, delimiters=("=",))
        kf.optionxform = str  # type: ignore[assignment]
        kf.read_string(metadata_text)
        if kf.get("Application", "name", fallback=None) != app_id:
            raise BundleParseError("bundle metadata does not match its ref")
        runtime = kf.get("Application", "runtime", fallback=None)
        sdk = kf.get("Application", "sdk", fallback=None)
        permissions = _finish_args(kf)

        files = await _list_files(repo, commit)
        component = await _load_component(repo, commit, app_id, files)
        icon_data, icon_ext = await _load_icon(repo, commit, app_id, files)

    info = _component_info(component) if component is not None else {}
    version_name = info.get("version") or f"{branch}-{commit[:8]}"
    ts = int((commit_date or datetime.now(UTC)).timestamp())
    gpg_sig = hashlib.sha256(f"flatpak-unsigned:{app_id}".encode()).hexdigest()

    return BundleMetadata(
        package_name=app_id,
        version_code=ts,
        version_name=version_name[:128],
        permissions=permissions,
        features=[r for r in (runtime, sdk) if r],
        native_code=[arch],
        locales=info.get("locales", []),
        signer_sha256=gpg_sig,
        sha256=sha,
        size_bytes=size,
        app_name=info.get("name") or app_id.rsplit(".", 1)[-1],
        icon_data=icon_data,
        icon_extension=icon_ext,
        flatpak_ref=ref,
        ostree_commit=commit,
        arch=arch,
        branch=branch,
        runtime=runtime,
        sdk=sdk,
        summary=info.get("summary"),
        description=info.get("description"),
        license=info.get("license"),
        homepage=info.get("homepage"),
        bugtracker=info.get("bugtracker"),
        developer=info.get("developer"),
        categories=info.get("categories", []),
        commit_date=commit_date,
    )


def _parse_commit_date(show: str) -> datetime | None:
    m = re.search(r"^Date:\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ([+-]\d{4})", show, re.M)
    if not m:
        return None
    return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S %z").astimezone(UTC)


def _finish_args(kf: configparser.ConfigParser) -> list[str]:
    """Rebuild the sandbox permissions as ``finish-args`` strings."""
    out: list[str] = []
    mapping = {
        "shared": "--share",
        "sockets": "--socket",
        "devices": "--device",
        "features": "--allow",
        "filesystems": "--filesystem",
        "persistent": "--persist",
    }
    if kf.has_section("Context"):
        for key, flag in mapping.items():
            for value in kf.get("Context", key, fallback="").split(";"):
                value = value.strip()
                if not value:
                    continue
                if value.startswith("!"):
                    neg = {"--share": "--unshare", "--socket": "--nosocket",
                           "--device": "--nodevice", "--allow": "--disallow",
                           "--filesystem": "--nofilesystem"}.get(flag, flag)
                    out.append(f"{neg}={value[1:]}")
                else:
                    out.append(f"{flag}={value}")
    for section, prefix in (("Session Bus Policy", ""), ("System Bus Policy", "system-")):
        if kf.has_section(section):
            for name, policy in kf.items(section):
                policy = policy.strip()
                if policy in ("talk", "own", "see", "call"):
                    out.append(f"--{prefix}{policy}-name={name}")
    if kf.has_section("Environment"):
        for name, _value in kf.items("Environment"):
            out.append(f"--env={name}")
    return out


async def _list_files(repo: Path, commit: str) -> set[str]:
    try:
        res = await cli.ostree(repo, "ls", "-R", commit, "/files/share")
    except cli.CommandError:
        return set()
    paths: set[str] = set()
    for line in res.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) == 5 and parts[0].startswith("-"):
            paths.add(parts[4].split(" -> ")[0])
    return paths


async def _cat(repo: Path, commit: str, path: str, limit: int) -> bytes | None:
    try:
        data = (await cli.ostree(repo, "cat", commit, path)).stdout_bytes
    except cli.CommandError:
        return None
    return data if len(data) <= limit else None


async def _load_component(repo: Path, commit: str, app_id: str, files: set[str]) -> ET.Element | None:
    candidates = [
        (f"/files/share/app-info/xmls/{app_id}.xml.gz", True),
        (f"/files/share/swcatalog/xml/{app_id}.xml.gz", True),
        (f"/files/share/metainfo/{app_id}.metainfo.xml", False),
        (f"/files/share/metainfo/{app_id}.appdata.xml", False),
        (f"/files/share/appdata/{app_id}.appdata.xml", False),
    ]
    for path, gz in candidates:
        if path not in files:
            continue
        raw = await _cat(repo, commit, path, _XML_MAX)
        if raw is None:
            continue
        try:
            if gz:
                raw = gzip.decompress(raw)[:_XML_MAX]
            root = _safe_xml(raw)
        except Exception:  # noqa: BLE001
            continue
        if root.tag == "component":
            return root
        for comp in root.iter("component"):
            cid = (comp.findtext("id") or "").strip()
            if cid in (app_id, f"{app_id}.desktop"):
                return comp
    return None


def _safe_xml(raw: bytes) -> ET.Element:
    # MetaInfo never needs a DTD; refusing one rules out entity expansion.
    if b"<!DOCTYPE" in raw[:2048] or b"<!ENTITY" in raw:
        raise ValueError("DTDs are not allowed in MetaInfo")
    return ET.fromstring(raw)


async def _load_icon(repo: Path, commit: str, app_id: str, files: set[str]) -> tuple[bytes | None, str | None]:
    order = [
        f"/files/share/app-info/icons/flatpak/128x128@2/{app_id}.png",
        f"/files/share/app-info/icons/flatpak/128x128/{app_id}.png",
        f"/files/share/icons/hicolor/512x512/apps/{app_id}.png",
        f"/files/share/icons/hicolor/256x256/apps/{app_id}.png",
        f"/files/share/icons/hicolor/128x128/apps/{app_id}.png",
        f"/files/share/icons/hicolor/scalable/apps/{app_id}.svg",
    ]
    for path in order:
        if path in files:
            data = await _cat(repo, commit, path, _ICON_MAX)
            if data:
                return data, path.rsplit(".", 1)[-1]
    return None, None


def _text(el: ET.Element | None) -> str | None:
    if el is None:
        return None
    t = "".join(el.itertext()).strip()
    return re.sub(r"\s+", " ", t) or None


def _unlocalized(parent: ET.Element, tag: str) -> ET.Element | None:
    for el in parent.findall(tag):
        if el.get(_XML_LANG) in (None, "", "C", "en", "en_US"):
            return el
    return None


def _description_md(desc: ET.Element | None) -> str | None:
    if desc is None:
        return None
    chunks: list[str] = []
    for child in desc:
        if child.get(_XML_LANG) not in (None, "", "C"):
            continue
        if child.tag == "p":
            if t := _text(child):
                chunks.append(t)
        elif child.tag in ("ul", "ol"):
            items = []
            for i, li in enumerate(child.findall("li"), 1):
                if li.get(_XML_LANG) not in (None, "", "C"):
                    continue
                if t := _text(li):
                    items.append(f"{i}. {t}" if child.tag == "ol" else f"- {t}")
            if items:
                chunks.append("\n".join(items))
    return "\n\n".join(chunks) or None


def _component_info(comp: ET.Element) -> dict:
    info: dict = {
        "name": _text(_unlocalized(comp, "name")),
        "summary": _text(_unlocalized(comp, "summary")),
        "description": _description_md(comp.find("description")),
        "license": _text(comp.find("project_license")),
    }
    for url in comp.findall("url"):
        kind = url.get("type")
        if kind == "homepage":
            info["homepage"] = _text(url)
        elif kind == "bugtracker":
            info["bugtracker"] = _text(url)
    dev = comp.find("developer")
    info["developer"] = _text(dev.find("name")) if dev is not None else _text(comp.find("developer_name"))
    releases = comp.find("releases")
    if releases is not None:
        rel = releases.find("release")
        if rel is not None and rel.get("version"):
            info["version"] = rel.get("version")
    info["categories"] = [c.text.strip() for c in comp.findall("categories/category") if c.text]
    langs = {el.get(_XML_LANG) for el in comp.iter() if el.get(_XML_LANG)}
    info["locales"] = sorted(lang.replace("_", "-") for lang in langs if lang and lang != "C")[:200]
    for key in ("summary", "name"):
        if info.get(key):
            info[key] = info[key][:255]
    return info
