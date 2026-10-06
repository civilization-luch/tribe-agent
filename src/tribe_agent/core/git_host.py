"""Git host helpers — opening pull requests against remote repositories."""

from __future__ import annotations

import httpx


def parse_github(url: str) -> tuple[str, str] | None:
    """Return (owner, repo) for a GitHub https/ssh URL, else None."""
    if not url:
        return None
    url = url.strip().removesuffix(".git")
    if url.startswith("https://github.com/"):
        parts = url.split("https://github.com/", 1)[1].split("/")
    elif url.startswith("git@github.com:"):
        tail = url.split("git@github.com:", 1)[1]
        owner, _, rest = tail.partition("/")
        parts = [owner, rest]
    else:
        return None
    if len(parts) >= 2 and parts[0] and parts[1]:
        return parts[0], parts[1]
    return None


async def open_pull_request(
    url: str,
    head: str,
    base: str,
    title: str,
    body: str,
    token: str,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Open a GitHub pull request and return its html url."""
    parsed = parse_github(url)
    if parsed is None:
        raise ValueError("unsupported PR host (only github.com)")
    owner, repo = parsed
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=30)
    try:
        resp = await client.post(
            f"https://api.github.com/repos/{owner}/{repo}/pulls",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"title": title, "head": head, "base": base, "body": body},
        )
    finally:
        if owns_client:
            await client.aclose()
    if resp.status_code >= 400:
        raise RuntimeError(
            f"github pull request failed ({resp.status_code}): {resp.text[:300]}"
        )
    data = resp.json()
    return data.get("html_url", "")