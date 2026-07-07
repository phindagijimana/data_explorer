"""Tests for the desktop update checker (phase 5).

The network is stubbed, so these are deterministic and offline-safe.
"""

import hashlib

from desktop import updates


def test_parse_version_strips_prefix_and_text():
    assert updates.parse_version("v3.2.10") == (3, 2, 10)
    assert updates.parse_version("3.1.1") == (3, 1, 1)
    assert updates.parse_version("") == (0,)


def test_is_newer_handles_uneven_lengths():
    assert updates.is_newer("3.2.0", "3.1.1") is True
    assert updates.is_newer("v3.1.2", "3.1.1") is True
    assert updates.is_newer("3.1.1", "3.1.1") is False
    assert updates.is_newer("3.1.0", "3.1.1") is False
    assert updates.is_newer("3.2", "3.1.9") is True       # (3,2) > (3,1,9)


def test_check_for_update_returns_info_when_newer(monkeypatch):
    monkeypatch.setattr(updates, "fetch_latest_release", lambda timeout=4.0: {
        "tag_name": "v9.9.9",
        "html_url": "https://github.com/phindagijimana/BIDSHub/releases/tag/v9.9.9",
        "name": "BIDSHub 9.9.9",
    })
    info = updates.check_for_update(current="3.1.1")
    assert info is not None
    assert info["version"] == "v9.9.9"
    assert "9.9.9" in info["url"]


def test_check_for_update_none_when_current_is_latest(monkeypatch):
    monkeypatch.setattr(updates, "fetch_latest_release", lambda timeout=4.0: {
        "tag_name": "v3.1.1", "html_url": "x", "name": "y",
    })
    assert updates.check_for_update(current="3.1.1") is None


def test_check_for_update_none_when_offline(monkeypatch):
    monkeypatch.setattr(updates, "fetch_latest_release", lambda timeout=4.0: None)
    assert updates.check_for_update(current="3.1.1") is None


def test_format_update_banner_mentions_versions_and_link():
    """The in-app banner (app.py) renders the new version, current version, link."""
    from app import format_update_banner

    info = {
        "version": "v3.2.0",
        "url": "https://github.com/phindagijimana/BIDSHub/releases/tag/v3.2.0",
        "name": "BIDSHub 3.2.0",
    }
    msg = format_update_banner(info, "3.1.4")
    assert "v3.2.0" in msg            # the available version
    assert "3.1.4" in msg             # the current version
    assert info["url"] in msg         # a working download link
    assert "reinstall" in msg.lower()


def test_check_for_update_carries_assets(monkeypatch):
    """The assisted installer needs the release's assets in the result."""
    monkeypatch.setattr(updates, "fetch_latest_release", lambda timeout=4.0: {
        "tag_name": "v9.9.9", "html_url": "x", "name": "y",
        "assets": [{"name": "BIDSHub.dmg", "browser_download_url": "http://x/dmg"}],
    })
    info = updates.check_for_update(current="3.1.1")
    assert info["assets"][0]["name"] == "BIDSHub.dmg"


# ---- assisted-update helpers ---------------------------------------------- #

def test_asset_name_for_platform_known_and_unknown():
    assert updates.asset_name_for_platform("Darwin") == "BIDSHub.dmg"
    assert updates.asset_name_for_platform("Windows") == "BIDSHub-Setup.exe"
    assert updates.asset_name_for_platform("Linux") == "BIDSHub-x86_64.AppImage"
    assert updates.asset_name_for_platform("Plan9") is None


def test_select_asset_matches_by_name():
    assets = [
        {"name": "BIDSHub.dmg", "browser_download_url": "http://x/dmg", "size": 5},
        {"name": "SHA256SUMS", "browser_download_url": "http://x/sums", "size": 1},
    ]
    got = updates.select_asset(assets, "BIDSHub.dmg")
    assert got["url"] == "http://x/dmg" and got["size"] == 5
    assert updates.select_asset(assets, "nope.exe") is None
    assert updates.select_asset(None, "BIDSHub.dmg") is None


