"""Source-checkout helpers for the native app; no distribution machinery."""

from __future__ import annotations

import os
import platform
import plistlib
import shutil
import subprocess
from pathlib import Path


def checkout_root() -> Path | None:
    # Support invocation from the checkout (including macos/) and editable installs.
    for start in (Path.cwd(), Path(__file__).resolve().parent):
        for root in (start, *start.parents):
            if ((root / "macos/scripts/make-app.sh").is_file()
                    and (root / "macos/Package.swift").is_file()
                    and (root / "pyproject.toml").is_file()):
                return root
    return None


def launch_app() -> int:
    if platform.system() != "Darwin":
        print("The native menu-bar app requires macOS.")
        return 1
    if shutil.which("swift") is None:
        print("Swift is missing. Install Apple's Command Line Tools with xcode-select --install, "
              "then retry vnr app.")
        return 1
    root = checkout_root()
    if root is None:
        print("Native sources not found. Run vnr app from the Voice-Native-Stuff source checkout.")
        return 1
    env = {**os.environ, "PRODUCT": "VNRApp"}
    env.setdefault("SIGN_IDENTITY", "VNR Dev")
    print(f"Building and launching VNRApp; signing identity: {env['SIGN_IDENTITY']}", flush=True)
    return subprocess.run(
        ["bash", str(root / "macos/scripts/make-app.sh"), "run"],
        cwd=root / "macos", env=env, check=False,
    ).returncode


def native_checks() -> list[tuple[str, bool]]:
    checks = [("Swift available (install with xcode-select --install)",
               shutil.which("swift") is not None)]
    root = checkout_root()
    if root is None:
        print("NOT FOUND  Native source checkout; run doctor from the repository.")
        checks.append(("Native source checkout", False))
        return checks
    app_name = os.environ.get("APP_NAME", "VoiceNativeResearch")
    app = root / "macos/dist" / f"{app_name}.app"
    if not app.exists():
        # Expected on first setup, before the documented launch command builds it.
        print(f"NOT BUILT  {app}\nBuild and launch with: SIGN_IDENTITY=\"VNR Dev\" uv run vnr app")
        return checks
    try:
        with (app / "Contents/Info.plist").open("rb") as stream:
            executable = plistlib.load(stream).get("CFBundleExecutable")
        valid = executable == "VNRApp" and (app / "Contents/MacOS/VNRApp").is_file()
    except (OSError, ValueError, plistlib.InvalidFileException):
        valid = False
    checks.append((f"VNRApp bundle built: {app}", valid))
    if not valid:
        print("Bundle is incomplete or belongs to a probe. Rebuild with vnr app.")
    codesign = shutil.which("codesign")
    if codesign is None:
        print("UNAVAILABLE  codesign; cannot inspect the bundle signature.")
        checks.append(("codesign available", False))
        return checks
    try:
        result = subprocess.run([codesign, "-dv", "--verbose=4", str(app)],
                                capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        print("UNAVAILABLE  Could not inspect the bundle signature.")
        checks.append(("Bundle signing metadata readable", False))
        return checks
    details = result.stdout + result.stderr
    authorities = [line.removeprefix("Authority=") for line in details.splitlines()
                   if line.startswith("Authority=")]
    if result.returncode:
        print("UNSIGNED / UNREADABLE  Rebuild with SIGN_IDENTITY=\"VNR Dev\" uv run vnr app")
        checks.append(("Bundle signing metadata readable", False))
    elif "Signature=adhoc" in details:
        print("SIGNED BY  ad-hoc (-); rebuilds can reset microphone permission. Use VNR Dev.")
    elif authorities and authorities[0] != "(unavailable)":
        print(f"SIGNED BY  {authorities[0]} (from codesign metadata)")
    else:
        print("UNAVAILABLE  Signing identity not reported by codesign in this environment.")
    print("Signature metadata does not establish microphone permission or certificate trust.")
    return checks
