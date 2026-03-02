"""GraphQL HTTP transport for Hardcover API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from hardcover_tagger.rate_limiter import RateLimiter

API_URL = "https://api.hardcover.app/v1/graphql"


class GraphQLError(Exception):
    """Raised when the API returns errors in the response body."""

    def __init__(self, errors: list[dict[str, Any]], query_name: str = "") -> None:
        self.errors = errors
        messages = "; ".join(e.get("message", str(e)) for e in errors)
        ctx = f" [{query_name}]" if query_name else ""
        super().__init__(f"GraphQL error{ctx}: {messages}")


@dataclass
class GraphQLClient:
    """Thin wrapper around httpx for Hardcover's GraphQL endpoint."""

    api_key: str
    rate_limiter: RateLimiter
    base_url: str = API_URL

    def execute(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        query_name: str = "",
    ) -> dict[str, Any]:
        """Send a GraphQL request, check for errors, return the data dict."""
        self.rate_limiter.acquire()

        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        resp = httpx.post(
            self.base_url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        body = resp.json()

        if "errors" in body:
            raise GraphQLError(body["errors"], query_name)

        data = body.get("data")
        if data is None:
            raise GraphQLError([{"message": "Response contained no data"}], query_name)

        return data
