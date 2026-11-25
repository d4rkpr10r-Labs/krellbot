"""Verify a native frozen archive and exercise an isolated installed copy."""

from __future__ import annotations

import argparse
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from .release_archive import _sha256_of
except ImportError:
    from release_archive import _sha256_of

NATIVE_TARGETS = {
    "linux": ("Linux", "x86_64"),
    "macos": ("Darwin", "arm64"),
    "windows": ("Windows", "x86_64"),
}
VERIFIED_SIGNATURE = {
    "macos": "developer_id_notarized_stapled_app",
    "windows": "authenticode",
}
SIGNING_INPUTS = {
    "macos": (
        "APPLE_DEVELOPER_ID_P12_BASE64",
        "APPLE_DEVELOPER_ID_P12_PASSWORD",
        "APPLE_CODESIGN_IDENTITY",
        "APPLE_TEAM_ID",
        "APPLE_API_KEY_P8_BASE64",
        "APPLE_API_KEY_ID",
        "APPLE_API_ISSUER_ID",
    ),
    "windows": ("WINDOWS_CODESIGN_P12_BASE64", "WINDOWS_CODESIGN_P12_PASSWORD"),
}
ROOT = Path(__file__).resolve().parent.parent


def require_signing_inputs(target: str) -> None:
    missing = [name for name in SIGNING_INPUTS[target] if not os.environ.get(name, "").strip()]
    if missing:
        raise ValueError(
            f"{target} signing requested but required secrets are missing: {', '.join(missing)}. See docs/signing.md."
        )


def verify_manifest(archive: Path, manifest: dict, *, target: str, source: str) -> str:
    _, arch = NATIVE_TARGETS[target]
    digest = _sha256_of(archive)
    expected = {
        "filename": f"krellbot-{target}-{arch}.zip",
        "size": archive.stat().st_size,
        "sha256": digest,
        "os": target,
        "arch": arch,
        "source_commit": source,
    }
    for key, value in expected.items():
        if manifest.get(key) != value or (key == "filename" and archive.name != value):
            raise ValueError(f"Archive manifest mismatch for {key}")
    return digest


def run(*command: str | Path, env: dict | None = None, cwd: Path | None = None) -> None:
    subprocess.run(command, check=True, timeout=180, env=env, cwd=cwd)


