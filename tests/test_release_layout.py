import io
import os
import sqlite3
import tarfile
from pathlib import Path

import pytest

import release_manager
import runtime_paths
from release_manager import (
    ActivationPhase,
    ReleaseError,
    ReleaseLayout,
    migrate_legacy_persistent,
    read_health_port,
    restore_database_backup,
)


class MemoryLinks:
    def __init__(self):
        self.targets = {}

    def is_link(self, path):
        return Path(path) in self.targets

    def read(self, path):
        return self.targets[Path(path)]

    def create(self, target, path):
        self.targets[Path(path)] = Path(target)

    def replace(self, source, destination):
        self.targets[Path(destination)] = self.targets.pop(Path(source))

    def unlink(self, path):
        del self.targets[Path(path)]


def _write_release(layout: ReleaseLayout, name: str) -> Path:
    path = layout.release_path(name)
    path.mkdir(parents=True)
    (path / release_manager.RELEASE_MARKER).write_text(name, encoding="utf-8")
    return path


def _source(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "autosub_server.py").write_text("APP = True\n", encoding="utf-8")
    (source / "requirements.txt").write_text("", encoding="utf-8")
    manifest = source / "runtime-manifest.txt"
    manifest.write_text(
        "autosub_server.py\nrequirements.txt\nruntime-manifest.txt\n",
        encoding="utf-8",
    )
    return source, manifest


def test_release_preparation_is_separate_and_failure_cleans_staging(tmp_path):
    layout = ReleaseLayout(tmp_path / "autosub-server")
    source, manifest = _source(tmp_path)
    prepared = layout.prepare_release(source, manifest, "release-a")

    assert prepared == layout.releases / "release-a"
    assert (prepared / release_manager.RELEASE_MARKER).is_file()
    assert not list(layout.releases.glob(".staging-*"))

    def fail(_release):
        raise RuntimeError("validation failed")

    with pytest.raises(RuntimeError, match="validation failed"):
        layout.prepare_release(source, manifest, "release-b", validate=fail)

    assert not (layout.releases / "release-b").exists()
    assert not list(layout.releases.glob(".staging-*"))


def test_legacy_venv_uses_exact_release_requirements_lock(tmp_path, monkeypatch):
    release = tmp_path / "legacy-release"
    release.mkdir()
    requirements = release / "requirements.txt"
    requirements.write_text("fastapi\n", encoding="utf-8")
    lock = tmp_path / "requirements-lock.txt"
    locked_content = "fastapi==1.2.3 --hash=sha256:" + "a" * 64 + "\n"
    lock.write_text(locked_content, encoding="utf-8")
    calls = []

    def record(command, **kwargs):
        calls.append((command, kwargs))

    monkeypatch.setattr(release_manager.subprocess, "run", record)

    release_manager.prepare_release_venv(
        release,
        "python3",
        tmp_path / "autosub-server",
        requirements_lock=lock,
    )

    assert requirements.read_text(encoding="utf-8") == locked_content
    pip_command = calls[1][0]
    assert "--require-hashes" in pip_command
    assert pip_command[-1] == str(requirements)


@pytest.mark.parametrize(
    "name", ["../evil", "/absolute/path", "nested/../../evil", "back\\slash", ".staging-x"]
)
def test_malicious_release_names_are_rejected(tmp_path, name):
    with pytest.raises(ReleaseError, match="unsafe release name"):
        ReleaseLayout(tmp_path).release_path(name)


@pytest.mark.parametrize("entry", ["../evil", "/absolute/path", "nested/../../evil", "back\\slash"])
def test_manifest_path_traversal_is_rejected(tmp_path, entry):
    manifest = tmp_path / "manifest.txt"
    manifest.write_text(f"autosub_server.py\n{entry}\n", encoding="utf-8")
    with pytest.raises(ReleaseError, match="unsafe manifest entry"):
        release_manager.read_runtime_manifest(manifest)


def test_atomic_current_switch_is_relative_and_validated(tmp_path):
    links = MemoryLinks()
    layout = ReleaseLayout(tmp_path / "autosub-server", links=links)
    layout.initialize()
    _write_release(layout, "release-a")
    _write_release(layout, "release-b")

    layout.atomic_switch("release-a")
    assert links.is_link(layout.current)
    assert links.read(layout.current) == Path("releases") / "release-a"
    layout.atomic_switch("release-b")

    assert layout.current_release() == "release-b"
    assert not list(layout.root.glob(".current-new-*"))


