"""Stdio MCP adapter over the Qingyu Gateway HTTP APIs.

The process is a client of the gateway. It does not import gateway code,
open a database, or write evidence except by calling the ingest HTTP API.
"""

__version__ = "0.1.0"
