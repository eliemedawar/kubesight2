"""Tests for the mobile-binary signature probe.

The probe decides whether a stored APK/AAB/IPA still carries a code signature,
which is what stops a SafeCore-shielded (signature-stripped) binary from being
published. It reads the zip container only — no keys, no platform tooling.
"""

from __future__ import annotations

import struct
import zipfile
from pathlib import Path

from api.services import binary_signature as sig


def _zip(path: Path, names) -> str:
    with zipfile.ZipFile(path, "w") as zf:
        for name in names:
            zf.writestr(name, b"payload")
    return str(path)


def _add_apk_signing_block(path: Path) -> None:
    """Splice an APK Signing Block in ahead of the central directory.

    This is where a v2/v3 signature actually lives. Inserting after all local
    entries and before the central directory leaves every recorded local-header
    offset valid, which is exactly why the real format can do this — the file
    still reads as an ordinary zip afterwards.
    """
    data = path.read_bytes()
    idx = data.rfind(b"PK\x05\x06")
    (cd_offset,) = struct.unpack_from("<I", data, idx + 16)

    block = b"\x00" * 24 + b"APK Sig Block 42"
    patched = data[:cd_offset] + block + data[cd_offset:]

    new_idx = patched.rfind(b"PK\x05\x06")
    patched = (
        patched[: new_idx + 16]
        + struct.pack("<I", cd_offset + len(block))
        + patched[new_idx + 20 :]
    )
    path.write_bytes(patched)


# ---------------------------------------------------------------------------
# Android
# ---------------------------------------------------------------------------

def test_aab_with_jar_signature_is_signed(tmp_path):
    p = tmp_path / "app.aab"
    _zip(p, ["base/manifest/AndroidManifest.xml", "META-INF/MANIFEST.MF", "META-INF/UPLOAD.RSA"])
    assert sig.detect(str(p), "aab") == sig.SIGNED


def test_aab_stripped_of_signature_is_unsigned(tmp_path):
    """What SafeCore hands back: the entries survive, the signature block does not."""
    p = tmp_path / "app.aab"
    _zip(p, ["base/manifest/AndroidManifest.xml", "META-INF/MANIFEST.MF"])
    assert sig.detect(str(p), "aab") == sig.UNSIGNED


def test_apk_v1_signature_is_signed(tmp_path):
    p = tmp_path / "app.apk"
    _zip(p, ["AndroidManifest.xml", "META-INF/CERT.RSA", "META-INF/CERT.SF"])
    assert sig.detect(str(p), "apk") == sig.SIGNED


def test_apk_v2_only_signature_is_signed(tmp_path):
    """A v2/v3-only APK has no META-INF/*.RSA at all — the JAR check alone
    would wrongly call it unsigned and block a legitimate publish."""
    p = tmp_path / "app.apk"
    _zip(p, ["AndroidManifest.xml", "classes.dex"])
    assert sig.detect(str(p), "apk") == sig.UNSIGNED  # before the block exists
    _add_apk_signing_block(p)
    assert sig.detect(str(p), "apk") == sig.SIGNED
    # Still a readable zip after splicing, as the real format requires.
    with zipfile.ZipFile(p) as zf:
        assert "classes.dex" in zf.namelist()


def test_apk_unsigned_is_unsigned(tmp_path):
    p = tmp_path / "app.apk"
    _zip(p, ["AndroidManifest.xml", "classes.dex"])
    assert sig.detect(str(p), "apk") == sig.UNSIGNED


def test_aab_ignores_apk_signing_block(tmp_path):
    """App bundles are JAR-signed only; a stray block must not read as signed."""
    p = tmp_path / "app.aab"
    _zip(p, ["base/manifest/AndroidManifest.xml"])
    _add_apk_signing_block(p)
    assert sig.detect(str(p), "aab") == sig.UNSIGNED


# ---------------------------------------------------------------------------
# iOS
# ---------------------------------------------------------------------------

def test_ipa_with_code_signature_is_signed(tmp_path):
    p = tmp_path / "app.ipa"
    _zip(p, ["Payload/POS.app/Info.plist", "Payload/POS.app/_CodeSignature/CodeResources"])
    assert sig.detect(str(p), "ipa") == sig.SIGNED


def test_ipa_stripped_of_signature_is_unsigned(tmp_path):
    p = tmp_path / "app.ipa"
    _zip(p, ["Payload/POS.app/Info.plist", "Payload/POS.app/POS"])
    assert sig.detect(str(p), "ipa") == sig.UNSIGNED


def test_ipa_nested_framework_signature_does_not_count(tmp_path):
    """Only the top-level .app bundle's signature counts — a signed embedded
    framework inside an otherwise stripped app must not read as signed."""
    p = tmp_path / "app.ipa"
    _zip(
        p,
        [
            "Payload/POS.app/Info.plist",
            "Payload/POS.app/Frameworks/Core.framework/_CodeSignature/CodeResources",
        ],
    )
    assert sig.detect(str(p), "ipa") == sig.UNSIGNED


# ---------------------------------------------------------------------------
# Unreadable / unknown
# ---------------------------------------------------------------------------

def test_non_zip_is_unknown(tmp_path):
    p = tmp_path / "app.aab"
    p.write_bytes(b"not a zip at all")
    assert sig.detect(str(p), "aab") == sig.UNKNOWN


def test_missing_file_is_unknown(tmp_path):
    assert sig.detect(str(tmp_path / "nope.apk"), "apk") == sig.UNKNOWN


def test_unrecognised_artifact_type_is_unknown(tmp_path):
    p = tmp_path / "app.zip"
    _zip(p, ["a"])
    assert sig.detect(str(p), "") == sig.UNKNOWN
    assert sig.detect(str(p), "exe") == sig.UNKNOWN


def test_detect_safe_swallows_errors(monkeypatch, tmp_path):
    def boom(*_a, **_k):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(sig, "detect", boom)
    assert sig.detect_safe(str(tmp_path / "x.apk"), "apk") == sig.UNKNOWN
