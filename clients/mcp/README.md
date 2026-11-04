# qingyu-mcp

Stdio MCP adapter for a running Qingyu Gateway. It is a separate process. It does not import gateway code, open the gateway database, or write evidence except by calling the gateway's HTTP APIs.

Streamable HTTP is not part of this package. `python -m qingyu_mcp` speaks newline-delimited JSON-RPC on stdin and stdout and writes logs to stderr. It does not listen on a port.

## Tools

| Tool | Gateway call | Effect |
|---|---|---|
| `search_evidence` | `POST /api/v1/evidence/search` | Read-only. Returns the `evidence-search-v1` JSON. |
| `read_evidence` | `GET /api/v1/evidence/...` | Read-only. One page of `evidence-query-v1`: conversation observations, nodes, or attachment observations. |
| `ingest_message` | `POST /api/v1/ingest/messages` | Appends one message through Archive → ImportJob. |
| `ingest_conversation` | `POST /api/v1/ingest/conversations` | Records an empty conversation on that same path. |

`read_evidence` takes `view`: `conversation_observations`, `nodes`, or `attachment_observations`. Pass `next_cursor` back as `cursor` with the same filters. The adapter does not walk pages for you.

The write tools do not accept `client_request_id` or `payload_hash`. Pass an `idempotency_key` and reuse that same key on every retry, including after this process restarts. The adapter appends a line to the pending log, fsyncs it, and only then POSTs. The line maps the key to a `mcp-` UUID. That UUID is the gateway `client_request_id`. A payload hash is never used as the id. The log is not a queue: startup does not replay it, and a second key is a second write even when the text matches.

A 202 response means the import worker has not finished. Call the same tool again with the same `idempotency_key`. The gateway returns the stored result when the body still matches, and `409 idempotency_key_reused` when it does not.

## Configure

The process reads these variables. It does not read `gateway/.env`. Tokens are sent only as `Authorization` headers. They are not written to the pending log, and log lines replace the raw values with `[redacted]`.

| Variable | Required | Meaning |
|---|---|---|
| `QINGYU_GATEWAY_BASE_URL` | yes | Gateway origin, for example `http://127.0.0.1:8000`. No userinfo, path, or query. |
| `QINGYU_EVIDENCE_READ_TOKEN` | yes | Raw `EVIDENCE_READ_API_TOKEN`. Used for search and read. At least 16 characters. |
| `QINGYU_INGEST_TOKEN` | yes | Raw ingest bearer token, the value whose SHA-256 is listed in `INGEST_CLIENTS`. Not the hash. At least 16 characters, and different from the read token. |
| `QINGYU_MCP_PENDING_PATH` | no | Pending log file. Default: `~/.local/state/qingyu-mcp/pending.jsonl`. Keep this path across restarts. |

The client connects directly to the base URL. It does not use `HTTP_PROXY` and it does not follow redirects.

## Run

Python 3.12, from a checkout:

```bash
cd clients/mcp
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e .

export QINGYU_GATEWAY_BASE_URL=http://127.0.0.1:8000
export QINGYU_EVIDENCE_READ_TOKEN='the evidence read token'
export QINGYU_INGEST_TOKEN='the raw ingest token'
export QINGYU_MCP_PENDING_PATH="$HOME/.local/state/qingyu-mcp/pending.jsonl"

python -m qingyu_mcp
```

`qingyu-mcp` is the same entry point. The process waits on stdin. A host launches it and keeps the pending-log path stable. One host entry looks like this, with the command pointed at the virtualenv's Python:

```json
{
  "mcpServers": {
    "qingyu": {
      "command": "/path/to/clients/mcp/.venv/bin/python",
      "args": ["-m", "qingyu_mcp"],
      "env": {
        "QINGYU_GATEWAY_BASE_URL": "http://127.0.0.1:8000",
        "QINGYU_EVIDENCE_READ_TOKEN": "the evidence read token",
        "QINGYU_INGEST_TOKEN": "the raw ingest token",
        "QINGYU_MCP_PENDING_PATH": "/home/you/.local/state/qingyu-mcp/pending.jsonl"
      }
    }
  }
}
```

The gateway still needs its own API process and, for a write to become evidence, the import worker. This adapter does not start either of them.

## Tests

No database:

```bash
cd clients/mcp
pip install -e ".[dev]"
pytest -q
```

## Freeze

- Thin adapter only. No second write path, no outbox, no queue, no hash chain.
- Writes are `POST /api/v1/ingest/messages` and `POST /api/v1/ingest/conversations` only.
- `client_request_id` is reused from the pending log across retry and process restart. It is not a payload hash.
- No `FACT_*`, no Derived, no Core changes, no new Evidence or Source tables.
- `evidence-query-v1` and `evidence-search-v1` responses are returned unchanged.
