"""Private `opencode serve` owned by the agent.

opencode v2 loads agents, MCP servers and providers when a server starts and
ignores the environment of whoever runs `opencode run`, so neither the shared
background service nor per-run flags can carry the agent's configuration. The
agent therefore keeps its own managed server alive and points every run at it,
paying the startup cost once instead of once per task.

Running it as a managed service (`opencode service start`) rather than passing
`--server` is what makes the target directory work: the CLI sends the working
directory to a service it discovers through its state directory, but ignores it
for an explicitly addressed server, which would silently bind every run to the
directory the server was started in.

The sidecar gets its own XDG directories so that it neither reads nor disturbs
the operator's interactive opencode: the config home holds the agents and MCP
definition, the data home holds sessions and the copied credentials, and the
state home keeps its service registration, port and password.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

from tribe_agent.core.config import AppConfig
from tribe_agent.core.opencode_config import (
    DEFAULT_PORT,
    USER_DATA_DIR,
    agents_path,
    config_home,
    ensure_config_home,
    missing_agents,
    sidecar_data_path,
    sidecar_db_path,
    sidecar_state_path,
)

logger = logging.getLogger("tribe_agent")

STARTUP_TIMEOUT = 60

OK = "ok"
DOWN = "down"


class OpencodeSidecar:
    def __init__(self, config: AppConfig, port: int | None = None) -> None:
        self.config = config
        self.port = int(port or config.opencode.port or DEFAULT_PORT)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def db_path(self) -> Path:
        return sidecar_db_path(self.config)

    def env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("OPENCODE")}
        env.update(
            {
                "XDG_CONFIG_HOME": str(config_home(self.config)),
                "XDG_DATA_HOME": str(sidecar_data_path(self.config)),
                "XDG_STATE_HOME": str(sidecar_state_path(self.config)),
            }
        )
        if self.config.community.mcp_url:
            env["TRIBE_MCP_URL"] = self.config.community.mcp_url
        if self.config.community.bot_token:
            env["TRIBE_BOT_TOKEN"] = self.config.community.bot_token
        return env

    async def _service(self, *args: str) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "opencode",
            "service",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=self.env(),
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return -1, "timeout"
        return proc.returncode or 0, out.decode(errors="replace").strip()

    async def probe(self) -> str:
        code, out = await self._service("status")
        return OK if code == 0 and out.startswith("http") else DOWN

    async def ensure(self) -> None:
        if await self.probe() == OK:
            return
        await self.start()

    async def start(self) -> None:
        ensure_config_home(self.config)
        self._copy_credentials()
        sidecar_data_path(self.config).mkdir(parents=True, exist_ok=True)

        code, out = await self._service("set", "port", str(self.port))
        if code != 0:
            raise RuntimeError(f"cannot set opencode service port: {out}")
        code, out = await self._service("start")
        if code != 0:
            raise RuntimeError(f"cannot start opencode sidecar: {out}")
        logger.info("opencode sidecar starting on %s", out or self.url)

        deadline = asyncio.get_running_loop().time() + STARTUP_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            if await self.probe() == OK:
                logger.info("opencode sidecar ready on %s", self.url)
                return
            await asyncio.sleep(1)
        raise RuntimeError(f"opencode sidecar did not become ready on {self.url}")

    async def stop(self) -> None:
        if await self.probe() == DOWN:
            return
        code, out = await self._service("stop")
        if code != 0:
            logger.warning("cannot stop opencode sidecar: %s", out)
        else:
            logger.info("opencode sidecar stopped on %s", self.url)

    def _copy_credentials(self) -> None:
        source = USER_DATA_DIR / "auth.json"
        if not source.is_file():
            return
        target_dir = sidecar_data_path(self.config) / "opencode"
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target_dir / "auth.json")


def agents_ready(config: AppConfig) -> bool:
    return bool(agents_path(config).is_dir()) and not missing_agents(config)