def test_current_rejects_broken_and_external_targets(tmp_path):
    links = MemoryLinks()
    layout = ReleaseLayout(tmp_path / "autosub-server", links=links)
    layout.initialize()
    assert layout.current_release(required=False) is None

    links.create(Path("releases") / "missing", layout.current)
    with pytest.raises(FileNotFoundError):
        layout.current_release()
    links.unlink(layout.current)

    external = tmp_path / "external"
    external.mkdir()
    (external / release_manager.RELEASE_MARKER).write_text("external", encoding="utf-8")
    links.create(external, layout.current)
    with pytest.raises(ReleaseError, match="outside releases"):
        layout.current_release()


def test_cleanup_and_retention_only_remove_marked_safe_directories(tmp_path):
    layout = ReleaseLayout(tmp_path / "autosub-server", links=MemoryLinks())
    layout.initialize()
    for name in ("release-a", "release-b", "release-c", "release-d"):
        _write_release(layout, name)
        os.utime(layout.release_path(name), None)
    layout.atomic_switch("release-d")
    abandoned = layout.releases / ".staging-release-e-token"
    abandoned.mkdir()
    (abandoned / release_manager.STAGING_MARKER).write_text("release-e", encoding="utf-8")
    unknown = layout.releases / ".staging-user-data"
    unknown.mkdir()
    source = layout.releases / ".source-abandoned"
    source.mkdir()
    (source / release_manager.SOURCE_MARKER).write_text("", encoding="utf-8")

    assert set(layout.cleanup_staging()) == {abandoned.name, source.name}
    removed = layout.retain(keep=2, previous="release-c")

    assert layout.current_release() == "release-d"
    assert layout.release_path("release-c").exists()
    assert unknown.exists()
    assert set(removed) == {"release-a", "release-b"}


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics require Linux")
def test_os_link_backend_performs_real_atomic_switch(tmp_path):
    layout = ReleaseLayout(tmp_path / "autosub-server")
    layout.initialize()
    _write_release(layout, "release-a")

    layout.atomic_switch("release-a")

    assert layout.current.is_symlink()
    assert os.readlink(layout.current) == str(Path("releases") / "release-a")


def test_safe_tar_extraction_rejects_traversal_and_links(tmp_path):
    archive = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        info = tarfile.TarInfo("project/../evil")
        info.size = 4
        output.addfile(info, io.BytesIO(b"evil"))

    with pytest.raises(ReleaseError, match="unsafe archive entry"):
        release_manager.safe_extract_tar(archive, tmp_path / "extract")


def test_legacy_persistent_migration_is_idempotent_and_preserves_data(tmp_path):
    root = tmp_path / "autosub-server"
    root.mkdir()
    (root / "autosub_server.py").write_text("APP = True\n", encoding="utf-8")
    (root / "requirements.txt").write_text("", encoding="utf-8")
    manifest = root / "runtime-manifest.txt"
    manifest.write_text(
        "autosub_server.py\nrequirements.txt\nruntime-manifest.txt\n",
        encoding="utf-8",
    )
    (root / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (root / "config.json").write_text('{"dashboard_enabled": true}', encoding="utf-8")
    (root / "autosub.log").write_text("legacy log\n", encoding="utf-8")
    database = root / "data.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE values_table (value TEXT)")
    connection.execute("INSERT INTO values_table VALUES ('preserved')")
    connection.commit()
    connection.close()

    links = MemoryLinks()
    layout = ReleaseLayout(root, links=links)
    legacy_release = layout.prepare_release(
        root,
        manifest,
        "legacy-a",
        prepare_venv=lambda path: (path / "venv").mkdir(),
    )
    backup = migrate_legacy_persistent(root, "legacy-a")
    layout.atomic_switch("legacy-a")
    second = migrate_legacy_persistent(root, "legacy-a")

    assert backup is not None and backup.is_file()
    assert second is None
    assert layout.current_release() == "legacy-a"
    assert (legacy_release / "venv").is_dir()
    assert (root / "shared/.env").read_text(encoding="utf-8") == "TOKEN=secret\n"
    assert (root / "shared/autosub.log").read_text(encoding="utf-8") == "legacy log\n"
    shared = sqlite3.connect(root / "shared/data.db")
    try:
        assert shared.execute("SELECT value FROM values_table").fetchone() == ("preserved",)
    finally:
        shared.close()


