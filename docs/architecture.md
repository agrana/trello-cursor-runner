# Board-driven coding runner

The runner turns explicitly released board tasks into verified code changes. The board holds the specification and visible workflow; the runner holds durable ownership and recovery state; the coding agent implements the task; the Git host holds the review and CI evidence.

## Support today

| Integration | Implemented | Work adaptation to build |
| --- | --- | --- |
| Task source | Trello API | Jira board and issues through the configured `twg` integration |
| Executor | Cursor CLI and Kiro CLI | Additional executors only with an explicit invocation adapter |
| Code review | GitHub through `gh` | GitLab merge requests through `glab` |
| Scheduling | Linux systemd user timer | Reuse on a compatible host, or supply another scheduler |
| Ownership and recovery | POSIX process lock, SQLite, preserved worktrees | Retain the same guarantees |

`twg` and `glab` are tools supplied by the intended work environment. This document does not prescribe unverified `twg` commands or assume a particular Jira deployment. Verify their installed help, authentication and machine-readable output before implementing adapters.

The current code calls Trello, Cursor, Kiro and GitHub directly. These are proposed integration boundaries, not an existing plugin API. A Jira board cannot yet be used by changing configuration alone. Jira and GitLab remain future work.

## Delivery lifecycle

1. Poll the configured board. A task must be open, in the configured active status and explicitly released using exactly one configured executor routing label. Defaults are `ready_for_cursor` and `ready_for_kiro`. Never choose work from the backlog automatically. Ambiguous multi-executor labels fail closed.
2. Re-read eligibility and check project mapping, repository, brief, execution contract, acceptance criteria and completed dependencies.
3. Acquire ownership under the shared host lock and persist a reservation before consuming the label or launching the executor. Record task identity, run ID, executor/model (and optional agent), branch, worktree and timestamps. Historical records without an executor field default to Cursor.
4. Fetch the configured remote base and create an isolated worktree. Pass the current specification and context to the agent.
5. Let the agent inspect the repository, design, plan, implement and verify within scope. The runner independently reruns configured checks and bounds repair attempts and total execution time, always with the recorded executor.
6. The runner owns commit, push and PR/MR publication. Verify remote repository, source/target branches, head commit and required CI before declaring Review-ready. Link the task and PR/MR in both directions and post acceptance evidence. Persist executor identity on the PR link.
7. Poll linked review requests. New published feedback from configured trusted reviewers can resume the same branch, worktree, PR/MR and original executor without another initial release label. Debounce and fingerprint feedback, bound review rounds, and reply with verified evidence.
8. After merge, require CI evidence on the final review head and merged commit before Done. An explicitly configured no-CI policy may accept absent checks, but must report their absence. Existing failed/pending checks, missing required jobs and skipped/neutral-only results still prevent completion.

The runner never merges or deploys automatically. Task release authorizes scoped implementation through publication for review; it does not extend the assignment to production activation, secret changes or unrelated infrastructure.

## Three integration boundaries

### Tracker: Trello or Jira

Normalize a task to its provider-qualified ID, URL, title, current specification, acceptance criteria, dependencies, project context, status, routing labels and comments. Preserve source IDs and a specification fingerprint for change detection.

Provide operations for listing eligible tasks, fetching full current context, consuming the release label, posting evidence and moving through configured workflow states. Persist outcome evidence before remote writes and reconcile uncertain writes with stable run markers.

For Jira, configure the board and filter scope, active/review/blocked/done status mappings, relevant custom fields and dependency conventions. Resolve transitions available to the current issue rather than treating a status name as a writable field. Handle pagination and board-specific workflow differences. Preview should explain readiness and mapping without changing issues or launching a model.

### Executor: Cursor or Kiro

Accept the worktree, context artifact, execution contract, model selection and deadline. Return an acceptance report containing status, summary, per-criterion evidence, actual checks/results, limitations and any blocker.

Executors are configured explicitly under `executors`, mapping name → label/binary/model/optional agent. Legacy `ready_label` / `cursor_binary` / `model` synthesize a Cursor-only map. One queue, store and host lock are shared. There is no silent fallback between executors or models.

Kiro uses its supported noninteractive invocation:

`kiro-cli chat --no-interactive --trust-all-tools --trust-workspace --output-format stream-json [--model MODEL] [--agent AGENT] INPUT`

with the isolated worktree as cwd. An unset Kiro model means the CLI default and is recorded as such; Cursor's `auto` must never be passed to Kiro.

Keep one publisher: this design assigns Git publication and tracker updates to the runner. Executors can use available read tools while leaving those writes to the runner. If publication ownership changes, update both the executor instructions and orchestration together.

### Forge: GitHub or GitLab

Normalize repository identity, review-request identity, branches, head/merge SHAs, review feedback, discussions and CI state. Support finding an existing request before creating one, publishing verified updates, replying to feedback and resolving addressed discussions after checks pass.

For GitLab, use `glab` and the actual host/project identity, including nested groups. Explicitly map reviewer trust, discussion resolution, pagination, pipelines/jobs, merge strategies and CI evidence to GitLab behavior; GitHub association names are not portable. Check freshness before replying or resolving a discussion.

## Recovery guarantees

- A single host runs one executor process at a time under the shared lock. Multiple hosts would need shared atomic claims; a task comment or label alone is not a lock.
- Empty polls and unchanged feedback do not launch a model.
- Interrupted or blocked implementation preserves evidence and requires deliberate retry or handoff. A recorded timestamp alone does not prove the previous child process stopped.
- Persisted outboxes retry tracker updates, replies and resolutions without repeating implementation. Reconcile uncertain remote writes before retrying them.
- Specification changes before publication stop work for scope reconciliation. Respect owner moves and archived tasks.
- Follow-ups preserve the existing branch, PR/MR and original executor/model. Unexpected head changes or a dirty worktree require reconciliation, not reset or force-push.
- Closed without merge becomes Blocked. After merge, failed CI becomes Blocked while CI reruns remain monitored; no implementation starts automatically after merge.
- Stable comment/PR markers keep their historical identities so existing runs continue to deduplicate correctly.

## Work installation plan

Keep the work installation's configuration, credentials, database, worktrees and scheduler separate from the personal installation. Do not share a claim database between differently configured instances.

1. Establish Jira deployment/board scope, `twg` capabilities, GitLab host/project, workflow mappings, acceptance fields, dependency links, required jobs and trusted reviewers.
2. Introduce the tracker/forge integration boundaries while preserving Trello/Cursor/Kiro/GitHub behavior. Include provider and instance identity in durable task/review keys.
3. Implement Jira read-only preview first, then ownership, GitLab publication, evidence and Jira transitions.
4. Implement trusted feedback and merge/CI reconciliation explicitly for GitLab.
5. Verify eligibility changes, duplicate suppression, crash recovery, changed scope, pagination, uncertain writes and failed/missing CI with fake providers and real local Git worktrees.
6. Run a live read-only preview before scheduling. Release one selected issue for end-to-end delivery before expanding the queue.

The target work setup is a poller pointing at a Jira board with GitLab MRs providing review and CI evidence. Those adapters remain future work. Cursor and Kiro already share the Trello + GitHub path through explicit executor configuration.
