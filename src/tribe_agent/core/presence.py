"""Presence heartbeat loop."""

from __future__ import annotations

import asyncio

from tribe_agent.core.module import ScanContext

PRESENCE_HEARTBEAT_SECONDS = 30


async def presence_loop(ctx: ScanContext, interval: int = PRESENCE_HEARTBEAT_SECONDS) -> None:
    while True:
        try:
            await ctx.client.gql("mutation { presenceHeartbeat }")
        except Exception as exc:  # noqa: BLE001
            ctx.logger.warning("presence heartbeat failed: %s", exc)
        await asyncio.sleep(interval)
