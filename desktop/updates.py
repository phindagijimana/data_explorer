"""Phase 5 — "is there a newer release?" check plus an *assisted* installer.

A packaged app can't ``git pull`` like ``./hub update``, so it asks the GitHub
Releases API whether a newer tag exists. On top of that notification, this
module offers an **assisted, opt-in** update: with the user's explicit consent
(a button in the app), it downloads the correct installer for the running OS,
verifies its SHA-256 against the release's ``SHA256SUMS``, and hands it to the
OS to complete the install. It deliberately does *not* replace the running app
in place (that needs Sparkle/Squirrel-class infrastructure) — the user finishes
the normal install and relaunches.

Best-effort and fully non-fatal: any network/parse error yields "no update" for
the check, or a structured ``{'ok': False, 'error': ...}`` for a download.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import subprocess
import tempfile
import urllib.request
from typing import Callable, Dict, Optional, Tuple

logger = logging.getLogger("bidshub.desktop")

REPO = "phindagijimana/BIDSHub"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{REPO}/releases/latest"

# Installer asset names, keyed by ``platform.system()`` — must match the names
# the packaging/CI jobs upload to each release (see .github/workflows/
# desktop-build.yml and packaging/).
_ASSET_FOR_SYSTEM = {
    "Darwin": "BIDSHub.dmg",
    "Windows": "BIDSHub-Setup.exe",
    "Linux": "BIDSHub-x86_64.AppImage",
}
CHECKSUMS_ASSET = "SHA256SUMS"


def parse_version(text: str) -> Tuple[int, ...]:
    """'v3.2.10' / '3.2.10' -> (3, 2, 10). Non-numeric tails are ignored."""
    nums = re.findall(r"\d+", text or "")
    return tuple(int(n) for n in nums) or (0,)


def is_newer(latest: str, current: str) -> bool:
    """True if ``latest`` is a strictly higher version than ``current``."""
    a, b = parse_version(latest), parse_version(current)
    width = max(len(a), len(b))
    a += (0,) * (width - len(a))
    b += (0,) * (width - len(b))
    return a > b


def fetch_latest_release(timeout: float = 4.0) -> Optional[dict]:
    """Return the GitHub 'latest release' JSON, or None on any failure.

    Split out from :func:`check_for_update` so tests can stub the network.
    """
    req = urllib.request.Request(
        LATEST_RELEASE_URL,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "BIDSHub"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # offline, rate-limited, DNS, etc.
        logger.debug("update check failed: %s", exc)
        return None


def current_version() -> str:
    try:
        from src.bidshub_version import __version__
        return __version__
    except Exception:
        return "0"


def check_for_update(current: Optional[str] = None) -> Optional[dict]:
    """Return ``{'version','url','name'}`` if a newer release exists, else None."""
    current = current or current_version()
    data = fetch_latest_release()
    if not data:
        return None
    tag = data.get("tag_name") or data.get("name") or ""
    if tag and is_newer(tag, current):
        return {
            "version": tag,
            "url": data.get("html_url", f"https://github.com/{REPO}/releases"),
            "name": data.get("name") or tag,
            # Carried so the assisted installer can find the platform asset +
            # SHA256SUMS without a second API round-trip.
            "assets": data.get("assets", []),
        }
    return None


# --------------------------------------------------------------------------- #
# Assisted update — pure helpers (no network/GUI; unit-testable)
# --------------------------------------------------------------------------- #

def asset_name_for_platform(system: Optional[str] = None) -> Optional[str]:
    """Installer file name for this OS (e.g. 'BIDSHub.dmg'), or None if unknown."""
    return _ASSET_FOR_SYSTEM.get(system or platform.system())


def select_asset(assets, name: str) -> Optional[dict]:
    """Pick the release asset named ``name``; return ``{name,url,size}`` or None."""
    for asset in assets or []:
        if asset.get("name") == name:
            return {
                "name": asset.get("name"),
                "url": asset.get("browser_download_url"),
                "size": asset.get("size"),
            }
    return None


def parse_sha256sums(text: str) -> Dict[str, str]:
    """Parse a ``SHA256SUMS`` body into ``{basename: hexdigest}``.

    Tolerates the ``<hash>  ./path/to/file`` layout our release job emits and
    ignores blank/comment lines and malformed rows.
    """
    sums: Dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 64:
            sums[os.path.basename(parts[-1])] = parts[0].lower()
    return sums


def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    """Streaming SHA-256 of a file (constant memory for large installers)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_checksum(path: str, expected_hex: Optional[str]) -> bool:
    """True iff ``path`` hashes to ``expected_hex`` (case-insensitive)."""
    return bool(expected_hex) and sha256_file(path).lower() == expected_hex.lower()


