# Trello Cursor Runner

A local, single-host queue that turns released Trello cards into verified pull requests using Cursor CLI. The configured board is polled every minute with the supplied timer. Empty polls make no model calls.

## Assignment

A card must be open, in the configured **In Progress** list, and carry the configured routing label (default: **`ready_for_cursor`**). List and label can be applied in either order. The runner never releases the backlog itself.

Project mappings are local configuration: each card pattern identifies its repository, base branch, project brief/contract, verification commands and required CI checks. Dependencies declared under Dependencies, Dependency links or Depends on must be in the configured Done list. Unmapped projects and missing acceptance criteria are blocked rather than guessed.

The shared delivery contract assigns interactive planning, automatic Cursor execution and explicitly chosen backup execution. Set `skill_dir` to the installed contract's directory; a copy must be accessible in any remote execution environment too.

## Execution and recovery

One OS lock serializes all projects on this host. SQLite records the reservation before the routing label is consumed or Cursor launches. Child commands inherit the lock so a surviving child prevents another wrapper from taking ownership after a crash.

The runner fetches the full task, checklists, attachment metadata, recent comments, project brief and linked execution contract. It creates an isolated Git worktree from the configured remote base. Cursor Auto designs/plans, implements, runs the task's checks and writes an acceptance report. The wrapper independently reruns configured checks, owns commit/push/PR publication and verifies remote PR identity and CI on its exact head. Failed checks get at most one bounded Cursor repair attempt within the overall two-hour run budget.

The owner’s active checkout and uncommitted work are preserved. Cursor is instructed to prepare worktree-local dependencies using the project’s pinned runtime; no production environment files are copied. File/workspace selection and Cursor's `--force` are not a sandbox: this is an explicitly authorized local coding agent running under the owner's account.

Successful acceptance and required CI move a card to the configured Review list with its PR and evidence. The runner never merges or deploys. Configure required CI job names per project. For a project without remote CI, explicitly set `allow_no_ci: true` with an empty `required_checks` list and appropriate local checks. After a discovery grace period, absence of remote checks is reported explicitly rather than claimed as CI success.

Failures preserve artifacts and move the card to Blocked. An interrupted claim is reconciled as blocked, never automatically relaunched. A failed final Trello update retains an outbox state; later polls retry only that update. If the owner moves/archives a card, the runner preserves that decision. Specification edits before publication stop the run for scope reconciliation. Acceptance evidence is supplied by the executor plus configured checks; human review remains responsible for semantic completeness.

## Requirements

- A POSIX host with Python 3.11+ including SQLite, Git, GitHub CLI (`gh`) and Cursor CLI (`cursor-agent`). The runner uses Python's standard library; no pip dependencies are required. The bundled scheduler additionally requires Linux and a running systemd user manager. Windows is not supported directly.
- Cursor authenticated under the account running the service, with access to the configured model and sufficient usage allowance. Check with `cursor-agent status`.
- GitHub authentication that can read the project repositories, push task branches, create/update pull requests and inspect CI. Check `gh auth status` and verify the repository's Git transport works without interactive prompts.
- Trello API key/token with read/write access to the configured board, including cards, labels and comments. Configure the workflow list IDs and create the routing label on the board.
- Existing local Git checkouts with the configured GitHub origins and base branches. Each project supplies its own runtime, dependencies, test tools and verification commands; the runner does not replace those requirements.
- A delivery skill directory containing `SKILL.md` and its referenced files, plus accessible Trello project brief/contract cards and usable acceptance criteria.
- Network access to Trello, GitHub and Cursor, and disk space for retained worktrees and evidence.
- Optional telemetry: a writable node-exporter textfile directory, Prometheus scraping that exporter and Grafana with a Prometheus datasource. These are not required to execute tasks; leave `metrics_file` empty to disable metrics.

## Local configuration

The repository ships only [config.example.json](config.example.json). Board/list IDs, project patterns, repositories, machine paths, check commands and telemetry destinations belong in your own local JSON file:

```sh
mkdir -p ~/.config/trello-cursor-runner
chmod 700 ~/.config/trello-cursor-runner
cp config.example.json ~/.config/trello-cursor-runner/config.json
chmod 600 ~/.config/trello-cursor-runner/config.json
```

Fill in the example before running the poller. Configuration selection, in order:

1. `--config /path/to/local/config.json` before the command.
2. The `TRELLO_CURSOR_CONFIG` environment variable.
3. `$XDG_CONFIG_HOME/trello-cursor-runner/config.json`, or `~/.config/trello-cursor-runner/config.json` when XDG_CONFIG_HOME is unset.

Paths beginning with `~` are expanded for configuration, runtime directories, checkout paths and check-command arguments. An `environment` object can override child-process environment variables when a project's tools require a particular PATH; values are literal strings, not shell commands.

Set `credential_file` to a private environment file containing `TRELLO_API_KEY` and `TRELLO_TOKEN`, or supply those environment variables directly. The credential file is optional when both variables are present. Only those named values are parsed; the file is never sourced or copied. Keep credentials in private local files, not in the example or repository.

Card patterns must match exactly one project. `checks` are argument arrays, not shell command strings; `required_checks` contains exact CI check names. Configure real commands and prerequisites for your project. The task card and project contract can require additional verification.

Local `config.json`, `config.local.json`, `service.env`, credential files named `.env*` and runtime artifacts are Git-ignored. The standard configuration directory is outside the repository entirely. Do not commit your customized configuration or service environment.

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

The units read the script and configuration paths from a local service environment file. Copy the template and replace its placeholders with absolute paths to your checkout and local settings:

```sh
cp systemd/service.env.example ~/.config/trello-cursor-runner/service.env
chmod 600 ~/.config/trello-cursor-runner/service.env
# Edit service.env before continuing.
mkdir -p ~/.config/systemd/user
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

When `metrics_file` is configured, the runner atomically writes Prometheus metrics to that path in your node-exporter textfile directory. It exposes no HTTP endpoint of its own. The file contains low-cardinality numeric metrics only: no card IDs, card text, repository URLs or credentials.

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

Import `grafana/trello-cursor-runner.json` into Grafana or install it in your own dashboard-provisioning source. Select a Prometheus datasource using the dashboard's datasource selector. Dashboard UID: `trello-cursor-runner`. It shows heartbeat age, active work, queue, pause, errors, outcomes and duration. With minute polling, idle heartbeat age should normally stay below two minutes; use heartbeat rather than last queue-read time to monitor a long active run. A stale existing file remains observable through the timestamp when a crash prevents rewriting metrics.

If another process mirrors your provisioned dashboard directory, add the JSON to that process's source tree so synchronization preserves it. Datasource selections, personal dashboard links and provisioning paths belong in your local deployment configuration.

## Validation

```sh
python3 -m unittest discover -s tests -v
python3 runner.py preview
```

Tests cover trigger matching, dependency gates, durable ownership, duplicate suppression, pause, interruption and outcome recovery, changed scope, remote identity, required CI and telemetry. The integration fixture uses a fake Cursor executable and hosted APIs with real Git worktrees, independent checks, commits and a local remote push. It makes no real model calls, Trello changes or GitHub PRs. Verify an actual owner-released card separately for live Cursor delivery.
