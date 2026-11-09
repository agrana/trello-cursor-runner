---
name: runner-execution
description: Implement a task or review follow-up supplied by this board-driven runner, using its isolated worktree and acceptance-report contract. Use when invoked by the runner, not for dispatching work or managing the backlog.
---

# Runner execution contract

The runner supplies the task context, worktree, report destination and execution mode. Loading this skill starts no poller and assigns no task. Follow the actual runner prompt and repository instructions; preserve the owner's explicit scope and authorization.

## Ownership

The runner owns the claim, routing label, task status, Git branch creation, commit, push, PR publication, review replies and discussion resolution. Prepare code changes and evidence without performing those writes yourself. Do not start another executor or take over another task.

Task release authorizes implementation through a review request. It does not authorize merging, deploying, changing secrets/shared infrastructure or paid live experiments without their applicable authorization. Credentials must remain outside prompts, repository files and evidence logs.

## Implementation

Read the full supplied task context, current acceptance criteria, dependency state, project brief and execution contract. Inspect repository identity, instructions, runtime and existing code before choosing the design. Treat missing decisions, unresolved dependencies and conflicting requirements as blockers rather than reducing scope.

Work only in the supplied isolated worktree. Preserve the source checkout and unrelated changes. Prepare worktree-local dependencies using the project's pinned runtime; do not copy production environment files or change the source checkout's virtual environment.

Write the short plan to the runner's requested artifact path. Implement within the agreed scope, run the task's required checks and demonstrate each current acceptance criterion. Record actual results and distinguish local fixtures from integration or live evidence. Repair actionable failures within the assigned scope and deadline.

## Review follow-up

Read the original assignment, current contract and exact supplied reviewer feedback. Reuse the supplied worktree and branch. Keep fixes within the original scope and verify them; disputed requests, changed scope and missing product decisions remain blockers.

Provide an evidence-backed explanation for each addressed feedback item at the runner's requested destination. Do not reply remotely, resolve discussions, reset branches, force-push or create another PR yourself.

## Report

Write the JSON report to the destination supplied by the runner, outside the repository:

```json
{
  "status": "complete",
  "summary": "Concrete resulting behavior",
  "acceptance": [
    {"criterion": "Observable outcome", "passed": true, "evidence": "Actual command or observation and result"}
  ],
  "checks": ["Actual commands and results"],
  "limitations": [],
  "blocker": ""
}
```

Use `blocked` when acceptance is incomplete. Retain completed work and describe the failure or smallest missing decision, including the actual command/result when relevant. Never claim checks passed if they were absent, skipped or not run. The runner independently verifies checks and publication; a complete report alone does not establish delivery or Done.
