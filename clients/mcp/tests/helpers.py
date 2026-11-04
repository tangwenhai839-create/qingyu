"""Values shared by the adapter tests. Not part of the installed tools."""

from __future__ import annotations

from pathlib import Path

from qingyu_mcp.config import Settings

READ = "read-token-value-xxxx"
INGEST = "ingest-token-value-xxxx"


def make_settings(directory: Path) -> Settings:
    return Settings(
        base_url="https://gw.example.test",
        read_token=READ,
        ingest_token=INGEST,
        pending_path=directory / "pending.jsonl",
        timeout_seconds=5.0,
    )
