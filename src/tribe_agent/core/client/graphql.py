"""GraphQL client for a Tribe community."""

from __future__ import annotations

from typing import Any

import httpx


class GraphQLError(Exception):
    pass


class GraphQLClient:
    def __init__(self, url: str, token: str = "", timeout: float = 30.0) -> None:
        self.url = url
        self.token = token
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def gql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        response = await self._client.post(
            self.url, json={"query": query, "variables": variables or {}}, headers=headers
        )
        response.raise_for_status()
        data = response.json()
        if "errors" in data:
            message = data["errors"][0].get("message", "GraphQL error")
            raise GraphQLError(message)
        return data.get("data", {})
