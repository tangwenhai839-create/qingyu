from types import SimpleNamespace

import pytest
from app.importers import processor as processor_module
from app.importers.generic import GenericTreeParser, ParserRegistry
from app.importers.processor import ImportProcessor, NonRetryablePreflightError
from app.importers.retry_policy import (
    ARCHIVE_INTEGRITY_FAILURE,
    PARSER_CONTRACT_UNAVAILABLE,
    committed_parser_block_reason,
    retry_block_reason,
)


def _job(**overrides):
    values = {
        "status": "partial",
        "parser_name": GenericTreeParser.name,
        "parser_version": GenericTreeParser.version,
        "error_code": "processor_error",
        "result_code": "partial",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_archive_integrity_failure_is_not_retryable_by_error_or_archive_state():
    assert retry_block_reason(_job(error_code=ARCHIVE_INTEGRITY_FAILURE)) == ARCHIVE_INTEGRITY_FAILURE
    archive = SimpleNamespace(integrity_status="integrity_failure")
    assert retry_block_reason(_job(), archive=archive) == ARCHIVE_INTEGRITY_FAILURE


def test_partial_retry_requires_installed_parser_name_and_version():
    assert retry_block_reason(_job()) is None
    assert retry_block_reason(_job(parser_name="retired-parser")) == PARSER_CONTRACT_UNAVAILABLE
    assert retry_block_reason(_job(parser_version="retired-version")) == PARSER_CONTRACT_UNAVAILABLE


def test_failed_job_without_durable_partial_state_does_not_require_old_parser():
    job = _job(status="failed", parser_name="retired-parser", parser_version="retired-version")
    assert retry_block_reason(job) is None


def test_claimed_replay_rejects_unavailable_durable_batch_contract():
    current = (GenericTreeParser.name, GenericTreeParser.version)
    assert committed_parser_block_reason([current]) is None
    assert committed_parser_block_reason([("retired-parser", "1")]) == PARSER_CONTRACT_UNAVAILABLE


def test_policy_accepts_matching_custom_registry_contract():
    class CompatibleParser(GenericTreeParser):
        name = "compatible-parser"
        version = "2"

    registry = ParserRegistry([CompatibleParser()])
    assert retry_block_reason(
        _job(parser_name=CompatibleParser.name, parser_version=CompatibleParser.version),
        registry=registry,
    ) is None


@pytest.mark.parametrize(
    ("integrity_status", "batch_identities", "expected"),
    [
        ("integrity_failure", [], ARCHIVE_INTEGRITY_FAILURE),
        ("verified", [("retired-parser", "1")], PARSER_CONTRACT_UNAVAILABLE),
    ],
)
def test_processor_preflight_failure_is_written_to_a_terminal_state(
    monkeypatch, tmp_path, integrity_status, batch_identities, expected
):
    archive = SimpleNamespace(integrity_status=integrity_status, relative_path="unused")

    class FakeResult:
        def all(self):
            return batch_identities

    class FakeDb:
        def get(self, _model, _identifier):
            return archive

        def scalar(self, _statement):
            return job

        def execute(self, _statement):
            return FakeResult()

        def rollback(self):
            pass

    terminal = {}

    class FakeWorker:
        def __init__(self, _db):
            pass

        def check_ownership(self, _job, _token):
            pass

        def mark_failed_or_partial(self, _job_id, _token, error, report, result_code):
            terminal.update(error=error, report=report, result_code=result_code, status="failed")
            return "failed"

        def attempt_staging_path(self, *_args):
            raise AssertionError("preflight failure must occur before staging")

    monkeypatch.setattr(processor_module, "ImportWorker", FakeWorker)
    job = SimpleNamespace(
        id="job",
        archive_object_id="archive",
        parser_name="retired-parser",
        parser_version="1",
    )

    with pytest.raises(NonRetryablePreflightError, match=expected):
        ImportProcessor(FakeDb(), tmp_path).process(job, "token")

    assert terminal["status"] == "failed"
    assert terminal["error"] == terminal["result_code"] == expected