def test_database_restore_requires_explicit_isolated_phase(tmp_path):
    database = tmp_path / "data.db"
    backup = tmp_path / "backup.db"
    for path, value in ((database, "new"), (backup, "old")):
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE values_table (value TEXT)")
        connection.execute("INSERT INTO values_table VALUES (?)", (value,))
        connection.commit()
        connection.close()

    with pytest.raises(ReleaseError, match="isolated pre-traffic"):
        restore_database_backup(
            backup,
            database,
            phase=ActivationPhase.TRAFFIC_POSSIBLE,
            service_stopped=True,
        )
    restore_database_backup(
        backup,
        database,
        phase=ActivationPhase.PRE_TRAFFIC,
        service_stopped=True,
    )
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT value FROM values_table").fetchone() == ("old",)
    finally:
        connection.close()


def test_runtime_paths_separate_release_and_shared(monkeypatch, tmp_path):
    root = tmp_path / "autosub-server"
    release = root / "releases/release-a"
    monkeypatch.setenv("AUTOSUB_ROOT", str(root))
    monkeypatch.setenv("AUTOSUB_APP_DIR", str(release))
    monkeypatch.delenv("AUTOSUB_SHARED_DIR", raising=False)

    assert runtime_paths.get_autosub_root() == root
    assert runtime_paths.get_release_dir() == release
    assert runtime_paths.get_shared_dir() == root / "shared"


def test_health_port_is_read_without_sourcing_environment(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TOKEN=secret\nAUTOSUB_PORT='26000'\n", encoding="utf-8")

    assert read_health_port(env) == 26000
    assert read_health_port(env, "27000") == 27000
    with pytest.raises(ReleaseError, match="between 1 and 65535"):
        read_health_port(env, "70000")


def test_systemd_and_manifest_contracts():
    unit = Path("autosub-server.service").read_text(encoding="utf-8")
    manifest = release_manager.read_runtime_manifest(Path("runtime-manifest.txt"))
    updater = Path("update.sh").read_text(encoding="utf-8")

    assert "WorkingDirectory=/opt/autosub-server/current" in unit
    assert "EnvironmentFile=/opt/autosub-server/shared/.env" in unit
    assert "current/venv/bin/python" in unit
    assert "Restart=on-failure" in unit
    assert "Restart=always" not in unit
    assert "User=" + "autosub" not in unit
    assert "Group=" + "autosub" not in unit
    assert "User=root" not in unit
    assert "flock -n" in updater
    assert ".update.lock" in updater
    assert '"$MANAGER" recover' in updater
    assert '--requirements-lock "$SRC_DIR/requirements.txt"' in updater
    assert 'requested="${requested:-main}"' not in updater
    assert "--require-hashes" in Path("release_manager.py").read_text(encoding="utf-8")
    assert not any(
        entry.startswith(("tests/", "docs/", ".codex/")) or entry in {".env", "data.db"}
        for entry in manifest
    )
    assert {path.name for path in Path().glob("*.py")} <= set(manifest)
    install_script = Path("install.sh").read_text(encoding="utf-8")
    assert '"$(uname -s)" != "Linux"' in install_script
    assert '"$(id -u)" -ne 0' in install_script
    assert "Python 3.10 or newer is required" in updater
    assert "AUTOSUB_ADMIN_PASSWORD={admin_password}" in updater
    assert 'if [ ! -f "$APP_DIR/shared/.env" ]' in updater
    assert 'if [ ! -f "$APP_DIR/shared/config.json" ]' in updater
    assert 'install -d -m 700 "$APP_DIR"' in updater
    assert '"$APP_DIR/shared"' in updater
    assert 'chmod 600 "$APP_DIR/shared/.env" "$APP_DIR/shared/config.json"' in updater
    assert 'rm -rf -- "$APP_DIR/shared"' not in updater
