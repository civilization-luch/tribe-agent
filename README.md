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
