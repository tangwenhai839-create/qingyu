# qingyu-client

Read-only Python client for the Gateway Evidence Query API (`GET /api/v1/evidence/...`).

The client sends the read token as a Bearer credential, parses responses with the `evidence-query-v1` models from `qingyu-contracts`, and follows signed `next_cursor` values until the server returns null. A cursor is opaque: it is not decoded, and a page with no items does not end the walk. There are no write methods.

`base_url` is the gateway origin, for example `http://127.0.0.1:8000`.

## Install

Python 3.12, from a checkout of this repository:

```bash
cd clients/python
pip install -e ../../contracts -e ".[dev]"
```

`qingyu-contracts` is the local package in [`contracts/`](../../contracts). Install that first; it is not published to PyPI.

## Usage

```python
import uuid

from qingyu_client import QingyuClient

with QingyuClient("http://127.0.0.1:8000", read_token) as client:
    for observation in client.iter_conversation_observations(limit=50):
        print(observation.conversation_observation_id, observation.observed_title)
        for node in client.iter_nodes(observation.conversation_observation_id):
            print(node.observation_order, node.message_presence)
```

`iter_conversation_observations` takes `limit`, `source_account_id`, and `conversation_id`. `iter_nodes` and `iter_attachment_observations` take the same ids as the single-page methods, plus `limit`. None of the iterators accept a cursor; pass `cursor=` only to `conversation_observations`, `nodes`, or `attachment_observations` when you want one page.

Defaults match the gateway: 50 for conversation observations, 100 for nodes and attachment observations.

## Tests

```bash
pytest -q
```
