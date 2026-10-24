from collections.abc import Iterable

from .generic import ParserRegistry
from .status import PARTIAL

ARCHIVE_INTEGRITY_FAILURE = "archive_integrity_failure"
PARSER_CONTRACT_UNAVAILABLE = "parser_contract_unavailable"
PARSER_CONTRACT_CONFLICT = "parser_contract_conflict"


def supported_parser_identities(registry: ParserRegistry | None = None) -> frozenset[tuple[str, str]]:
    """Return the parser contracts that this process can replay safely."""
    active = registry or ParserRegistry()
    return frozenset((parser.name, parser.version) for parser in active.parsers)


def retry_block_reason(job, *, archive=None, registry: ParserRegistry | None = None) -> str | None:
    """Return a stable reason when retrying ``job`` cannot make progress.

    The caller should evaluate this before its atomic retry transition.  The
    transition itself must repeat the same predicates to remain concurrency-safe.
    """
    if (
        getattr(job, "error_code", None) == ARCHIVE_INTEGRITY_FAILURE
        or getattr(job, "result_code", None) == ARCHIVE_INTEGRITY_FAILURE
        or (archive is not None and getattr(archive, "integrity_status", None) != "verified")
    ):
        return ARCHIVE_INTEGRITY_FAILURE

    # A partial job owns durable batches written with the parser contract saved
    # on the job. Replaying them with a different installed contract is unsafe.
    if getattr(job, "status", None) == PARTIAL:
        identity = (getattr(job, "parser_name", None), getattr(job, "parser_version", None))
        if identity not in supported_parser_identities(registry):
            return PARSER_CONTRACT_UNAVAILABLE
    return None


def committed_parser_block_reason(
    identities: Iterable[tuple[str, str]], *, registry: ParserRegistry | None = None
) -> str | None:
    """Validate durable batch contracts before a claimed job is processed."""
    supported = supported_parser_identities(registry)
    if any(identity not in supported for identity in identities):
        return PARSER_CONTRACT_UNAVAILABLE
    return None


def resolve_durable_parser_contract(job, identities, *, registry: ParserRegistry | None = None):
    """Resolve the one parser contract proven by durable ledger rows."""
    active = registry or ParserRegistry()
    distinct = set(identities)
    if not distinct:
        return None, None
    if len(distinct) != 1:
        return None, PARSER_CONTRACT_CONFLICT
    identity = next(iter(distinct))
    job_identity = (getattr(job, "parser_name", None), getattr(job, "parser_version", None))
    if identity != job_identity:
        return None, PARSER_CONTRACT_CONFLICT
    parser = active.resolve(*identity)
    if parser is None:
        return None, PARSER_CONTRACT_UNAVAILABLE
    return parser, None
