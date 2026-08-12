import os
import subprocess
import sys
from pathlib import Path

import release_manager
from release_manager import ReleaseLayout


RUNTIME_ASSETS = {
    "templates/admin.html",
    "templates/preview.html",
    "templates/subscription.html",
    "static/dashboard.css",
    "static/dashboard.js",
    "static/subscription.css",
}


def test_runtime_manifest_covers_python_modules_and_runtime_assets():
    manifest = set(release_manager.read_runtime_manifest(Path("runtime-manifest.txt")))

    assert {path.name for path in Path().glob("*.py")} <= manifest
    assert RUNTIME_ASSETS <= manifest
    assert "LICENSE" in manifest
    assert not any(
        entry.startswith(("tests/", "docs/", ".codex/", ".git/"))
        or entry in {".env", "data.db", "autosub.log"}
        for entry in manifest
    )


def test_final_ci_has_locked_python_security_and_shell_gates():
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    for required in (
        'python-version: ["3.10", "3.12", "3.14"]',
        "--require-hashes -r requirements-dev.txt",
        "pytest --cov=.",
        "ruff check .",
        "pyright",
        "pip-audit -r requirements.txt --require-hashes",
        "bandit -c pyproject.toml -r . -x tests -ll",
        "shellcheck install.sh update.sh setup_nginx.sh",
        "bash -n install.sh update.sh setup_nginx.sh",
    ):
        assert required in workflow


def test_manifest_only_release_imports_entrypoint_and_finds_assets(tmp_path):
    root = tmp_path / "autosub-server"
    layout = ReleaseLayout(root)
    release = layout.prepare_release(Path(), Path("runtime-manifest.txt"), "smoke-a")
    shared = root / "shared"
    # Simulate stale flat-layout asset directories left by a v2 installation.
    # The active immutable release must never load templates or static files from them.
    (root / "templates").mkdir()
    (root / "templates" / "admin.html").write_text("stale", encoding="utf-8")
    (root / "static").mkdir()
    (root / "static" / "dashboard.css").write_text("stale", encoding="utf-8")
    (shared / ".env").write_text(
        "AUTOSUB_HOST=127.0.0.1\nAUTOSUB_ADMIN_PASSWORD=\nAUTOSUB_SECRET_KEY=test-only-secret\n",
        encoding="utf-8",
    )
    (shared / "config.json").write_text("{}\n", encoding="utf-8")

    environment = os.environ.copy()
    environment.update(
        {
            "AUTOSUB_ROOT": str(root),
            "AUTOSUB_APP_DIR": str(root),
            "AUTOSUB_SHARED_DIR": str(shared),
            "AUTOSUB_ENV": str(shared / ".env"),
            "AUTOSUB_CONFIG": str(shared / "config.json"),
            "AUTOSUB_DB": str(shared / "data.db"),
            "AUTOSUB_LOG": str(shared / "autosub.log"),
            "AUTOSUB_BACKUP_DIR": str(shared / "backups"),
        }
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import autosub_server; "
                "import dashboard; "
                "import subscription_representation; "
                "assert autosub_server.app is not None; "
                "release = __import__('pathlib').Path.cwd().resolve(); "
                "assert autosub_server.static_dir.resolve() == release / 'static'; "
                "assert dashboard.templates_dir.resolve() == release / 'templates'; "
                "assert subscription_representation.templates_dir.resolve() == release / 'templates'; "
                "assert autosub_server.subscription_css_path.resolve() == release / 'static' / 'subscription.css'; "
                "assert subscription_representation.templates.get_template('subscription.html')"
            ),
        ],
        cwd=release,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
