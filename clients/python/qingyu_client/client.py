"""A small httpx wrapper around the GET-only Evidence Query API."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from typing import Self

import httpx
from qingyu_contracts.evidence import (
    AttachmentAttemptEvidence,
    AttachmentObservationPage,
    ConversationObservationSummary,
    NodeEvidence,
    NodePage,
    ObservationPage,
)

_PREFIX = "/api/v1/evidence"
_Page = ObservationPage | NodePage | AttachmentObservationPage
_Item = ConversationObservationSummary | NodeEvidence | AttachmentAttemptEvidence


class QingyuClient:
    """Talks to one Gateway's `/api/v1/evidence` surface with a read token.

    `base_url` is the gateway origin (`http://127.0.0.1:8000`), not the
    evidence prefix. The token is sent as a Bearer credential on every
    request. Only the GET endpoints are wrapped.
    """

    def __init__(
        self,
        base_url: str,
        read_token: str,
        *,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        # One client instance, one connection pool; reuse it for a session's reads.
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {read_token}"},
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def conversation_observations(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        source_account_id: uuid.UUID | None = None,
        conversation_id: uuid.UUID | None = None,
    ) -> ObservationPage:
        """One page of conversation observations. Gateway default limit is 50."""
        response = self._get(
            f"{_PREFIX}/conversation-observations",
            limit=limit,
            cursor=cursor,
            source_account_id=source_account_id,
            conversation_id=conversation_id,
        )
        return ObservationPage.model_validate(response.json())

    def nodes(
        self,
        conversation_observation_id: uuid.UUID,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> NodePage:
        """One page of nodes for a conversation observation. Gateway default limit is 100."""
        response = self._get(
            f"{_PREFIX}/conversation-observations/{conversation_observation_id}/nodes",
            limit=limit,
            cursor=cursor,
        )
        return NodePage.model_validate(response.json())

    def attachment_observations(
        self,
        conversation_observation_id: uuid.UUID,
        attachment_id: uuid.UUID,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> AttachmentObservationPage:
        """One page of observations for one attachment. Gateway default limit is 100."""
        response = self._get(
            (
                f"{_PREFIX}/conversation-observations/{conversation_observation_id}"
                f"/attachments/{attachment_id}/observations"
            ),
            limit=limit,
            cursor=cursor,
        )
        return AttachmentObservationPage.model_validate(response.json())

    def iter_conversation_observations(
        self,
        *,
        limit: int = 50,
        source_account_id: uuid.UUID | None = None,
        conversation_id: uuid.UUID | None = None,
    ) -> Iterator[ConversationObservationSummary]:
        """Yield every observation, following `next_cursor` until it is null.

        `limit`, `source_account_id`, and `conversation_id` are sent on every
        page. There is no `cursor` parameter: each signed `next_cursor` is
        passed back unchanged by this method.
        """
        return self._iter_items(
            lambda cursor: self.conversation_observations(
                limit=limit,
                cursor=cursor,
                source_account_id=source_account_id,
                conversation_id=conversation_id,
            )
        )

    def iter_nodes(
        self,
        conversation_observation_id: uuid.UUID,
        *,
        limit: int = 100,
    ) -> Iterator[NodeEvidence]:
        """Yield every node, following `next_cursor` until it is null."""
        return self._iter_items(
            lambda cursor: self.nodes(
                conversation_observation_id,
                limit=limit,
                cursor=cursor,
            )
        )

    def iter_attachment_observations(
        self,
        conversation_observation_id: uuid.UUID,
        attachment_id: uuid.UUID,
        *,
        limit: int = 100,
    ) -> Iterator[AttachmentAttemptEvidence]:
        """Yield every attachment observation, following `next_cursor` until it is null."""
        return self._iter_items(
            lambda cursor: self.attachment_observations(
                conversation_observation_id,
                attachment_id,
                limit=limit,
                cursor=cursor,
            )
        )

    def _get(
        self,
        path: str,
        *,
        limit: int,
        cursor: str | None,
        source_account_id: uuid.UUID | None = None,
        conversation_id: uuid.UUID | None = None,
    ) -> httpx.Response:
        params: dict[str, str | int] = {"limit": limit}
        # Absent on the first page. Any other value, including the string
        # "null", is a cursor and is forwarded unchanged.
        if cursor is not None:
            params["cursor"] = cursor
        if source_account_id is not None:
            params["source_account_id"] = str(source_account_id)
        if conversation_id is not None:
            params["conversation_id"] = str(conversation_id)
        response = self._http.get(path, params=params)
        response.raise_for_status()
        return response

    def _iter_items(
        self,
        fetch_page: Callable[[str | None], _Page],
    ) -> Iterator[_Item]:
        """Walk pages until `next_cursor` is null.

        The cursor stays opaque. The value from the contract model is sent
        back as-is: it is not decoded, and an empty page does not stop the
        walk. JSON null is the only stop.
        """
        cursor: str | None = None
        while True:
            page = fetch_page(cursor)
            yield from page.items
            cursor = page.next_cursor
            if cursor is None:
                return
