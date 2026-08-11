#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Callable, Protocol


RELEASE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
RELEASE_MARKER = ".autosub-release"
STAGING_MARKER = ".autosub-staging"
SOURCE_MARKER = ".autosub-source"
MAX_ARCHIVE_FILES = 4096
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024


class ReleaseError(RuntimeError):
    pass


class UpdateFailedRollbackSucceeded(ReleaseError):
    pass


class UpdateFailedRollbackFailed(ReleaseError):
    pass


class ActivationPhase(Enum):
    PRE_TRAFFIC = "pre_traffic"
    TRAFFIC_POSSIBLE = "traffic_possible"


class LinkBackend(Protocol):
    def is_link(self, path: Path) -> bool: ...
    def read(self, path: Path) -> Path: ...
    def create(self, target: Path, path: Path) -> None: ...
    def replace(self, source: Path, destination: Path) -> None: ...
    def unlink(self, path: Path) -> None: ...


class OsLinkBackend:
    def is_link(self, path: Path) -> bool:
        return path.is_symlink()

    def read(self, path: Path) -> Path:
        return Path(os.readlink(path))

    def create(self, target: Path, path: Path) -> None:
        os.symlink(target, path, target_is_directory=True)

    def replace(self, source: Path, destination: Path) -> None:
        os.replace(source, destination)

    def unlink(self, path: Path) -> None:
        path.unlink()


def validate_release_name(value: str) -> str:
    if not RELEASE_NAME.fullmatch(value) or value.startswith(".staging-"):
        raise ReleaseError(f"unsafe release name: {value!r}")
    return value


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def read_runtime_manifest(path: Path) -> tuple[str, ...]:
    entries: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        pure = PurePosixPath(value)
        if (
            pure.is_absolute()
            or "\\" in value
            or any(part in {"", ".", ".."} for part in pure.parts)
        ):
            raise ReleaseError(f"unsafe manifest entry: {value!r}")
        normalized = pure.as_posix()
        if normalized in seen:
            raise ReleaseError(f"duplicate manifest entry: {normalized}")
        seen.add(normalized)
        entries.append(normalized)
    if not entries:
        raise ReleaseError("runtime manifest is empty")
    return tuple(entries)


