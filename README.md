# Trello Cursor Runner

A local, single-host queue that turns released Trello cards into verified pull requests using Cursor CLI. The Apps board is polled every minute. Empty polls make no model calls.

Workflow reference: https://trello.com/c/gaq6tKcD

## Assignment

A card must be open, in **In Progress**, and labelled exactly **`ready_for_cursor`**. List and label can be applied in either order. The **Ready for Cursor** list does not trigger work. The runner never releases the backlog itself.

The configured card prefix identifies its repository and project brief/contract. Dependencies declared under Dependencies/Dependency links must be Done. Unmapped projects and missing acceptance criteria are blocked rather than guessed.

Currently configured:

| Prefix | Repository | Base |
| --- | --- | --- |
| UNO | agrana/agent-arena | main |
| APP | agrana/free_personal_stack | main |
| AIDASH | agrana/aidash | main |
| RUNNER | agrana/trello-cursor-runner | main |

Codex writes tasks interactively with the owner; Cursor is the automatic executor; Pi is an explicitly assigned backup or model-specific executor. Hermes supports status inspection when asked. The shared local skill is `~/.local/share/agent-skills/trello-delivery/SKILL.md`. A copy of the contract must be installed or passed to any remote execution environment.

## Execution and recovery

One OS lock serializes all projects on this host. SQLite records the reservation before the routing label is consumed or Cursor launches. Child commands inherit the lock so a surviving child prevents another wrapper from taking ownership after a crash.

The runner fetches the full task, checklists, attachment metadata, recent comments, project brief and linked execution contract. It creates an isolated Git worktree from the configured remote base. Cursor Auto designs/plans, implements, runs the task's checks and writes an acceptance report. The wrapper independently reruns configured checks, owns commit/push/PR publication and verifies remote PR identity and CI on its exact head. Failed checks get at most one bounded Cursor repair attempt within the overall two-hour run budget.

The owner’s active checkout and uncommitted work are preserved. Cursor is instructed to prepare worktree-local dependencies using the project’s pinned runtime; no production environment files are copied. File/workspace selection and Cursor's `--force` are not a sandbox: this is an explicitly authorized local coding agent running under the owner's account.

Successful acceptance and required CI move a card to Review with its PR and evidence. The runner never merges or deploys. UNO/APP require the App Quality job. AIDASH currently has no remote workflow, so its independently rerun pytest suite is the configured gate; absence of CI is explicitly reported after a discovery grace period rather than claimed as CI success.

Failures preserve artifacts and move the card to Blocked. An interrupted claim is reconciled as blocked, never automatically relaunched. A failed final Trello update retains an outbox state; later polls retry only that update. If the owner moves/archives a card, the runner preserves that decision. Specification edits before publication stop the run for scope reconciliation. Acceptance evidence is supplied by the executor plus configured checks; human review remains responsible for semantic completeness.

## Requirements and configuration

- Python 3.11+ (standard library only), Git, authenticated GitHub CLI and authenticated Cursor CLI.
- Existing local checkouts with the configured GitHub origin and base branch.
- The shared skill at `skill_dir` in `config.json`.
- Trello key/token in the existing `credential_file`, or `TRELLO_API_KEY`/`TRELLO_TOKEN` environment variables. Only those named values are parsed; the environment file is never sourced or copied. Secrets stay out of Git, prompts and metrics.

`config.json` contains this host's non-secret paths, board/list/project mappings, model, timeouts, independent checks and metrics destination. Adapt it when installing elsewhere. Prefixes must map to exactly one project. Setup/verification details in the latest card and project contract still apply.

## Commands

From this repository (or pass an absolute script path):

```sh
python3 runner.py preview       # Live queue, no Trello writes or model calls
python3 runner.py status        # Local run history, outcomes and artifact locations
python3 runner.py run           # Reconcile outcomes, then handle at most one card
python3 runner.py pause         # Stop pickups; active command stops on the pause check
python3 runner.py resume        # Allow pickups; does not requeue blocked cards
python3 runner.py retry CARD_ID # Deliberately lift local suppression after inspection
python3 runner.py handoff CARD_ID # Release a stopped terminal run for manual continuation
```

