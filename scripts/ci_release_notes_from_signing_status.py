"""Build honest GitHub Release notes from signing-status markers.

Reads ``signing-status-*.json`` artifacts produced by
``release-frozen.yml`` and prints a single notes string to stdout.
Never claims notarized / Authenticode unless both macOS and Windows
markers report ``signed: true``. Always suitable for ``--prerelease``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from .ci_verify_frozen_archive import NATIVE_TARGETS, VERIFIED_SIGNATURE, verify_manifest
except ImportError:
    from ci_verify_frozen_archive import NATIVE_TARGETS, VERIFIED_SIGNATURE, verify_manifest


def verified_markers(root: Path, sha: str) -> list[dict]:
    markers = []
    for target, (_, arch) in NATIVE_TARGETS.items():
        name = f"{target}-{arch}"
        manifest = json.loads((root / f"manifest-{name}.json").read_text(encoding="utf-8"))
        marker = json.loads((root / f"signing-status-{name}.json").read_text(encoding="utf-8"))
        archive = root / f"krellbot-{name}.zip"
        digest = verify_manifest(archive, manifest, target=target, source=sha)
        if (
            marker.get("platform") != target
            or marker.get("arch") != arch
            or marker.get("sha") != sha
            or marker.get("sha256") != digest
            or marker.get("size") != manifest["size"]
            or marker.get("install_smoke") != "passed"
            or type(marker.get("signing_requested")) is not bool
            or type(marker.get("signed")) is not bool
        ):
            raise ValueError(f"Missing or mismatched native release evidence for {name}")

        expected_signed = marker["signing_requested"] and target in VERIFIED_SIGNATURE
        if marker["signed"] is not expected_signed or (
            expected_signed and marker.get("detail") != VERIFIED_SIGNATURE[target]
        ):
            raise ValueError(f"Required platform signature was not verified for {name}")
        markers.append(marker)
    if len({marker["signing_requested"] for marker in markers}) != 1:
        raise ValueError("Conflicting signing requests in native release evidence")
    return markers


def build_notes(*, root: Path, sha: str, verify_archives: bool = False) -> str:
    macos_signed = False
    windows_signed = False
    linux_policy = "checksum_pinned_policy"
    markers = verified_markers(root, sha) if verify_archives else [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(root.rglob("signing-status-*.json"))
    ]
    for data in markers:
        platform = data.get("platform")
        signed = data.get("signed") is True and data.get("sha") == sha
        detail = str(data.get("detail") or "")
        if platform == "macos" and signed:
            macos_signed = True
        elif platform == "windows" and signed:
            windows_signed = True
        elif platform == "linux":
            linux_policy = detail or linux_policy

    base = f"Checksum-pinned build from {sha}."
    if macos_signed and windows_signed:
        return (
            f"{base} macOS: Developer ID-signed, notarized, stapled Krellbot.app. "
            f"Windows: Authenticode-signed. Linux: checksum-pinned ({linux_policy}); "
            "no platform code signature. Still a prerelease — not GA."
        )
    return (
        f"{base} Unsigned checksum-pinned build. Not notarized. "
        "Not Authenticode-signed. SHA-256 is the integrity check."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("release-assets"),
        help="directory containing signing-status-*.json markers",
    )
    parser.add_argument("--sha", required=True, help="git commit SHA for the notes")
    parser.add_argument("--verify-archives", action="store_true")
    args = parser.parse_args(argv)
    sys.stdout.write(build_notes(root=args.root, sha=args.sha, verify_archives=args.verify_archives))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