def derive_release_id(source: Path, manifest: Path, requested: str = "") -> str:
    entries = read_runtime_manifest(manifest)
    version = "release"
    config_path = source / "config.py"
    if config_path.is_file():
        match = re.search(
            r'^VERSION\s*=\s*["\']([^"\']+)["\']',
            config_path.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        if match:
            version = f"v{match.group(1).lstrip('v')}"
    requested = requested.strip()
    if requested and requested not in {"latest", "main", "dev"}:
        version = requested
    validate_release_name(version)
    digest = hashlib.sha256()
    for entry in entries:
        candidate = source / Path(entry)
        if not candidate.is_file() or candidate.is_symlink():
            raise ReleaseError(f"required runtime file is missing: {entry}")
        digest.update(entry.encode("utf-8"))
        digest.update(b"\0")
        digest.update(candidate.read_bytes())
    return validate_release_name(f"{version}-{digest.hexdigest()[:12]}")


def derive_legacy_release_id(source: Path) -> str:
    entrypoint = source / "autosub_server.py"
    requirements = source / "requirements.txt"
    if not entrypoint.is_file() or not requirements.is_file():
        raise ReleaseError("legacy install is missing runtime files")
    digest = hashlib.sha256()
    digest.update(entrypoint.read_bytes())
    digest.update(requirements.read_bytes())
    return f"legacy-{digest.hexdigest()[:12]}"


def read_health_port(env_path: Path, override: str = "", default: int = 25500) -> int:
    value = override.strip()
    if not value and env_path.is_file():
        for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, candidate = line.split("=", 1)
            if key.strip() == "AUTOSUB_PORT":
                value = candidate.strip().strip('"').strip("'")
                break
    value = value or str(default)
    try:
        port = int(value)
    except ValueError as exc:
        raise ReleaseError("AUTOSUB_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ReleaseError("AUTOSUB_PORT must be between 1 and 65535")
    return port


def safe_extract_tar(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    destination_root = destination.resolve()
    with tarfile.open(archive_path, mode="r:*") as archive:
        members = archive.getmembers()
        if len(members) > MAX_ARCHIVE_FILES:
            raise ReleaseError("archive contains too many entries")
        if sum(item.size for item in members if item.isfile()) > MAX_ARCHIVE_BYTES:
            raise ReleaseError("archive is too large")
        safe_members: list[tuple[tarfile.TarInfo, tuple[str, ...]]] = []
        roots: set[str] = set()
        for member in members:
            pure = PurePosixPath(member.name)
            if (
                pure.is_absolute()
                or "\\" in member.name
                or any(part in {"", ".", ".."} for part in pure.parts)
                or member.issym()
                or member.islnk()
                or not (member.isdir() or member.isfile())
            ):
                raise ReleaseError(f"unsafe archive entry: {member.name!r}")
            parts = tuple(pure.parts)
            roots.add(parts[0])
            safe_members.append((member, parts))
        if len(roots) != 1:
            raise ReleaseError("archive must contain one top-level directory")
        written: set[Path] = set()
        for member, parts in safe_members:
            relative = Path(*parts[1:])
            if not relative.parts:
                continue
            target = (destination / relative).resolve()
            if not _inside(target, destination_root) or target in written:
                raise ReleaseError(f"unsafe archive destination: {member.name!r}")
            written.add(target)
            if member.isdir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ReleaseError(f"archive file cannot be read: {member.name!r}")
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            if os.name != "nt":
                target.chmod(0o700 if member.mode & 0o111 else 0o600)


class ReleaseLayout:
    def __init__(self, root: Path, *, links: LinkBackend | None = None):
        self.root = Path(root)
        self.releases = self.root / "releases"
        self.shared = self.root / "shared"
        self.current = self.root / "current"
        self.update_state = self.root / ".update-state.json"
        self.links = links or OsLinkBackend()

    def initialize(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.releases.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.shared.mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.shared / "backups").mkdir(mode=0o700, parents=True, exist_ok=True)

    def release_path(self, release_name: str) -> Path:
        name = validate_release_name(release_name)
        path = self.releases / name
        if path.parent.resolve() != self.releases.resolve():
            raise ReleaseError("release path escaped releases root")
        return path

    def prepare_release(
        self,
        source: Path,
        manifest: Path,
        release_name: str,
        *,
        prepare_venv: Callable[[Path], None] | None = None,
        validate: Callable[[Path], None] | None = None,
        allow_missing: bool = False,
    ) -> Path:
        self.initialize()
        final_path = self.release_path(release_name)
        if final_path.exists():
            if (final_path / RELEASE_MARKER).is_file():
                return final_path
            raise ReleaseError("release path already exists without a marker")
        staging = self.releases / (f".staging-{release_name}-{secrets.token_hex(4)}")
        staging.mkdir(mode=0o700)
        (staging / STAGING_MARKER).write_text(release_name, encoding="utf-8")
        try:
            source_root = Path(source).resolve()
            copied = 0
            for entry in read_runtime_manifest(manifest):
                source_path = (source_root / Path(entry)).resolve()
                if not _inside(source_path, source_root):
                    raise ReleaseError(f"manifest source escaped root: {entry}")
                if not source_path.is_file() or source_path.is_symlink():
                    if allow_missing:
                        continue
                    raise ReleaseError(f"required runtime file is missing: {entry}")
                target = staging / Path(entry)
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copy2(source_path, target)
                copied += 1
            if not copied or not (staging / "autosub_server.py").is_file():
                raise ReleaseError("release is missing its server entrypoint")
            if prepare_venv:
                prepare_venv(staging)
            if validate:
                validate(staging)
            (staging / RELEASE_MARKER).write_text(release_name, encoding="utf-8")
            (staging / STAGING_MARKER).unlink()
            os.replace(staging, final_path)
            return final_path
        except BaseException:
            if staging.exists() and _inside(staging, self.releases):
                shutil.rmtree(staging)
            raise

    def current_release(self, *, required: bool = True) -> str | None:
        if not self.current.exists() and not self.links.is_link(self.current):
            if required:
                raise ReleaseError("current release is not configured")
            return None
        if not self.links.is_link(self.current):
            raise ReleaseError("current must be a symbolic link")
        link_target = self.links.read(self.current)
        target = (
            link_target if link_target.is_absolute() else self.current.parent / link_target
        ).resolve(strict=True)
        if target.parent != self.releases.resolve():
            raise ReleaseError("current target is outside releases")
        validate_release_name(target.name)
        if not (target / RELEASE_MARKER).is_file():
            raise ReleaseError("current target is not a complete release")
        return target.name

    def atomic_switch(self, release_name: str) -> None:
        target = self.release_path(release_name)
        if not target.is_dir() or not (target / RELEASE_MARKER).is_file():
            raise ReleaseError("cannot activate an incomplete release")
        temporary = self.root / f".current-new-{os.getpid()}-{secrets.token_hex(4)}"
        if temporary.exists() or self.links.is_link(temporary):
            raise ReleaseError("temporary current link already exists")
        try:
            self.links.create(Path("releases") / release_name, temporary)
            link_target = self.links.read(temporary)
            resolved = (
                link_target if link_target.is_absolute() else temporary.parent / link_target
            ).resolve(strict=True)
            if resolved != target.resolve(strict=True):
                raise ReleaseError("temporary current target validation failed")
            self.links.replace(temporary, self.current)
        finally:
            if temporary.exists() or self.links.is_link(temporary):
                self.links.unlink(temporary)

    def clear_current(self) -> None:
        if self.links.is_link(self.current):
            self.links.unlink(self.current)
        elif self.current.exists():
            raise ReleaseError("refusing to remove non-symlink current")

    def write_update_state(
        self,
        *,
        phase: str,
        previous: str | None,
        candidate: str,
        backup: Path | None,
    ) -> None:
        if phase not in {"prepared", "switched", "rollback"}:
            raise ReleaseError("invalid update state phase")
        if previous is not None:
            validate_release_name(previous)
        validate_release_name(candidate)
        payload = {
            "phase": phase,
            "previous": previous,
            "candidate": candidate,
            "backup": str(backup) if backup else None,
        }
        temporary = self.root / f".update-state-{secrets.token_hex(4)}.tmp"
        try:
            temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            if os.name != "nt":
                temporary.chmod(0o600)
            os.replace(temporary, self.update_state)
        finally:
            temporary.unlink(missing_ok=True)

    def read_update_state(self) -> dict[str, str | None] | None:
        if not self.update_state.exists():
            return None
        try:
            payload = json.loads(self.update_state.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ReleaseError("update state is unreadable") from exc
        if not isinstance(payload, dict):
            raise ReleaseError("update state is invalid")
        phase = payload.get("phase")
        previous = payload.get("previous")
        candidate = payload.get("candidate")
        backup = payload.get("backup")
        if phase not in {"prepared", "switched", "rollback"}:
            raise ReleaseError("update state phase is invalid")
        if previous is not None and not isinstance(previous, str):
            raise ReleaseError("update state previous release is invalid")
        if not isinstance(candidate, str):
            raise ReleaseError("update state candidate is invalid")
        if backup is not None and not isinstance(backup, str):
            raise ReleaseError("update state backup is invalid")
        if previous is not None:
            validate_release_name(previous)
        validate_release_name(candidate)
        return {
            "phase": phase,
            "previous": previous,
            "candidate": candidate,
            "backup": backup,
        }

    def clear_update_state(self) -> None:
        self.update_state.unlink(missing_ok=True)

    def cleanup_staging(self, *, preserve: Path | None = None) -> list[str]:
        self.initialize()
        removed: list[str] = []
        for path in self.releases.iterdir():
            is_staging = (
                path.name.startswith(".staging-")
                and path.is_dir()
                and not path.is_symlink()
                and (path / STAGING_MARKER).is_file()
            )
            is_source = (
                path.name.startswith(".source-")
                and path.is_dir()
                and not path.is_symlink()
                and (path / SOURCE_MARKER).is_file()
            )
            if preserve is not None and _inside(preserve, path):
                continue
            if is_staging or is_source:
                shutil.rmtree(path)
                removed.append(path.name)
        return removed

    def retain(
        self,
        keep: int = 3,
        *,
        previous: str | None = None,
        preparing: str | None = None,
    ) -> list[str]:
        if keep < 1:
            raise ReleaseError("retention must keep at least one release")
        current = self.current_release()
        protected = {item for item in (current, previous, preparing) if item}
        releases = [
            item
            for item in self.releases.iterdir()
            if item.is_dir()
            and not item.is_symlink()
            and RELEASE_NAME.fullmatch(item.name)
            and (item / RELEASE_MARKER).is_file()
        ]
        releases.sort(key=lambda item: (item.stat().st_mtime_ns, item.name), reverse=True)
        retained = {item.name for item in releases[:keep]} | protected
        removed: list[str] = []
        for item in releases:
            if item.name not in retained:
                shutil.rmtree(item)
                removed.append(item.name)
        return removed


def prepare_release_venv(
    release: Path,
    python: str,
    root: Path,
    requirements_lock: Path | None = None,
) -> None:
    requirements = release / "requirements.txt"
    if requirements_lock is not None:
        lock_source = Path(requirements_lock)
        if not lock_source.is_file() or lock_source.is_symlink():
            raise ReleaseError("requirements lock source is not a regular file")
        if lock_source.resolve() != requirements.resolve():
            shutil.copy2(lock_source, requirements)
    subprocess.run([python, "-m", "venv", str(release / "venv")], check=True)
    venv_python = release / "venv" / "bin" / "python"
    subprocess.run(
        [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--require-hashes",
            "-r",
            str(requirements),
        ],
        check=True,
    )
    subprocess.run(
        [str(venv_python), "-m", "compileall", "-q", str(release)],
        check=True,
    )
    environment = os.environ.copy()
    environment.update(
        {
            "AUTOSUB_ROOT": str(root),
            "AUTOSUB_APP_DIR": str(release),
            "AUTOSUB_BACKUP_DIR": str(root / "shared" / "backups"),
            "AUTOSUB_CONFIG": str(root / "shared" / "config.json"),
            "AUTOSUB_DB": str(root / "shared" / "data.db"),
            "AUTOSUB_ENV": str(root / "shared" / ".env"),
            "AUTOSUB_LOG": str(root / "shared" / "autosub.log"),
            "PYTHONPATH": str(release),
        }
    )
    subprocess.run(
        [
            str(venv_python),
            "-c",
            (
                "import autosub_server; "
                "assert autosub_server.app is not None; "
                "assert autosub_server.static_dir.is_dir()"
            ),
        ],
        check=True,
        cwd=release,
        env=environment,
    )


def _verify_sqlite(path: Path) -> None:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ReleaseError("SQLite quick_check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ReleaseError("SQLite foreign_key_check failed")
    finally:
        connection.close()


def create_database_backup(
    database: Path,
    backup_dir: Path,
    release_name: str,
    *,
    prefix: str = "pre-update",
) -> Path | None:
    validate_release_name(release_name)
    if not database.exists():
        return None
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = backup_dir / f"{prefix}-{release_name}-{stamp}-{secrets.token_hex(4)}.db"
    source = sqlite3.connect(database)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    if os.name != "nt":
        destination.chmod(0o600)
    _verify_sqlite(destination)
    return destination


def restore_database_backup(
    backup: Path,
    database: Path,
    *,
    phase: ActivationPhase,
    service_stopped: bool,
) -> None:
    if phase is not ActivationPhase.PRE_TRAFFIC or not service_stopped:
        raise ReleaseError("database restore is allowed only while isolated pre-traffic")
    _verify_sqlite(backup)
    database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = database.parent / f".{database.name}.restore-{secrets.token_hex(4)}"
    source = sqlite3.connect(f"file:{backup.as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(temporary)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    _verify_sqlite(temporary)
    os.replace(temporary, database)
    for suffix in ("-wal", "-shm"):
        Path(f"{database}{suffix}").unlink(missing_ok=True)
    if os.name != "nt":
        database.chmod(0o600)


def migrate_legacy_persistent(root: Path, release_name: str) -> Path | None:
    layout = ReleaseLayout(root)
    layout.initialize()
    shared = layout.shared
    for name, mode in ((".env", 0o600), ("config.json", 0o600)):
        source = root / name
        destination = shared / name
        if source.is_file() and not destination.exists():
            shutil.copy2(source, destination)
            if os.name != "nt":
                destination.chmod(mode)
    log_source = root / "autosub.log"
    log_destination = shared / "autosub.log"
    if log_source.is_file() and not log_destination.exists():
        shutil.copy2(log_source, log_destination)
    legacy_backups = root / "backups"
    if legacy_backups.is_dir() and legacy_backups.resolve() != (shared / "backups").resolve():
        for item in legacy_backups.iterdir():
            destination = shared / "backups" / item.name
            if item.is_file() and not item.is_symlink() and not destination.exists():
                shutil.copy2(item, destination)
    database = root / "data.db"
    shared_database = shared / "data.db"
    backup = None
    if database.exists() and not shared_database.exists():
        backup = create_database_backup(
            database,
            shared / "backups",
            release_name,
            prefix="pre-layout",
        )
        if backup is None:
            raise ReleaseError("legacy database backup was not created")
        restore_database_backup(
            backup,
            shared_database,
            phase=ActivationPhase.PRE_TRAFFIC,
            service_stopped=True,
        )
    return backup


class ServiceRunner(Protocol):
    traffic_isolated: bool

    def stop(self) -> None: ...
    def restart(self) -> None: ...
    def wait_ready(self, path: str) -> bool: ...


class SystemdServiceRunner:
    traffic_isolated = False

    def __init__(self, service: str, port: int, timeout: int):
        self.service = service
        self.port = port
        self.timeout = timeout

    def _systemctl(self, action: str) -> None:
        subprocess.run(["systemctl", action, self.service], check=True)

    def stop(self) -> None:
        self._systemctl("stop")

    def restart(self) -> None:
        self._systemctl("restart")

    def wait_ready(self, path: str) -> bool:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
            try:
                connection.request("GET", path)
                if connection.getresponse().status == 200:
                    return True
            except (OSError, http.client.HTTPException):
                pass
            finally:
                connection.close()
            time.sleep(1)
        return False


@dataclass(frozen=True)
class ActivationOutcome:
    previous: str | None
    current: str
    backup: Path | None


class AtomicUpdater:
    def __init__(self, layout: ReleaseLayout, runner: ServiceRunner):
        self.layout = layout
        self.runner = runner

    def activate(
        self,
        release_name: str,
        *,
        restore_database_when_isolated: bool = False,
    ) -> ActivationOutcome:
        previous = self.layout.current_release(required=False)
        database = self.layout.shared / "data.db"
        backup = create_database_backup(
            database,
            self.layout.shared / "backups",
            release_name,
        )
        self.layout.write_update_state(
            phase="prepared",
            previous=previous,
            candidate=release_name,
            backup=backup,
        )
        switched = False
        try:
            self.layout.atomic_switch(release_name)
            switched = True
            self.layout.write_update_state(
                phase="switched",
                previous=previous,
                candidate=release_name,
                backup=backup,
            )
            self.runner.restart()
            if not self.runner.wait_ready("/health/ready"):
                raise ReleaseError("new release readiness timed out")
            self.layout.clear_update_state()
            return ActivationOutcome(previous, release_name, backup)
        except BaseException as update_error:
            if not switched:
                self.layout.clear_update_state()
                raise
            rollback_errors: list[BaseException] = []
            try:
                self.layout.write_update_state(
                    phase="rollback",
                    previous=previous,
                    candidate=release_name,
                    backup=backup,
                )
            except BaseException as exc:
                rollback_errors.append(exc)
            try:
                self.runner.stop()
            except BaseException as exc:
                rollback_errors.append(exc)
            try:
                if previous is None:
                    self.layout.clear_current()
                    rollback_errors.append(
                        ReleaseError("initial activation has no previous service")
                    )
                else:
                    self.layout.atomic_switch(previous)
            except BaseException as exc:
                rollback_errors.append(exc)
            if (
                backup is not None
                and restore_database_when_isolated
                and self.runner.traffic_isolated
            ):
                try:
                    restore_database_backup(
                        backup,
                        database,
                        phase=ActivationPhase.PRE_TRAFFIC,
                        service_stopped=not rollback_errors,
                    )
                except BaseException as exc:
                    rollback_errors.append(exc)
            if previous is not None and not rollback_errors:
                try:
                    self.runner.restart()
                    if not self.runner.wait_ready("/health"):
                        raise ReleaseError("previous release health timed out")
                except BaseException as exc:
                    rollback_errors.append(exc)
            if rollback_errors:
                if previous is None and self.layout.current_release(required=False) is None:
                    self.layout.clear_update_state()
                raise UpdateFailedRollbackFailed(
                    "UPDATE_FAILED_ROLLBACK_FAILED "
                    f"database_backup={backup or 'none'} database_restore=not_performed"
                ) from update_error
            self.layout.clear_update_state()
            raise UpdateFailedRollbackSucceeded(
                "UPDATE_FAILED_ROLLBACK_SUCCEEDED "
                f"database_backup={backup or 'none'} database_restore=not_performed"
            ) from update_error

    def recover_interrupted(self) -> bool:
        state = self.layout.read_update_state()
        if state is None:
            return False
        previous = state["previous"]
        candidate = state["candidate"]
        phase = state["phase"]
        current = self.layout.current_release(required=False)
        if phase == "prepared" and current == previous:
            self.layout.clear_update_state()
            return True
        if current == candidate:
            if previous is None:
                try:
                    self.runner.stop()
                    self.layout.clear_current()
                finally:
                    self.layout.clear_update_state()
                raise UpdateFailedRollbackFailed(
                    "UPDATE_FAILED_ROLLBACK_FAILED interrupted initial activation"
                )
            try:
                self.runner.stop()
                self.layout.atomic_switch(previous)
                self.runner.restart()
                if not self.runner.wait_ready("/health"):
                    raise ReleaseError("interrupted rollback health timed out")
            except BaseException as exc:
                raise UpdateFailedRollbackFailed(
                    "UPDATE_FAILED_ROLLBACK_FAILED during interrupted recovery"
                ) from exc
            self.layout.clear_update_state()
            return True
        if previous is None and current is None:
            self.layout.clear_update_state()
            return True
        if previous is not None and current == previous:
            try:
                self.runner.restart()
                if not self.runner.wait_ready("/health"):
                    raise ReleaseError("interrupted previous release health timed out")
            except BaseException as exc:
                raise UpdateFailedRollbackFailed(
                    "UPDATE_FAILED_ROLLBACK_FAILED during interrupted recovery"
                ) from exc
            self.layout.clear_update_state()
            return True
        raise UpdateFailedRollbackFailed(
            "UPDATE_FAILED_ROLLBACK_FAILED update state does not match current"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    release_id = commands.add_parser("release-id")
    release_id.add_argument("source", type=Path)
    release_id.add_argument("manifest", type=Path)
    release_id.add_argument("--requested", default="")
    legacy_id = commands.add_parser("legacy-id")
    legacy_id.add_argument("source", type=Path)
    port = commands.add_parser("port")
    port.add_argument("env", type=Path)
    port.add_argument("--override", default="")
    port.add_argument("--default", type=int, default=25500)
    extract = commands.add_parser("safe-extract")
    extract.add_argument("archive", type=Path)
    extract.add_argument("destination", type=Path)
    cleanup = commands.add_parser("cleanup-staging")
    cleanup.add_argument("root", type=Path)
    cleanup.add_argument("--preserve", type=Path)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("root", type=Path)
    prepare.add_argument("source", type=Path)
    prepare.add_argument("manifest", type=Path)
    prepare.add_argument("release")
    prepare.add_argument("--python", default="python3")
    prepare.add_argument("--allow-missing", action="store_true")
    prepare.add_argument("--requirements-lock", type=Path)
    current = commands.add_parser("current")
    current.add_argument("root", type=Path)
    current.add_argument("--optional", action="store_true")
    switch = commands.add_parser("switch")
    switch.add_argument("root", type=Path)
    switch.add_argument("release")
    migrate = commands.add_parser("migrate-legacy")
    migrate.add_argument("root", type=Path)
    migrate.add_argument("release")
    retain = commands.add_parser("retain")
    retain.add_argument("root", type=Path)
    retain.add_argument("--keep", type=int, default=3)
    retain.add_argument("--previous")
    activate = commands.add_parser("activate")
    activate.add_argument("root", type=Path)
    activate.add_argument("release")
    activate.add_argument("--service", default="autosub-server")
    activate.add_argument("--port", type=int, default=25500)
    activate.add_argument("--timeout", type=int, default=45)
    recover = commands.add_parser("recover")
    recover.add_argument("root", type=Path)
    recover.add_argument("--service", default="autosub-server")
    recover.add_argument("--port", type=int, default=25500)
    recover.add_argument("--timeout", type=int, default=45)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "release-id":
            print(derive_release_id(args.source, args.manifest, args.requested))
        elif args.command == "legacy-id":
            print(derive_legacy_release_id(args.source))
        elif args.command == "port":
            print(read_health_port(args.env, args.override, args.default))
        elif args.command == "safe-extract":
            safe_extract_tar(args.archive, args.destination)
        elif args.command == "cleanup-staging":
            for item in ReleaseLayout(args.root).cleanup_staging(preserve=args.preserve):
                print(item)
        elif args.command == "prepare":
            release = ReleaseLayout(args.root).prepare_release(
                args.source,
                args.manifest,
                args.release,
                prepare_venv=lambda path: prepare_release_venv(
                    path,
                    args.python,
                    args.root,
                    args.requirements_lock,
                ),
                allow_missing=args.allow_missing,
            )
            print(release)
        elif args.command == "current":
            value = ReleaseLayout(args.root).current_release(required=not args.optional)
            if value:
                print(value)
        elif args.command == "switch":
            ReleaseLayout(args.root).atomic_switch(args.release)
        elif args.command == "migrate-legacy":
            backup = migrate_legacy_persistent(args.root, args.release)
            if backup:
                print(backup)
        elif args.command == "retain":
            for item in ReleaseLayout(args.root).retain(args.keep, previous=args.previous):
                print(item)
        elif args.command == "activate":
            runner = SystemdServiceRunner(args.service, args.port, args.timeout)
            outcome = AtomicUpdater(ReleaseLayout(args.root), runner).activate(args.release)
            print(f"ACTIVE_RELEASE={outcome.current}")
            if outcome.backup:
                print(f"DATABASE_BACKUP={outcome.backup}")
        elif args.command == "recover":
            runner = SystemdServiceRunner(args.service, args.port, args.timeout)
            recovered = AtomicUpdater(ReleaseLayout(args.root), runner).recover_interrupted()
            if recovered:
                print("INTERRUPTED_UPDATE_RECOVERED")
    except UpdateFailedRollbackSucceeded as exc:
        print(str(exc), file=sys.stderr)
        return 20
    except UpdateFailedRollbackFailed as exc:
        print(str(exc), file=sys.stderr)
        return 21
    except (OSError, ReleaseError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"deployment error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
