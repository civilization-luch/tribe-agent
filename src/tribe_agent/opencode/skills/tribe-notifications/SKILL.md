---
name: tribe-notifications
description: Processes pending notifications (mentions, assignments, reviews). Use when handling the notification batch.
---

Delegate to the @tribe-notifications agent to process the batch of notifications.

Process each notification according to its type (UserMentioned, IssueAssigned, IssueResolved, ...)
and finish every item completely before moving on.

The notifications agent is NOT an implementer: it must never post solution plans or ask for approval.
An `IssueAssigned` issue is handled automatically by @tribe-task-executor — at most acknowledge it.
Implementation and "fixed/deployed" claims belong exclusively to @tribe-task-executor/the deployer.
