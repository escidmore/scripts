"""GraphQL HTTP transport for Hardcover API."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from hardcover_tagger.rate_limiter import RateLimiter

API_URL = "https://api.hardcover.app/v1/graphql"
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
DEFAULT_RETRY_DELAY_SECONDS = 65.0
MAX_ATTEMPTS = 2


class GraphQLError(Exception):
    """Raised when the API returns errors in the response body."""

    def __init__(
        self,
        errors: list[dict[str, Any]],
        query_name: str = "",
        data: dict[str, Any] | None = None,
    ) -> None:
        self.errors = errors
        self.data = data or {}
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
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        resp = self._post_with_retries(payload)
        resp.raise_for_status()
        body = resp.json()

        if "errors" in body:
            raise GraphQLError(body["errors"], query_name, body.get("data"))

        data = body.get("data")
        if data is None:
            raise GraphQLError([{"message": "Response contained no data"}], query_name)

        return data

    def _post_with_retries(self, payload: dict[str, Any]) -> httpx.Response:
        last_response: httpx.Response | None = None

        for attempt in range(MAX_ATTEMPTS):
            self.rate_limiter.acquire()
            response = httpx.post(
                self.base_url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                timeout=30.0,
            )
            last_response = response
            if response.status_code not in RETRYABLE_STATUS_CODES:
                return response
            if attempt == MAX_ATTEMPTS - 1:
                return response
            time.sleep(_retry_delay(response))

        if last_response is None:
            msg = "HTTP request was not attempted"
            raise RuntimeError(msg)
        return last_response


def _retry_delay(response: httpx.Response) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after is None:
        return DEFAULT_RETRY_DELAY_SECONDS

    try:
        return float(retry_after)
    except ValueError:
        return DEFAULT_RETRY_DELAY_SECONDS
