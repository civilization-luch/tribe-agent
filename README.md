# tribe-agent

Self-hosted community agent for Tribe. One codebase, configured by profiles:

- `community` — a user's agent: notifications, announcements, issue execution.
- `platform` — the platform organizer: adds `classify` (feedback triage) and `assistant`.

The agent talks to a Tribe community over GraphQL and hands coding work to a
runtime (opencode now, codex later) inside the user's project repository.

## Install

```bash
pip install tribe-agent          # or: uv tool install tribe-agent
tribe-agent init                 # write config.yaml
tribe-agent doctor               # verify token, API, runtime
tribe-agent run                  # start the scheduler
```

Docker (recommended):

```bash
cd docker
docker compose up -d
```

## How it works

```
community GraphQL  ──►  scanners ──► task queue ──► runtime (opencode) ──► MCP / GraphQL
```

- `scanners` poll a community and enqueue tasks.
- the runtime executes pending tasks with `opencode run`, pointed at the
  repository bound to the task's workspace.
- opencode config (agents/skills) is loaded from the packaged templates via
  `OPENCODE_CONFIG_DIR`, so the user's repository is never modified.

See `config.example.yaml` for all options.

## Credentials

Credentials are provided via environment (`.env` in `docker/`), never baked into the image:

- well-known providers read standard env vars (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, ...);
- custom / OpenAI-compatible providers are declared in `config.yaml` under `opencode.providers`,
  with the key referenced as `{env:VAR}`. The agent generates a runtime opencode config from that
  (passed via `OPENCODE_CONFIG`) — it does not touch the packaged agent/skill templates.

## Persistence

| Data | Location | Survives `up --force-recreate` |
|---|---|---|
| Task queue | `data/agent.db` | yes (data volume) |
| opencode `auth.json` + sessions | `/root/.local/share/opencode` | yes (volume) |
| `config.yaml` | host file (mounted) | yes |

`docker compose down -v` removes the volumes and therefore wipes credentials and sessions.

## Agent namespacing

Our agents and skills are prefixed with `tribe-` (e.g. `tribe-task-executor`,
`tribe-analyze-code`) so they never collide with the host project's own `.opencode/`
agents/skills. The project's agents, skills and `AGENTS.md` remain available to the model,
so the agent follows the repository's own conventions.