def verify_native_archive(args: argparse.Namespace) -> None:
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    digest = verify_manifest(args.archive, manifest, target=args.platform, source=args.sha)
    system, arch = NATIVE_TARGETS[args.platform]
    machine = {"amd64": "x86_64", "aarch64": "arm64"}.get(platform.machine().lower(), platform.machine().lower())
    if (platform.system(), machine) != (system, arch):
        raise ValueError(f"Unexpected native runner: {platform.system()} {machine}")
    detail = "checksum_pinned_policy" if args.platform == "linux" else "unsigned_preview"
    install_method = {
        "linux": "bundled_linux_installer",
        "macos": "isolated_archive_copy",
        "windows": "bundled_windows_installer",
    }[args.platform]
    signed = args.sign_requested and args.platform in VERIFIED_SIGNATURE
    with tempfile.TemporaryDirectory(prefix="krellbot release ") as directory:
        scratch = Path(directory)
        extracted = scratch / "download"
        if args.platform == "macos" and signed:
            run("ditto", "-x", "-k", args.archive, extracted)
        elif args.platform in {"linux", "macos"}:
            run("unzip", "-q", args.archive, "-d", extracted)
        else:
            shutil.unpack_archive(args.archive, extracted)
        executable = "krellbot.exe" if args.platform == "windows" else "krellbot"
        relative = Path(executable)
        if signed and args.platform == "macos":
            app = extracted / "Krellbot.app"
            relative = Path("Krellbot.app", "Contents", "MacOS", executable)
            info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
            expected_metadata = {
                "CFBundleIdentifier": "dev.krellbot.workstation",
                "CFBundleExecutable": executable,
                "CFBundlePackageType": "APPL",
                "CFBundleShortVersionString": manifest["version"].removeprefix("v"),
            }
            if any(info.get(key) != value for key, value in expected_metadata.items()):
                raise ValueError("Archived macOS bundle metadata mismatch")
            run(
                "codesign",
                "--verify",
                "--deep",
                "--strict",
                "-R",
                "anchor apple generic and certificate leaf[field.1.2.840.113635.100.6.1.13] exists",
                app,
            )
            run("xcrun", "stapler", "validate", app)
            run("spctl", "--assess", "--type", "execute", app)
        if signed and args.platform == "windows":
            run(args.signtool, "verify", "/pa", "/all", "/tw", extracted / relative)
        if signed:
            detail = VERIFIED_SIGNATURE[args.platform]

        installed = scratch / "installed"
        previous = scratch / "installed.old"
        home = scratch / "data"
        home.mkdir()
        sentinel = home / "owner-state"
        sentinel.write_text("isolated owner state", encoding="utf-8")

        def install() -> None:
            if args.platform == "linux":
                run(
                    "bash",
                    extracted / "install-krellbot.sh",
                    installed,
                    env={
                        **os.environ,
                        "HOME": str(scratch),
                        "XDG_DATA_HOME": str(scratch / "local"),
                        "XDG_CONFIG_HOME": str(scratch / "config"),
                    },
                    cwd=scratch,
                )
            else:
                shutil.copytree(extracted, installed, symlinks=True)

        install()
        binary = installed / relative
        binary_digest = _sha256_of(extracted / relative)
        marker = installed / "prior-program"
        marker.touch()
        if args.platform != "linux":
            installed.rename(previous)
        install()
        if marker.exists() or _sha256_of(binary) != binary_digest:
            raise ValueError("Candidate upgrade did not install the verified executable")
        installed.rename(scratch / "candidate")
        previous.rename(installed)
        if not marker.exists() or _sha256_of(binary) != binary_digest:
            raise ValueError("Previous program could not be restored")
        installed.rename(previous)
        (scratch / "candidate").rename(installed)
        if sentinel.read_text(encoding="utf-8") != "isolated owner state":
            raise ValueError("Install, upgrade, or rollback altered the isolated profile")

        # Keep the existing paper smoke as the pack reproduction and first-launch
        # check. Its staging happens in an isolated directory outside the checkout.
        log = scratch / "paper-smoke.log"
        try:
            run(
                sys.executable,
                ROOT / "scripts/ci_smoke_frozen_paper.py",
                "--binary",
                binary,
                "--workdir",
                scratch / "smoke",
                "--home",
                scratch / "smoke-home",
                "--pack",
                ROOT / "docs/example-pack.json",
                "--log",
                log,
                cwd=scratch,
            )
        finally:
            if log.exists():
                shutil.copyfile(log, args.output.with_suffix(".log"))

        if args.browser_evidence:
            run("node", ROOT / "scripts/ci_smoke_installed_preview.mjs", extracted, args.browser_evidence)

    args.output.write_text(
        json.dumps(
            {
                "platform": args.platform,
                "arch": arch,
                "sha": args.sha,
                "sha256": digest,
                "size": manifest["size"],
                "signed": signed,
                "signing_requested": args.sign_requested,
                "detail": detail,
                "install_smoke": "passed",
                "browser_smoke": "passed" if args.browser_evidence else "not_run",
                "warning_behavior": "manual_native_check_required",
                "install_method": install_method,
                "executable_sha256": binary_digest,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=NATIVE_TARGETS, required=True)
    parser.add_argument("--require-signing-inputs", action="store_true")
    parser.add_argument("--sign-requested", action="store_true")
    parser.add_argument("--signtool", default="signtool")
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--sha")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--browser-evidence", type=Path)
    args = parser.parse_args()
    try:
        if args.require_signing_inputs:
            require_signing_inputs(args.platform)
        else:
            verify_native_archive(args)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
