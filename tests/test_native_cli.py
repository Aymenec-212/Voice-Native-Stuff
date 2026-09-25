"""Native launch/diagnostics without rebuilding Swift or changing Keychain state."""

import plistlib
import subprocess

import pytest

from vnr.cli import native
from vnr.cli.main import main


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    (tmp_path / "macos/scripts").mkdir(parents=True)
    for name in ("macos/scripts/make-app.sh", "macos/Package.swift", "pyproject.toml"):
        (tmp_path / name).touch()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(native.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(native.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.delenv("APP_NAME", raising=False)
    return tmp_path


def bundle(root, executable="VNRApp"):
    app = root / "macos/dist/VoiceNativeResearch.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/MacOS" / executable).touch()
    (app / "Contents/Info.plist").write_bytes(
        plistlib.dumps({"CFBundleExecutable": executable}))
    return app


@pytest.mark.parametrize("identity", [None, "Another Local Identity", "-"])
def test_app_uses_existing_script_and_honours_identity(checkout, monkeypatch, identity):
    if identity is None:
        monkeypatch.delenv("SIGN_IDENTITY", raising=False)
    else:
        monkeypatch.setenv("SIGN_IDENTITY", identity)
    monkeypatch.setenv("PRODUCT", "VNRProbe")
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 7)

    monkeypatch.setattr(native.subprocess, "run", run)
    assert main(["app"]) == 7
    args, options = calls[0]
    assert args == ["bash", str(checkout / "macos/scripts/make-app.sh"), "run"]
    assert options["cwd"] == checkout / "macos"
    assert options["env"]["SIGN_IDENTITY"] == (identity if identity is not None else "VNR Dev")
    assert options["env"]["PRODUCT"] == "VNRApp"
    assert len(calls) == 1  # no security find-identity gate


def test_missing_swift_has_install_guidance(checkout, monkeypatch, capsys):
    monkeypatch.setattr(native.shutil, "which", lambda _: None)
    assert native.launch_app() == 1
    assert "xcode-select --install" in capsys.readouterr().out


def test_app_requires_macos(checkout, monkeypatch, capsys):
    monkeypatch.setattr(native.platform, "system", lambda: "Linux")
    assert native.launch_app() == 1
    assert "requires macOS" in capsys.readouterr().out


def test_wheel_outside_checkout_has_clear_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(native.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(native.shutil, "which", lambda _: "/usr/bin/swift")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(native, "__file__", str(tmp_path / "site-packages/vnr/cli/native.py"))
    assert native.launch_app() == 1
    assert "source checkout" in capsys.readouterr().out


def test_finds_checkout_from_macos_directory(checkout, monkeypatch):
    monkeypatch.chdir(checkout / "macos")
    assert native.checkout_root() == checkout


def test_first_setup_reports_unbuilt_without_failing_preflight(checkout, capsys):
    assert all(ok for _, ok in native.native_checks())
    assert "NOT BUILT" in capsys.readouterr().out


@pytest.mark.parametrize("metadata, status, expected", [
    ("Authority=VNR Dev\n", 0, "SIGNED BY  VNR Dev"),
    ("Signature=adhoc\n", 0, "SIGNED BY  ad-hoc"),
    ("code object is not signed at all", 1, "UNSIGNED / UNREADABLE"),
])
def test_doctor_reports_actual_signature(checkout, monkeypatch, capsys, metadata, status, expected):
    app = bundle(checkout)
    # A stale script record must not override the actual built signature.
    (app.parent / ".sign-identity").write_text("Wrong Identity")

    def run(args, **kwargs):
        assert args == ["/usr/bin/codesign", "-dv", "--verbose=4", str(app)]
        return subprocess.CompletedProcess(args, status, "", metadata)

    monkeypatch.setattr(native.subprocess, "run", run)
    checks = native.native_checks()
    assert all(ok for _, ok in checks) == (status == 0)
    output = capsys.readouterr().out
    assert expected in output
    assert "Wrong Identity" not in output


def test_probe_bundle_does_not_count_as_built_app(checkout, monkeypatch, capsys):
    bundle(checkout, "VNRProbe")
    monkeypatch.setattr(native.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "Authority=VNR Dev"))
    assert not all(ok for _, ok in native.native_checks())
    assert "belongs to a probe" in capsys.readouterr().out


def test_doctor_timeout_is_reported(checkout, monkeypatch, capsys):
    bundle(checkout)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("codesign", 10)

    monkeypatch.setattr(native.subprocess, "run", timeout)
    assert not all(ok for _, ok in native.native_checks())
    assert "Could not inspect" in capsys.readouterr().out


def test_text_doctor_does_not_check_native(monkeypatch):
    from vnr.cli import main as cli
    from vnr.config import Settings

    settings = Settings.load(env={"NEBIUS_API_KEY": "test", "TAVILY_API_KEY": "test"})
    monkeypatch.setattr(cli.Settings, "load", lambda: settings)

    def unexpected():
        raise AssertionError("Native tooling must stay optional for text research")

    monkeypatch.setattr(native, "native_checks", unexpected)
    assert cli.main(["doctor", "--text"]) == 0
