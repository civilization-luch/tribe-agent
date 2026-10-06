# tribe-agent

Self-hosted community agent for Tribe. One codebase, configured by profiles:

- `community` — a user's agent: notifications, announcements, issue execution.
- `platform` — the platform organizer: adds `classify` (feedback triage) and `assistant`.

The agent talks to a Tribe community over GraphQL and hands coding work to a
runtime (opencode now, codex later) inside the user's project repository.

## Install

```bash
git clone https://github.com/civilization-luch/tribe-agent
cd tribe-agent
uv venv && uv pip install -e .          # or: pip install .
tribe-agent init                 # write config.yaml
tribe-agent doctor               # verify token, API, runtime
tribe-agent run                  # start the scheduler
```

Docker (recommended):

```bash
cd docker
docker compose up -d
```

## Onboarding the bot

The agent acts as a community **bot member**. Onboarding is done manually through the
platform — the agent only needs the bot id and token.

1. **Create the bot** — `My Bots` → "Create bot" (choose a type). Copy the **Bot ID** and the
   **API key** (token).
2. **Invite it to the community** — Community → Members → Invite → "By user ID" → paste the
   **Bot ID**, choose role `member`, send the invitation.
3. **Accept the invitation as the bot** — `My Bots` → "Switch to bot" (switches the session to the
   bot), then accept the pending invitation (`My Invitations` / the invite link). For communities
   with `access_level: open` the bot can simply join.
4. **Configure the agent** — put the Bot ID into `community.user_id` and the token into
   `community.bot_token`.

Membership matters: permissions such as `comment.create`, `issue.update` and `issue.assign`
require an **active member**, so the agent cannot act in a community the bot has not joined.

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

## Repository resolution

A task reaches a repository through this chain (first match wins):

1. `module_settings.executor.repos[<workspace_id>]` — local per-workspace override;
2. `workspace(id).repoUrl` — the workspace-bound repository
   (`metadata.repository` in the platform);
3. `repo.default_url` — the agent's **single repository**, a fallback for any
   workspace without its own binding (this is the classic "platform repository"
   the agent used to know);
4. `project_path` — a manual working directory as a last fallback.

This way the platform agent can keep working against ONE repository while
communities still run per-workspace repositories.

## Pushing work

By default the executor commits changes only inside its local clone
(`data/repos/<workspace>`) and comments on the issue with the commit hash.
To push the commits to the workspace repository, enable it per config:

```yaml
module_settings:
  executor:
    push: true            # push after a successful opencode run
    push_mode: direct     # direct | branch
    branch_prefix: tribe
    open_pr: false        # open a GitHub PR (branch mode only)
    push_comment: true
```

- `push_mode: direct` pushes to the branch the workspace is bound to
  (the `metadata.repository.branch`, or the clone's current branch).
- `push_mode: branch` creates a feature branch
  (`<branch_prefix>/<workspace>/i<issue>`), runs the work there and pushes it.
- `open_pr: true` opens a GitHub pull request after a branch push — the
  repository URL must be `github.com` and `repo.git_token` must have
  `pull_requests: write` scope.
- HTTPS remotes authenticate with `repo.git_token` (already embedded in the
  clone's `origin`); local/file remotes need nothing; SSH remotes are not
  supported yet.
- A failed push does not mark the task as failed — the committed work stays
  local and a warning is logged.

## Agent namespacing

Our agents and skills are prefixed with `tribe-` (e.g. `tribe-task-executor`,
`tribe-analyze-code`) so they never collide with the host project's own `.opencode/`
agents/skills. The project's agents, skills and `AGENTS.md` remain available to the model,
so the agent follows the repository's own conventions.

