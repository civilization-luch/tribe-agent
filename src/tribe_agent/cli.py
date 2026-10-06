"""tribe-agent command line interface."""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import subprocess
import sys
from pathlib import Path

from tribe_agent.core.config import load_config

INIT_TEMPLATE = """\
profile: community
data_dir: ./data

community:
  api_url: https://api.tribe.partnr.icu/graphql
  mcp_url: https://mcp.tribe.partnr.icu/mcp
  bot_token: ""
  user_id: ""
  communities: []

project_path: ""

repo:
  root: ./data/repos
  strategy: shallow
  update: lazy
  refetch_interval_minutes: 60
  default_url: ""
  default_branch: ""
  git_token: ""

concurrency:
  max_runs: 2
  max_queue: 200

opencode:
  config_dir: ""
  db: ""
  url: ""
  password: ""
  model: ""
  providers: {}

telegram:
  bot_token: ""
  admin_chat_id: ""
"""


def _resolve_config(args: argparse.Namespace):
    if args.config and not Path(args.config).is_file():
        print(f"config not found: {args.config}", file=sys.stderr)
        raise SystemExit(1)
    path = args.config if args.config else None
    return load_config(path, args.profile)


def cmd_init(args: argparse.Namespace) -> int:
    output = Path(args.output)
    if output.exists() and not args.force:
        print(f"{output} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    output.write_text(INIT_TEMPLATE, encoding="utf-8")
    print(f"wrote {output}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from tribe_agent.core.scheduler import Runner
    from tribe_agent.modules import load_modules

    config = _resolve_config(args)
    logger = logging.getLogger("tribe_agent")
    logger.info("starting tribe-agent (profile=%s)", config.profile)
    try:
        asyncio.run(Runner(config, load_modules(config)).run())
    except KeyboardInterrupt:
        logger.info("stopped")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    from tribe_agent.core.scheduler import Runner
    from tribe_agent.modules import load_modules

    config = _resolve_config(args)
    asyncio.run(Runner(config, load_modules(config)).scan_once(args.name))
    return 0


def cmd_run_queue(args: argparse.Namespace) -> int:
    from tribe_agent.core.scheduler import Runner
    from tribe_agent.modules import load_modules

    config = _resolve_config(args)
    asyncio.run(Runner(config, load_modules(config)).run_queue_once())
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config = _resolve_config(args)
    ok = True

    def check(label: str, value: bool, hint: str = "") -> None:
        nonlocal ok
        mark = "ok  " if value else "FAIL"
        if not value:
            ok = False
        suffix = f" — {hint}" if hint and not value else ""
        print(f"[{mark}] {label}{suffix}")

    check("bot_token set", bool(config.community.bot_token), "set community.bot_token")
    check("user_id set", bool(config.community.user_id), "set community.user_id")
    check("mcp_url set", bool(config.community.mcp_url), "set community.mcp_url")
    check("opencode on PATH", shutil.which("opencode") is not None, "install opencode")
    check(
        "opencode config dir",
        Path(config.opencode.config_dir).is_dir(),
        config.opencode.config_dir,
    )
    check(
        "project or repo binding",
        bool(config.project_path)
        or bool(config.module_settings.get("executor", {}).get("repos"))
        or bool(config.repo.default_url),
        "set project_path, repo.default_url or executor.repos",
    )

    if config.community.api_url and config.community.bot_token:
        import httpx

        try:
            resp = httpx.post(
                config.community.api_url,
                json={"query": "{ __typename }"},
                headers={"Authorization": f"Bearer {config.community.bot_token}"},
                timeout=10,
            )
            check(f"api reachable ({resp.status_code})", resp.status_code == 200, config.community.api_url)
        except Exception as exc:  # noqa: BLE001
            check("api reachable", False, str(exc))

    print("all good" if ok else "some checks failed")
    return 0 if ok else 1


def cmd_sessions(args: argparse.Namespace) -> int:
    from tribe_agent.core.opencode_config import default_db_path

    config = _resolve_config(args)
    db = Path(config.opencode.db) if config.opencode.db else default_db_path()
    if not db.is_file():
        print(f"no opencode database found at {db}")
        return 0
    result = subprocess.run(
        ["sqlite3", str(db), "SELECT id, title, time_created FROM session ORDER BY time_created DESC LIMIT 20;"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(result.stderr.strip() or "failed to read sessions")
        return 1
    print(result.stdout.strip() or "(no sessions)")
    return 0


def cmd_serve_assistant(args: argparse.Namespace) -> int:
    import uvicorn

    from tribe_agent.modules.assistant import make_app

    config = _resolve_config(args)
    app = make_app(config)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tribe-agent")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument("--profile", default=None, help="override profile (community|platform)")
    parser.add_argument("--log-level", default="INFO")

    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="write a starter config.yaml")
    p_init.add_argument("--output", default="config.yaml")
    p_init.add_argument("--force", action="store_true")
    p_init.set_defaults(func=cmd_init)

    sub.add_parser("run", help="start the scheduler").set_defaults(func=cmd_run)

    p_scan = sub.add_parser("scan", help="run a single scanner once")
    p_scan.add_argument("name", nargs="?", default="all")
    p_scan.set_defaults(func=cmd_scan)

    sub.add_parser("run-queue", help="drain the task queue once").set_defaults(func=cmd_run_queue)
    sub.add_parser("doctor", help="check configuration and connectivity").set_defaults(func=cmd_doctor)
    sub.add_parser("sessions", help="list opencode sessions").set_defaults(func=cmd_sessions)

    p_assistant = sub.add_parser("serve-assistant", help="serve the assistant API (platform)")
    p_assistant.add_argument("--host", default="0.0.0.0")
    p_assistant.add_argument("--port", type=int, default=8081)
    p_assistant.set_defaults(func=cmd_serve_assistant)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
