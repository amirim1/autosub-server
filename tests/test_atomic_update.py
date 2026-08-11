import sqlite3

import pytest

import release_manager
from release_manager import (
    AtomicUpdater,
    ReleaseError,
    ReleaseLayout,
    UpdateFailedRollbackFailed,
    UpdateFailedRollbackSucceeded,
)


class MemoryLinks:
    def __init__(self):
        self.targets = {}

    def is_link(self, path):
        return path in self.targets

    def read(self, path):
        return self.targets[path]

    def create(self, target, path):
        self.targets[path] = target

    def replace(self, source, destination):
        self.targets[destination] = self.targets.pop(source)

    def unlink(self, path):
        del self.targets[path]


class FakeRunner:
    def __init__(self, readiness, *, traffic_isolated=False, on_restart=None):
        self.readiness = iter(readiness)
        self.traffic_isolated = traffic_isolated
        self.on_restart = on_restart
        self.calls = []
        self.restart_count = 0

    def stop(self):
        self.calls.append("stop")

    def restart(self):
        self.calls.append("restart")
        self.restart_count += 1
        if self.on_restart:
            self.on_restart(self.restart_count)

    def wait_ready(self, path):
        self.calls.append(("wait", path))
        return next(self.readiness)


def _layout(tmp_path):
    layout = ReleaseLayout(tmp_path / "autosub-server", links=MemoryLinks())
    layout.initialize()
    for name in ("release-a", "release-b"):
        release = layout.release_path(name)
        release.mkdir()
        (release / release_manager.RELEASE_MARKER).write_text(name, encoding="utf-8")
    layout.atomic_switch("release-a")
    return layout


def _database(layout, value="old"):
    database = layout.shared / "data.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE values_table (value TEXT)")
    connection.execute("INSERT INTO values_table VALUES (?)", (value,))
    connection.commit()
    connection.close()
    return database


def _read_value(database):
    connection = sqlite3.connect(database)
    try:
        return connection.execute("SELECT value FROM values_table").fetchone()[0]
    finally:
        connection.close()


def test_successful_update_switches_to_prepared_release_and_keeps_previous(tmp_path):
    layout = _layout(tmp_path)
    database = _database(layout)
    runner = FakeRunner([True])

    outcome = AtomicUpdater(layout, runner).activate("release-b")

    assert outcome.previous == "release-a"
    assert outcome.current == "release-b"
    assert outcome.backup is not None and outcome.backup.is_file()
    assert layout.current_release() == "release-b"
    assert layout.release_path("release-a").exists()
    assert _read_value(database) == "old"
    assert runner.calls == ["restart", ("wait", "/health/ready")]


def test_backup_failure_does_not_switch_or_touch_service(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    _database(layout)
    runner = FakeRunner([True])
    monkeypatch.setattr(
        release_manager,
        "create_database_backup",
        lambda *args, **kwargs: (_ for _ in ()).throw(ReleaseError("backup failed")),
    )

    with pytest.raises(ReleaseError, match="backup failed"):
        AtomicUpdater(layout, runner).activate("release-b")

    assert layout.current_release() == "release-a"
    assert runner.calls == []


def test_new_readiness_failure_rolls_code_back_and_checks_old_health(tmp_path):
    layout = _layout(tmp_path)
    _database(layout)
    runner = FakeRunner([False, True])

    with pytest.raises(UpdateFailedRollbackSucceeded, match="UPDATE_FAILED_ROLLBACK_SUCCEEDED"):
        AtomicUpdater(layout, runner).activate("release-b")

    assert layout.current_release() == "release-a"
    assert runner.calls == [
        "restart",
        ("wait", "/health/ready"),
        "stop",
        "restart",
        ("wait", "/health"),
    ]


def test_rollback_health_failure_has_distinct_failure(tmp_path):
    layout = _layout(tmp_path)
    _database(layout)
    runner = FakeRunner([False, False])

    with pytest.raises(UpdateFailedRollbackFailed, match="UPDATE_FAILED_ROLLBACK_FAILED"):
        AtomicUpdater(layout, runner).activate("release-b")

    assert layout.current_release() == "release-a"


def test_isolated_pretraffic_failure_restores_database_before_old_restart(tmp_path):
    layout = _layout(tmp_path)
    database = _database(layout)

    def migrate_on_new_start(restart_count):
        if restart_count != 1:
            return
        connection = sqlite3.connect(database)
        connection.execute("UPDATE values_table SET value = 'migrated'")
        connection.commit()
        connection.close()

    runner = FakeRunner([False, True], traffic_isolated=True, on_restart=migrate_on_new_start)
    with pytest.raises(UpdateFailedRollbackSucceeded):
        AtomicUpdater(layout, runner).activate("release-b", restore_database_when_isolated=True)

    assert layout.current_release() == "release-a"
    assert _read_value(database) == "old"


def test_nonisolated_systemd_contract_never_restores_database(tmp_path):
    layout = _layout(tmp_path)
    database = _database(layout)

    def migrate_on_new_start(restart_count):
        if restart_count == 1:
            connection = sqlite3.connect(database)
            connection.execute("UPDATE values_table SET value = 'migrated'")
            connection.commit()
            connection.close()

    runner = FakeRunner([False, True], on_restart=migrate_on_new_start)
    with pytest.raises(UpdateFailedRollbackSucceeded):
        AtomicUpdater(layout, runner).activate("release-b", restore_database_when_isolated=True)

    assert layout.current_release() == "release-a"
    assert _read_value(database) == "migrated"


def test_initial_activation_failure_clears_known_initial_current(tmp_path):
    layout = ReleaseLayout(tmp_path / "autosub-server", links=MemoryLinks())
    layout.initialize()
    release = layout.release_path("release-a")
    release.mkdir()
    (release / release_manager.RELEASE_MARKER).write_text("release-a", encoding="utf-8")
    runner = FakeRunner([False])

    with pytest.raises(UpdateFailedRollbackFailed):
        AtomicUpdater(layout, runner).activate("release-a")

    assert layout.current_release(required=False) is None
    assert not layout.update_state.exists()


def test_next_run_recovers_switch_interruption_to_recorded_previous(tmp_path):
    layout = _layout(tmp_path)
    layout.write_update_state(
        phase="prepared",
        previous="release-a",
        candidate="release-b",
        backup=None,
    )
    layout.atomic_switch("release-b")
    runner = FakeRunner([True])

    recovered = AtomicUpdater(layout, runner).recover_interrupted()

    assert recovered is True
    assert layout.current_release() == "release-a"
    assert not layout.update_state.exists()
    assert runner.calls == ["stop", "restart", ("wait", "/health")]


def test_next_run_finishes_interrupted_rollback_restart(tmp_path):
    layout = _layout(tmp_path)
    layout.write_update_state(
        phase="rollback",
        previous="release-a",
        candidate="release-b",
        backup=None,
    )
    runner = FakeRunner([True])

    assert AtomicUpdater(layout, runner).recover_interrupted() is True

    assert layout.current_release() == "release-a"
    assert runner.calls == ["restart", ("wait", "/health")]
