"""Integration tests against a live Tribe API + opencode.

The light test runs the executor scanner against a live API. The heavy e2e
test additionally spends model tokens, so it is gated behind
`TRIBE_AGENT_TEST_E2E=1`.

Required env when enabled:
- TRIBE_AGENT_BOT_TOKEN, TRIBE_AGENT_USER_ID — the community bot credentials;
- TRIBE_AGENT_TEST_API (default http://localhost:8000/graphql);
- TRIBE_AGENT_TEST_COMMUNITY — scope the light scan (optional).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from tribe_agent.core.config import load_config
from tribe_agent.core.scheduler import Runner
from tribe_agent.modules.executor import ExecutorModule

API = os.environ.get("TRIBE_AGENT_TEST_API", "http://localhost:8000/graphql")
BOT_TOKEN = os.environ.get("TRIBE_AGENT_BOT_TOKEN", "")
BOT_USER_ID = os.environ.get("TRIBE_AGENT_USER_ID", "")
TEST_COMMUNITY = os.environ.get("TRIBE_AGENT_TEST_COMMUNITY", "")

pytestmark = pytest.mark.integration


def _require_bot() -> None:
    if not BOT_TOKEN or not BOT_USER_ID:
        pytest.skip("TRIBE_AGENT_BOT_TOKEN / TRIBE_AGENT_USER_ID not set")


def _api_online() -> bool:
    try:
        resp = httpx.post(API, json={"query": "{ __typename }"}, timeout=5)
        return resp.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def _config(tmp_path: Path, community_ids: list[str] | None = None) -> object:
    config = load_config(profile="community")
    config.community.api_url = API
    config.community.bot_token = BOT_TOKEN
    config.community.user_id = BOT_USER_ID
    config.community.communities = community_ids or [c for c in [TEST_COMMUNITY] if c]
    config.data_dir = str(tmp_path / "data")
    config.repo.root = str(tmp_path / "repos")
    config.opencode.model = os.environ.get("TRIBE_AGENT_TEST_MODEL", "") or config.opencode.model
    return config


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _make_bare(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    (src / "README.md").write_text("# E2E\n")
    _git(src, "add", "-A")
    _git(src, "-c", "user.email=t@t", "-c", "user.name=tribe", "commit", "-qm", "chore: init")
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    return bare


async def _post(client: httpx.AsyncClient, query: str, token: str = "") -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = await client.post(API, json={"query": query}, headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "errors" not in data, data.get("errors")
    return data["data"]


def test_api_reachable() -> None:
    _require_bot()
    assert _api_online(), f"Tribe API not reachable at {API}"


async def test_executor_scan_against_live_api(tmp_path: Path) -> None:
    _require_bot()
    if not _api_online():
        pytest.skip("live API not reachable")
    config = _config(tmp_path)
    runner = Runner(config, [ExecutorModule()])
    await runner.scan_once("executor")


async def test_executor_full_e2e_flow(tmp_path: Path) -> None:
    _require_bot()
    if os.environ.get("TRIBE_AGENT_TEST_E2E", "") != "1":
        pytest.skip("set TRIBE_AGENT_TEST_E2E=1 to run the full e2e (spends model tokens)")
    if not _api_online():
        pytest.skip("live API not reachable")

    bare = await asyncio.to_thread(lambda: _make_bare(tmp_path))
    email = f"e2e_{uuid4().hex[:8]}@test.local"

    async with httpx.AsyncClient(timeout=30) as client:
        data = await _post(
            client,
            f'mutation {{ register(email: "{email}", password: "123456", name: "E2E") {{ accessToken }} }}',
        )
        owner = data["register"]["accessToken"]

        data = await _post(client, 'mutation { createCommunity(input: {name: "E2E Com"}) { id } }', owner)
        cid = data["createCommunity"]["id"]

        await _post(
            client,
            f'mutation {{ setModules(input: {{communityId: "{cid}", moduleIds: ["workspaces"]}}) {{ id }} }}',
            owner,
        )

        data = await _post(
            client,
            f'mutation {{ createWorkspace(input: {{name: "Engine", communityId: "{cid}"}}) {{ id }} }}',
            owner,
        )
        ws_id = data["createWorkspace"]["id"]

        await _post(
            client,
            f'mutation {{ setWorkspaceRepository(workspaceId: "{ws_id}", url: "{bare}", branch: "main") {{ id }} }}',
            owner,
        )

        data = await _post(
            client,
            f'mutation {{ join(input: {{userId: "{BOT_USER_ID}", communityId: "{cid}"}}) {{ id status }} }}',
            BOT_TOKEN,
        )
        assert data["join"]["status"] == "active"

        data = await _post(
            client,
            f'mutation {{ createIssue(input: {{title: "Add E2E_MARKER", '
            f'description: "Append the exact text line E2E_MARKER as a new line at the end of README.md, '
            f'commit with a conventional message, then comment with the commit hash and set status to resolved.", '
            f'workspaceId: "{ws_id}", communityId: "{cid}", assigneeId: "{BOT_USER_ID}"}}) {{ id }} }}',
            owner,
        )
        issue_id = data["createIssue"]["id"]

    config = _config(tmp_path, [cid])
    runner = Runner(config, [ExecutorModule()])
    await runner.scan_once("executor")
    await runner.run_queue_once()

    async with httpx.AsyncClient(timeout=20) as client:
        data = await _post(
            client,
            f'query {{ issue(id: "{issue_id}") {{ status }} comments(parentId: "{issue_id}") {{ id }} }}',
            BOT_TOKEN,
        )
        assert data["issue"]["status"] == "resolved"
        assert len(data.get("comments", [])) >= 1