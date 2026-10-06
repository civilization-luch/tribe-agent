---
name: tribe-self-heal
description: Analyzes community metrics and creates issues on anomalies. Use when monitoring community health.
---

Rules (thresholds are configurable per community):

- More than N open unresolved issues → create_issue("Issue backlog growing", labels=["auto", "health"])
- 0 issues created in the last 7 days → create_issue("Community may be inactive", labels=["auto", "engagement"])
- Repeated metric anomalies → create_issue describing the metric and timeframe
- **Do NOT create a duplicate** "Issue backlog growing" (or any health issue) while one is already **open** —
  instead add a comment to the existing open issue with the current metric values and a timestamp.

Use the community MCP `search_issues` to check metrics. Always include the metric values and
a timestamp in the created issue.
