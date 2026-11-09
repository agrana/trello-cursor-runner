# Board-driven coding runner

The runner turns explicitly released board tasks into verified code changes. The board holds the specification and visible workflow; the runner holds durable ownership and recovery state; the coding agent implements the task; the Git host holds the review and CI evidence.

## Support today

| Integration | Implemented | Work adaptation to build |
| --- | --- | --- |
| Task source | Trello API | Jira board and issues through the configured `twg` integration |
| Executor | Cursor CLI | Kiro with its actual supported noninteractive invocation |
| Code review | GitHub through `gh` | GitLab merge requests through `glab` |
| Scheduling | Linux systemd user timer | Reuse on a compatible host, or supply another scheduler |
| Ownership and recovery | POSIX process lock, SQLite, preserved worktrees | Retain the same guarantees |

`twg` and `glab` are tools supplied by the intended work environment. This document does not prescribe unverified `twg` commands or assume a particular Jira deployment or Kiro CLI syntax. Verify their installed help, authentication and machine-readable output before implementing adapters.

The current code calls Trello, Cursor and GitHub directly. These are proposed integration boundaries, not an existing plugin API. A Jira board cannot yet be used by changing configuration alone.

## Delivery lifecycle

1. Poll the configured board. A task must be open, in the configured active status and explicitly released using the routing label. The current default is `ready_for_cursor`. Never choose work from the backlog automatically.
2. Re-read eligibility and check project mapping, repository, brief, execution contract, acceptance criteria and completed dependencies.
3. Acquire ownership under the host lock and persist a reservation before consuming the label or launching the executor. Record task identity, run ID, executor/model, branch, worktree and timestamps.
4. Fetch the configured remote base and create an isolated worktree. Pass the current specification and context to the agent.
5. Let the agent inspect the repository, design, plan, implement and verify within scope. The runner independently reruns configured checks and bounds repair attempts and total execution time.
6. The runner owns commit, push and PR/MR publication. Verify remote repository, source/target branches, head commit and required CI before declaring Review-ready. Link the task and PR/MR in both directions and post acceptance evidence.
7. Poll linked review requests. New published feedback from configured trusted reviewers can resume the same branch, worktree and PR/MR without another initial release label. Debounce and fingerprint feedback, bound review rounds, and reply with verified evidence.
8. After merge, require CI evidence on the final review head and merged commit before Done. An explicitly configured no-CI policy may accept absent checks, but must report their absence. Existing failed/pending checks, missing required jobs and skipped/neutral-only results still prevent completion.

The runner never merges or deploys automatically. Task release authorizes scoped implementation through publication for review; it does not extend the assignment to production activation, secret changes or unrelated infrastructure.

## Three integration boundaries

### Tracker: Trello or Jira

Normalize a task to its provider-qualified ID, URL, title, current specification, acceptance criteria, dependencies, project context, status, routing labels and comments. Preserve source IDs and a specification fingerprint for change detection.

Provide operations for listing eligible tasks, fetching full current context, consuming the release label, posting evidence and moving through configured workflow states. Persist outcome evidence before remote writes and reconcile uncertain writes with stable run markers.

For Jira, configure the board and filter scope, active/review/blocked/done status mappings, relevant custom fields and dependency conventions. Resolve transitions available to the current issue rather than treating a status name as a writable field. Handle pagination and board-specific workflow differences. Preview should explain readiness and mapping without changing issues or launching a model.

### Executor: Cursor or Kiro

Accept the worktree, context artifact, execution contract, model selection and deadline. Return an acceptance report containing status, summary, per-criterion evidence, actual checks/results, limitations and any blocker.

For Kiro, verify its headless execution mode, authentication, permission handling, output format, cancellation and exit behavior. Changing `cursor_binary` is insufficient because the current runner constructs Cursor-specific arguments and prompts.

Keep one publisher: this design assigns Git publication and tracker updates to the runner. Kiro can use available read tools, including `twg` and `glab`, while leaving those writes to the runner. If publication ownership changes, update both the executor instructions and orchestration together.

### Forge: GitHub or GitLab

Normalize repository identity, review-request identity, branches, head/merge SHAs, review feedback, discussions and CI state. Support finding an existing request before creating one, publishing verified updates, replying to feedback and resolving addressed discussions after checks pass.

For GitLab, use `glab` and the actual host/project identity, including nested groups. Explicitly map reviewer trust, discussion resolution, pagination, pipelines/jobs, merge strategies and CI evidence to GitLab behavior; GitHub association names are not portable. Check freshness before replying or resolving a discussion.

## Recovery guarantees

- A single host runs one executor at a time. Multiple hosts would need shared atomic claims; a task comment or label alone is not a lock.
- Empty polls and unchanged feedback do not launch a model.
- Interrupted or blocked implementation preserves evidence and requires deliberate retry or handoff. A recorded timestamp alone does not prove the previous child process stopped.
- Persisted outboxes retry tracker updates, replies and resolutions without repeating implementation. Reconcile uncertain remote writes before retrying.
- Specification changes before publication stop work for scope reconciliation. Respect owner moves and archived tasks.
- Follow-ups preserve the existing branch and PR/MR. Unexpected head changes or a dirty worktree require reconciliation, not reset or force-push.
- Closed without merge becomes Blocked. After merge, failed CI becomes Blocked while CI reruns remain monitored; no implementation starts automatically after merge.

## Work installation plan

Keep the work installation's configuration, credentials, database, worktrees and scheduler separate from the personal installation. Do not share a claim database between differently configured instances.

1. Establish Jira deployment/board scope, `twg` capabilities, GitLab host/project, workflow mappings, acceptance fields, dependency links, Kiro invocation, required jobs and trusted reviewers.
2. Introduce the integration boundaries while preserving Trello/Cursor/GitHub behavior. Include provider and instance identity in durable task/review keys.
3. Implement Jira read-only preview first, then ownership, Kiro execution, GitLab publication, evidence and Jira transitions.
4. Implement trusted feedback and merge/CI reconciliation explicitly.
5. Verify eligibility changes, duplicate suppression, crash recovery, changed scope, pagination, uncertain writes and failed/missing CI with fake providers and real local Git worktrees.
6. Run a live read-only preview before scheduling. Release one selected issue for end-to-end delivery before expanding the queue.

The target setup is a poller pointing at a Jira board, Kiro implementing released issues, and GitLab MRs providing review and CI evidence. These adapters remain future work.
