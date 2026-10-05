---
name: tribe-classifier
description: Classifies incoming issues, finds duplicates and triages with labels. Use when processing a batch of new issues.
---

Delegate to the @tribe-classifier agent to triage the batch of issues.

Use the `triaged:*` labels (spam, out-of-scope, duplicate, accepted, needs-info) and always leave a
comment explaining the decision. For feedback issues, inspect `source.url` for context.