# --------------------------------------------------------------------------- #
# Assisted update — network + OS side effects
# --------------------------------------------------------------------------- #

def _download_text(url: str, timeout: float = 10.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "BIDSHub"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def fetch_checksums(assets, timeout: float = 10.0) -> Dict[str, str]:
    """Download + parse the release's SHA256SUMS asset; {} on any failure."""
    asset = select_asset(assets, CHECKSUMS_ASSET)
    if not asset or not asset.get("url"):
        return {}
    try:
        return parse_sha256sums(_download_text(asset["url"], timeout=timeout))
    except Exception as exc:  # offline, 404, parse error
        logger.debug("checksum fetch failed: %s", exc)
        return {}


def download_file(
    url: str,
    dest: str,
    timeout: float = 30.0,
    progress: Optional[Callable[[int, int], None]] = None,
    chunk: int = 1 << 20,
) -> str:
    """Stream ``url`` to ``dest`` (via a ``.part`` temp), calling ``progress``.

    ``progress(bytes_read, total_bytes)`` is invoked per chunk; ``total`` is 0
    when the server omits Content-Length. Callback errors are swallowed so a
    flaky UI can't abort the download.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "BIDSHub"})
    part = f"{dest}.part"
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(part, "wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        read = 0
        while True:
            block = resp.read(chunk)
            if not block:
                break
            fh.write(block)
            read += len(block)
            if progress:
                try:
                    progress(read, total)
                except Exception:
                    pass
    os.replace(part, dest)
    return dest


def download_update(
    info: dict,
    dest_dir: Optional[str] = None,
    progress: Optional[Callable[[int, int], None]] = None,
    timeout: float = 30.0,
) -> dict:
    """Download this platform's installer for ``info`` and verify its checksum.

    ``info`` is a :func:`check_for_update` result (must carry ``assets``). On a
    checksum mismatch the file is discarded. When the release publishes no
    checksum for the asset, the download still succeeds over HTTPS but is marked
    ``verified=False`` so the caller can surface that.

    Returns ``{'ok', 'path', 'verified', 'error'}``.
    """
    name = asset_name_for_platform()
    if not name:
        return {"ok": False, "path": None, "verified": False,
                "error": f"No installer is published for this platform "
                         f"({platform.system()})."}

    asset = select_asset(info.get("assets"), name)
    if not asset or not asset.get("url"):
        return {"ok": False, "path": None, "verified": False,
                "error": f"This release has no {name} asset."}

    dest_dir = dest_dir or os.path.join(tempfile.gettempdir(), "bidshub-update")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, name)

    try:
        download_file(asset["url"], dest, timeout=timeout, progress=progress)
    except Exception as exc:
        logger.warning("update download failed: %s", exc)
        return {"ok": False, "path": None, "verified": False,
                "error": f"Download failed: {exc}"}

    expected = fetch_checksums(info.get("assets"), timeout=timeout).get(name)
    if expected is not None and not verify_checksum(dest, expected):
        try:
            os.remove(dest)
        except OSError:
            pass
        return {"ok": False, "path": None, "verified": False,
                "error": "Checksum verification failed — the download was discarded."}

    return {"ok": True, "path": dest, "verified": expected is not None, "error": None}


def open_installer(path: str, system: Optional[str] = None) -> bool:
    """Hand the downloaded installer to the OS. Returns True on a launch attempt.

    macOS mounts the .dmg (Finder shows the drag-to-Applications window);
    Windows runs the setup .exe; Linux reveals the AppImage in the file manager
    (there's nothing to "run" to install — the user places/executes it).
    """
    system = system or platform.system()
    try:
        if system == "Darwin":
            subprocess.Popen(["open", path])
        elif system == "Windows":
            os.startfile(path)  # type: ignore[attr-defined]  # Windows-only
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path) or "."])
        return True
    except Exception as exc:
        logger.warning("failed to open installer %s: %s", path, exc)
        return False
