"""Read-only Python client for the Qingyu Evidence Query API.

GET-only by construction: the client exposes no method that writes, and
every request it makes is a GET. Pagination cursors are signed by the
server. Callers pass them back opaquely; `iter_*` walks pages until
`next_cursor` is null.
"""

from .client import QingyuClient

__all__ = ["QingyuClient"]