def test_parse_sha256sums_ignores_noise():
    dmg_hash = "aa" * 32
    exe_hash = "bb" * 32
    body = "\n".join([
        "# a comment",
        f"{dmg_hash}  ./BIDSHub-macos-arm64/BIDSHub.dmg",
        "",
        f"{exe_hash}  ./BIDSHub-windows-x86_64/BIDSHub-Setup.exe",
        "garbage line",
    ])
    sums = updates.parse_sha256sums(body)
    assert sums["BIDSHub.dmg"] == dmg_hash
    assert sums["BIDSHub-Setup.exe"] == exe_hash
    assert "garbage" not in sums


def test_verify_checksum_roundtrip(tmp_path):
    f = tmp_path / "BIDSHub.dmg"
    f.write_bytes(b"hello world")
    digest = hashlib.sha256(b"hello world").hexdigest()
    assert updates.verify_checksum(str(f), digest) is True
    assert updates.verify_checksum(str(f), digest.upper()) is True   # case-insensitive
    assert updates.verify_checksum(str(f), "00" * 32) is False
    assert updates.verify_checksum(str(f), None) is False


def _dmg_info(url="http://x/dmg"):
    return {"version": "v9.9.9", "url": "http://rel",
            "assets": [{"name": "BIDSHub.dmg", "browser_download_url": url}]}


def test_download_update_ok_and_verified(monkeypatch, tmp_path):
    monkeypatch.setattr(updates.platform, "system", lambda: "Darwin")

    def fake_download(url, dest, **kw):
        with open(dest, "wb") as fh:
            fh.write(b"abc")
        return dest

    monkeypatch.setattr(updates, "download_file", fake_download)
    monkeypatch.setattr(updates, "fetch_checksums",
                        lambda assets, **kw: {"BIDSHub.dmg": hashlib.sha256(b"abc").hexdigest()})

    res = updates.download_update(_dmg_info(), dest_dir=str(tmp_path))
    assert res["ok"] is True and res["verified"] is True
    assert res["path"].endswith("BIDSHub.dmg")


def test_download_update_rejects_bad_checksum(monkeypatch, tmp_path):
    monkeypatch.setattr(updates.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(updates, "download_file",
                        lambda url, dest, **kw: open(dest, "wb").write(b"abc") or dest)
    monkeypatch.setattr(updates, "fetch_checksums", lambda assets, **kw: {"BIDSHub.dmg": "00" * 32})

    res = updates.download_update(_dmg_info(), dest_dir=str(tmp_path))
    assert res["ok"] is False and "verification failed" in res["error"].lower()
    assert not (tmp_path / "BIDSHub.dmg").exists()   # bad file discarded


def test_download_update_ok_when_no_checksum_published(monkeypatch, tmp_path):
    monkeypatch.setattr(updates.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(updates, "download_file",
                        lambda url, dest, **kw: open(dest, "wb").write(b"abc") or dest)
    monkeypatch.setattr(updates, "fetch_checksums", lambda assets, **kw: {})

    res = updates.download_update(_dmg_info(), dest_dir=str(tmp_path))
    assert res["ok"] is True and res["verified"] is False   # HTTPS-only, unverified


def test_download_update_no_asset_for_platform(monkeypatch, tmp_path):
    monkeypatch.setattr(updates.platform, "system", lambda: "Darwin")
    info = {"version": "v9.9.9", "url": "http://rel", "assets": []}
    res = updates.download_update(info, dest_dir=str(tmp_path))
    assert res["ok"] is False and "no bidshub.dmg" in res["error"].lower()


def test_download_update_unsupported_platform(monkeypatch, tmp_path):
    monkeypatch.setattr(updates.platform, "system", lambda: "Plan9")
    res = updates.download_update(_dmg_info(), dest_dir=str(tmp_path))
    assert res["ok"] is False and "platform" in res["error"].lower()


def test_open_installer_dispatches_per_os(monkeypatch):
    calls = {}
    monkeypatch.setattr(updates.subprocess, "Popen", lambda argv: calls.setdefault("argv", argv))
    assert updates.open_installer("/tmp/BIDSHub.dmg", system="Darwin") is True
    assert calls["argv"][0] == "open" and calls["argv"][1].endswith("BIDSHub.dmg")

    calls.clear()
    assert updates.open_installer("/tmp/BIDSHub-x86_64.AppImage", system="Linux") is True
    assert calls["argv"][0] == "xdg-open"
