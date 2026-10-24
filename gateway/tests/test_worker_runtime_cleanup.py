import logging
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.importers import worker_runtime
from app.importers.worker_runtime import WorkerRuntime


class _Session:
    def __init__(self, events):
        self.events = events

    def rollback(self):
        self.events.append("rollback")

    def close(self):
        self.events.append("close")


class _IdleWorker:
    def __init__(self, db):
        self.db = db

    def recover_stale(self):
        self.db.events.append("recover")

    def claim_next(self, lease_duration):
        self.db.events.append("claim")
        return None


def _result():
    return SimpleNamespace(
        counts={
            "deleted": 0,
            "skipped_active": 0,
            "skipped_retention": 0,
            "skipped_recent": 0,
            "skipped_unknown": 0,
            "quarantined": 0,
            "quarantine_recovered": 0,
            "failed": 0,
        }
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("staging_failed_retention_days", -1),
        ("staging_cleanup_interval_seconds", 0),
        ("staging_cleanup_max_candidates", 0),
    ],
)
def test_cleanup_settings_reject_invalid_values(field, value):
    with pytest.raises(ValidationError):
        Settings(app_env="test", api_token="", **{field: value})


def test_cleanup_runs_before_recovery_at_bounded_monotonic_frequency(tmp_path, monkeypatch):
    events = []
    clock = {"now": 100.0}

    def cleanup(root, db, *, retention_days, max_candidates):
        events.append(("cleanup", root, retention_days, max_candidates))
        return _result()

    monkeypatch.setattr(worker_runtime, "cleanup_failed_staging", cleanup)
    monkeypatch.setattr(worker_runtime, "ImportWorker", _IdleWorker)
    runtime = WorkerRuntime(
        tmp_path,
        session_factory=lambda: _Session(events),
        staging_retention_days=0,
        cleanup_interval_seconds=10,
        cleanup_max_candidates=3,
        monotonic=lambda: clock["now"],
    )

    assert runtime.run_once() is False
    assert events[:3] == [("cleanup", tmp_path, 0, 3), "close", "recover"]

    events.clear()
    clock["now"] = 109.999
    assert runtime.run_once() is False
    assert not any(isinstance(event, tuple) and event[0] == "cleanup" for event in events)

    events.clear()
    clock["now"] = 110.0
    assert runtime.run_once() is False
    assert events[0] == ("cleanup", tmp_path, 0, 3)


def test_cleanup_failure_is_private_and_does_not_block_worker(tmp_path, monkeypatch, caplog):
    marker = "SUPER_SECRET_STAGING_PATH_123"
    events = []

    def broken_cleanup(*args, **kwargs):
        raise RuntimeError(marker)

    monkeypatch.setattr(worker_runtime, "cleanup_failed_staging", broken_cleanup)
    monkeypatch.setattr(worker_runtime, "ImportWorker", _IdleWorker)
    caplog.set_level(logging.ERROR)
    runtime = WorkerRuntime(
        tmp_path,
        session_factory=lambda: _Session(events),
        cleanup_interval_seconds=10,
        monotonic=lambda: 100.0,
    )

    assert runtime.run_once() is False
    assert events == ["rollback", "close", "recover", "claim", "close"]
    assert runtime.last_iteration_failed is False
    assert "staging cleanup failed" in caplog.text
    assert marker not in caplog.text


def test_quarantined_cleanup_is_observable_without_exposing_paths(tmp_path, monkeypatch, caplog):
    events = []
    retained = _result()
    retained.counts["quarantined"] = 1
    monkeypatch.setattr(worker_runtime, "cleanup_failed_staging", lambda *_args, **_kwargs: retained)
    monkeypatch.setattr(worker_runtime, "ImportWorker", _IdleWorker)
    caplog.set_level(logging.WARNING)
    runtime = WorkerRuntime(tmp_path, session_factory=lambda: _Session(events), monotonic=lambda: 100.0)

    assert runtime.run_once() is False
    assert "staging cleanup completed with retained failures" in caplog.text


def test_main_passes_validated_cleanup_configuration(monkeypatch, tmp_path):
    captured = {}

    class Runtime:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_forever(self):
            captured["ran"] = True

    settings = SimpleNamespace(
        archive_root=str(tmp_path),
        log_level="INFO",
        log_format="text",
        staging_failed_retention_days=2,
        staging_cleanup_interval_seconds=45.0,
        staging_cleanup_max_candidates=17,
    )
    monkeypatch.setattr(worker_runtime, "get_settings", lambda: settings)
    monkeypatch.setattr(worker_runtime, "WorkerRuntime", Runtime)

    assert worker_runtime.main() == 0
    assert captured["staging_retention_days"] == 2
    assert captured["cleanup_interval_seconds"] == 45.0
    assert captured["cleanup_max_candidates"] == 17
    assert captured["ran"] is True