After `retry`, explicitly restore In Progress and `ready_for_cursor` on the card. A fresh run preserves the old worktree and starts from the remote base; inspect existing work first. If a PR already exists, `retry` refuses a duplicate implementation: use `handoff` and continue the preserved branch/PR with the explicitly chosen executor/model. These controls require the lock and cannot transfer ownership while an old command remains active.

The pause marker persists across service restarts. Removing a routing label is not a cancellation mechanism for a card already claimed. To stop all work immediately, stop the service (its cgroup includes child processes); the next reconciliation reports the interrupted attempt.

## Service installation

The units assume the repository lives at `~/Documents/code/trello-cursor-runner`:

```sh
install -m 644 systemd/trello-cursor-runner.service ~/.config/systemd/user/
install -m 644 systemd/trello-cursor-runner.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now trello-cursor-runner.timer
systemctl --user start trello-cursor-runner.service
systemctl --user list-timers trello-cursor-runner.timer
journalctl --user -u trello-cursor-runner.service -n 30
```

The timer uses a minute calendar schedule with Persistent enabled. systemd does not overlap the same active oneshot service; the independent OS lock also protects manual invocations. The service has a 125-minute hard limit around the runner's two-hour budget. Host sleep/offline time pauses execution; persistent timers reconcile when the user manager is running again. This is a user service, not a cloud availability guarantee.

SQLite, prompts, reports, step logs, PR snapshots and preserved worktrees live under `~/.local/state/trello-cursor-runner/`, outside the repository. State is private to the owner. Maintain the database and artifacts together; deleting claims without reconciling remote work can cause duplicate assignments. Automatic cleanup is intentionally absent.

## Grafana and Prometheus

The runner atomically writes `trello_cursor_runner.prom` into the existing node-exporter textfile directory configured by `metrics_file`. No extra server or container is needed. The file contains low-cardinality numeric metrics only: no card IDs, card text, repository URLs or credentials.

| Metric | Meaning |
| --- | --- |
| `trello_cursor_queue_depth` | Released cards not suppressed by an existing claim |
| `trello_cursor_active_runs` | Currently executing run (0 or 1) |
| `trello_cursor_paused` | Persistent pause marker |
| `trello_cursor_heartbeat_timestamp_seconds` | Last wrapper heartbeat; refreshed at least every 30 seconds during commands |
| `trello_cursor_last_successful_poll_timestamp_seconds` | Last successful queue read |
| `trello_cursor_last_poll_success` | Last poll's API/runner health (task blockers are separate outcomes) |
| `trello_cursor_poll_errors_total` | Failed polling invocations |
| `trello_cursor_runs_started_total` | Durable reservations created |
| `trello_cursor_run_transitions_total{status}` | Monotonic state-transition counters, including Review and Blocked |
| `trello_cursor_runs{status}` | Retained run records grouped by their current state |
| `trello_cursor_last_run_duration_seconds` | Most recently completed execution duration |

`grafana/trello-cursor-runner.json` uses the existing datasource UID `prometheus`. Install it in the existing Grafana provisioned dashboards directory. Dashboard UID: `trello-cursor-runner`. It shows heartbeat age, active work, queue, pause, errors, outcomes and duration. Idle heartbeat age should normally stay below two minutes; use heartbeat rather than last queue-read time to monitor a long active run. A stale existing file remains observable through the timestamp when a crash prevents rewriting metrics.

On this host, `monitoring-dashboard-git-sync` mirrors `~/Documents/code/home-monitoring/grafana/dashboards/` with deletion enabled. The dashboard must also exist at that source path to survive sync. The installed copies are `Monitoring/trello-cursor-runner.json` in both the source tree and the provisioned directory. Keep them synchronized with the version in this repository when changing the dashboard.

## Validation

```sh
python3 -m unittest discover -s tests -v
python3 runner.py preview
```

Tests cover trigger matching, dependency gates, durable ownership, duplicate suppression, pause, interruption and outcome recovery, changed scope, remote identity, required CI and telemetry. The integration fixture uses a fake Cursor executable and hosted APIs with real Git worktrees, independent checks, commits and a local remote push. It makes no real model calls, Trello changes or GitHub PRs. Verify an actual owner-released card separately for live Cursor delivery.
