import os
import subprocess
import sys
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LINUX_ONLY = pytest.mark.skipif(os.name == "nt", reason="Bash entry points require Linux")


def _legacy_environment(tmp_path):
    legacy_directory = tmp_path / "legacy-flat-install"
    legacy_directory.mkdir()
    (legacy_directory / "autosub_server.py").write_text("APP = True\n", encoding="utf-8")
    (legacy_directory / "update.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")

    environment = os.environ.copy()
    environment.update(
        {
            "AUTOSUB_DEPLOY_PYTHON": sys.executable,
            "AUTOSUB_MIN_FREE_KB": "0",
            "AUTOSUB_ROOT": str(tmp_path / "autosub-root"),
            "AUTOSUB_SKIP_ROOT_CHECK": "1",
            "AUTOSUB_VERSION": "../unsafe-ref",
        }
    )
    return legacy_directory, environment


def _run_from_stdin(script_name, tmp_path):
    legacy_directory, environment = _legacy_environment(tmp_path)
    return subprocess.run(
        ["bash"],
        input=(REPOSITORY_ROOT / script_name).read_text(encoding="utf-8"),
        cwd=legacy_directory,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


@LINUX_ONLY
def test_install_from_stdin_does_not_treat_working_directory_as_source(tmp_path):
    completed = _run_from_stdin("install.sh", tmp_path)

    assert completed.returncode != 0
    assert "Unsafe AutoSub version/ref" in completed.stderr
    assert "BASH_SOURCE" not in completed.stderr


@LINUX_ONLY
def test_update_from_stdin_does_not_treat_working_directory_as_source(tmp_path):
    completed = _run_from_stdin("update.sh", tmp_path)

    assert completed.returncode != 0
    assert "AutoSub update failed: unsafe version/ref" in completed.stderr
    assert "release manager missing from source" not in completed.stderr
    assert "BASH_SOURCE" not in completed.stderr


@LINUX_ONLY
def test_installed_updater_ignores_leftover_legacy_entrypoint(tmp_path):
    legacy_directory, environment = _legacy_environment(tmp_path)
    updater = legacy_directory / "update.sh"
    updater.write_text(
        (REPOSITORY_ROOT / "update.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    completed = subprocess.run(
        ["bash", str(updater)],
        cwd=legacy_directory,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode != 0
    assert "AutoSub update failed: unsafe version/ref" in completed.stderr
    assert "release manager missing from source" not in completed.stderr
