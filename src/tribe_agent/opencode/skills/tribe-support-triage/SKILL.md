---
name: tribe-support-triage
description: Processes SupportRequest queue with claim, classification, replies, escalation and conversion to issues
---

# Support triage

Use this skill to process `SupportRequest` records in a community.

## Rules

- A request is not an issue. Do not create an issue until the request is accepted.
- Always claim a request before working on it: `claim_support_request` with a short TTL.
- Public replies go to the author. Internal notes are for the team only.
- Never expose internal notes, source tokens, or issue details to the author.
- Use `needs_info` only with a concrete question.
- Convert accepted requests with `convert_support_request` into the correct workspace.
- Release the claim when finished, or let the TTL expire.

## Flow

1. `get_support_request` for the request.
2. `claim_support_request` with the TTL from the prompt.
3. Classify: bug, feature, question, spam, duplicate.
4. Search for duplicates if the same problem is already known.
5. Reply publicly with the decision or a concrete `needs_info` question.
6. Update status:
   - `triaged` — reviewed;
   - `accepted` — will be worked on;
   - `declined` — out of scope or not planned;
   - `duplicate` — known request;
   - `needs_info` — waiting for the author.
7. If accepted:
   - choose the workspace;
   - `convert_support_request` when auto-convert is enabled;
   - optionally add an internal note with the issue ID.
8. If the request belongs to another team:
   - `escalate_support_request` with the target group and an internal note.
9. `release_support_request_claim`.

## Sources

- `tribe_app` — authenticated or anonymous app users.
- `tribe_landing` — anonymous landing visitors; check `requester.email` / `requester.telegram`.
- `video_review` — external app with server-to-server auth.
