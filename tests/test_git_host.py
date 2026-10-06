from __future__ import annotations

import json

import httpx
import pytest

from tribe_agent.core.git_host import open_pull_request, parse_github


def test_parse_github_https():
    assert parse_github("https://github.com/owner/repo") == ("owner", "repo")
    assert parse_github("https://github.com/owner/repo.git") == ("owner", "repo")
    assert parse_github("https://github.com/owner/repo/extra") == ("owner", "repo")


def test_parse_github_ssh():
    assert parse_github("git@github.com:owner/repo.git") == ("owner", "repo")


def test_parse_github_rejects_other_hosts():
    assert parse_github("https://gitlab.com/o/r") is None
    assert parse_github("/home/user/repo.git") is None
    assert parse_github("") is None
    assert parse_github("https://example.com") is None


async def test_open_pull_request_success():
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["head"] == "tribe/w1-i1"
        assert body["base"] == "main"
        assert body["title"] == "Fix it"
        headers = request.headers
        assert headers["Authorization"] == "Bearer tok"
        return httpx.Response(201, json={"html_url": "https://github.com/o/r/pull/7"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        url = await open_pull_request(
            "https://github.com/o/r", "tribe/w1-i1", "main", "Fix it", "body", "tok", client=client
        )
    finally:
        await client.aclose()
    assert url == "https://github.com/o/r/pull/7"


async def test_open_pull_request_error_raises():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, text="no base branch")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError):
        try:
            await open_pull_request(
                "https://github.com/o/r", "b", "main", "t", "bd", "tok", client=client
            )
        finally:
            await client.aclose()