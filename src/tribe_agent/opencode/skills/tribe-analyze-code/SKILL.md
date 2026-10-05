---
name: tribe-analyze-code
description: Reads an assigned issue, analyzes the project repository and produces a solution. Use when implementing issues from the community.
---

Pipeline:

1. **Read the issue** via the community MCP `get_issue` — extract `title`, `description`, `labels`,
   `source.url` (if it is a feedback issue) and any browser logs.

2. **Restore context** from `source.url` when present: map the URL path to a route in the project,
   open the matching component or module and read the browser logs for errors.

3. **Find the related code** in the current repository:
   - Read `AGENTS.md` / `README.md` for conventions and structure.
   - Follow the existing module/folder layout instead of guessing.

4. **Implement the solution**:
   - Edit existing files, add new ones only when necessary.
   - Match the surrounding code style. Do not add comments that only restate the code.

5. **Verify** by running the project's lint and test commands (from the configured commands or inferred
   from the repository).

6. **Commit and report**: `git add -A && git commit -m "type(scope): description"`, then comment on the
   issue with the commit hash and set the status to `resolved`.
