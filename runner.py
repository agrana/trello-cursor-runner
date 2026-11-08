#!/usr/bin/env python3
"""Single-host Trello queue -> Cursor CLI -> verified GitHub PR. Stdlib only."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"
FIELDS = "id,name,desc,idBoard,idList,idLabels,labels,closed,pos,shortUrl"
TERMINAL = {"blocked", "interrupted", "review", "handoff", "cancelled"}


class Failure(Exception):
    pass


class Repairable(Failure):
    pass


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def load_credentials(path):
    result = {}
    if Path(path).exists():
        for line in Path(path).read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and key in {"TRELLO_API_KEY", "TRELLO_TOKEN"}:
                value = value.strip()
                if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                result[key] = value
    for key in ("TRELLO_API_KEY", "TRELLO_TOKEN"):
        if os.environ.get(key):
            result[key] = os.environ[key]
        if not result.get(key):
            raise Failure(f"Missing {key}; configure the secure credential source")
    return result


class Trello:
    def __init__(self, credentials):
        self.credentials = credentials

    def request(self, method, path, **params):
        # Never include credentials, a raw HTTPError or response body in diagnostics.
        auth = {"key": self.credentials["TRELLO_API_KEY"],
                "token": self.credentials["TRELLO_TOKEN"]}
        url = "https://api.trello.com/1" + path + "?" + urllib.parse.urlencode(auth)
        data = None
        if method == "GET":
            if params:
                url += "&" + urllib.parse.urlencode(params)
        elif params:
            data = urllib.parse.urlencode(params).encode()
        request = urllib.request.Request(url, data=data, method=method)
        for attempt in range(3 if method == "GET" else 1):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    raw = response.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as exc:
                if method == "GET" and exc.code in {429, 500, 502, 503, 504} and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise Failure(f"Trello {method} {path}: HTTP {exc.code}") from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if method == "GET" and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise Failure(f"Trello {method} {path}: network request failed") from None
        raise Failure("Trello request exhausted retries")

    def card(self, card_id, full=False):
        params = {"fields": FIELDS}
        if full:
            params.update(checklists="all", attachments="true", attachment_fields="name,url,mimeType")
        return self.request("GET", f"/cards/{card_id}", **params)

    def comment_once(self, card_id, marker, message):
        actions = self.request("GET", f"/cards/{card_id}/actions", filter="commentCard", limit=1000)
        if not any(marker in action.get("data", {}).get("text", "") for action in actions):
            self.request("POST", f"/cards/{card_id}/actions/comments", text=f"{marker}\n{message}"[:16000])


def eligible(card, config):
    return (not card.get("closed") and card.get("idBoard") == config["board_id"]
            and card.get("idList") == config["lists"]["in_progress"]
            and any(label.get("name") == config["ready_label"] for label in card.get("labels", [])))


def project_for(card, config):
    matches = [p for p in config["projects"] if re.search(p["card_pattern"], card["name"])]
    if len(matches) != 1:
        raise Failure("Card needs exactly one configured project/repository mapping")
    return matches[0]


def dependency_cards(card, cards, config):
    sections = re.findall(r"(?ims)^#{1,6}\s+Dependenc(?:ies|y links)\s*\n(.*?)(?=^#{1,6}\s|\Z)", card.get("desc", ""))
    text = "\n".join(sections)
    keys = set(re.findall(r"\b[A-Z][A-Z0-9]*-\d+\b", text))
    short_ids = set(re.findall(r"https://trello\.com/c/([A-Za-z0-9]+)", text))
    resolved = {}
    for key in sorted(keys):
        matches = [c for c in cards if re.match(r"^\[" + re.escape(key) + r"\]", c["name"])]
        if len(matches) != 1:
            raise Failure(f"Dependency {key} is missing or ambiguous")
        resolved[matches[0]["id"]] = matches[0]
    for short_id in short_ids:
        matches = [c for c in cards if c.get("shortUrl", "").rstrip("/").endswith("/" + short_id)]
        if len(matches) != 1:
            raise Failure(f"Dependency link {short_id} is missing or outside the configured board")
        resolved[matches[0]["id"]] = matches[0]
    if text.strip() and not keys and not short_ids and not re.search(r"(?i)\bnone\b|^\s*n/a\s*$", text):
        raise Failure("Dependencies need explicit card keys/links or None")
    for dep in resolved.values():
        if dep["idList"] != config["lists"]["done"]:
            raise Failure(f"Dependency is not Done: {dep['name']}")
    return list(resolved.values())


def remote_repository(url):
    match = re.fullmatch(r"(?:git@github\.com:|https://github\.com/)([\w.-]+/[\w.-]+?)(?:\.git)?/?", url.strip())
    return match[1] if match else None


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY, card_id TEXT NOT NULL, status TEXT NOT NULL,
            started TEXT NOT NULL, updated TEXT NOT NULL, data TEXT NOT NULL)""")
        self.db.execute("CREATE INDEX IF NOT EXISTS card_runs ON runs(card_id, started)")
        self.db.execute("CREATE TABLE IF NOT EXISTS transitions (id INTEGER PRIMARY KEY, status TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.commit()

    def all(self):
        return [dict(row) | json.loads(row["data"]) for row in self.db.execute("SELECT * FROM runs ORDER BY rowid")]

    def latest(self, card_id):
        rows = [row for row in self.all() if row["card_id"] == card_id]
        return rows[-1] if rows else None

    def save(self, run):
        run["updated"] = now()
        previous = self.db.execute("SELECT status FROM runs WHERE run_id=?", (run["run_id"],)).fetchone()
        if previous is None or previous["status"] != run["status"]:
            self.db.execute("INSERT INTO transitions(status) VALUES (?)", (run["status"],))
        self.db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?)",
                        (run["run_id"], run["card_id"], run["status"], run["started"], run["updated"], json.dumps(run)))
        self.db.commit()

    def close(self):
        self.db.close()

    def metadata(self):
        row = self.db.execute("SELECT value FROM metadata WHERE key='telemetry'").fetchone()
        return json.loads(row[0]) if row else {}

    def save_metadata(self, value):
        self.db.execute("INSERT OR REPLACE INTO metadata VALUES ('telemetry', ?)", (json.dumps(value),))
        self.db.commit()


@contextlib.contextmanager
def process_lock(state):
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / "runner.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Failure("Runner busy; another process still owns the lock") from None
        yield handle.fileno()


class Runner:
    def __init__(self, config, api, store=None, lock_fd=None):
        self.config, self.api, self.store, self.lock_fd = config, api, store, lock_fd
        self.state = Path(config["state_dir"]).expanduser()
        self.step = 0
        self.deadline = None
        self.last_heartbeat = 0

    def telemetry(self, poll=False, success=False, error=False, queue=None):
        path = self.config.get("metrics_file")
        if not path or not self.store:
            return
        stamp = time.time()
        meta = self.store.metadata()
        meta["heartbeat"] = stamp
        if poll:
            meta["poll_attempt"] = stamp
        if success:
            meta.update(poll_success=stamp, last_poll_ok=1)
        if error:
            meta["poll_errors"] = meta.get("poll_errors", 0) + 1
            meta["last_poll_ok"] = 0
        if queue is not None:
            meta["queue_depth"] = queue
        self.store.save_metadata(meta)
        rows = self.store.all()
        statuses = {"running", "blocked_pending", "review_pending", "review", "blocked", "cancelled", "released", "handoff", "interrupted"}
        transitions = dict(self.store.db.execute("SELECT status,COUNT(*) FROM transitions GROUP BY status"))
        lines = []
        def metric(name, kind, help_text, value=None, samples=None):
            full = "trello_cursor_" + name
            lines.extend([f"# HELP {full} {help_text}", f"# TYPE {full} {kind}"])
            if samples is not None:
                lines.extend(f'{full}{{status="{status}"}} {sample}' for status, sample in samples)
            else:
                lines.append(f"{full} {value}")
        metric("heartbeat_timestamp_seconds", "gauge", "Last runner heartbeat in Unix seconds.", stamp)
        metric("last_poll_timestamp_seconds", "gauge", "Last polling invocation in Unix seconds.", meta.get("poll_attempt", 0))
        metric("last_successful_poll_timestamp_seconds", "gauge", "Last successful Trello queue read.", meta.get("poll_success", 0))
        metric("last_poll_success", "gauge", "Whether the last polling invocation succeeded.", meta.get("last_poll_ok", 0))
        metric("poll_errors_total", "counter", "Polling invocations that failed.", meta.get("poll_errors", 0))
        metric("queue_depth", "gauge", "Labelled In Progress cards not suppressed by an existing run.", meta.get("queue_depth", 0))
        metric("paused", "gauge", "Whether execution is paused.", int(self.paused()))
        metric("active_runs", "gauge", "Runs currently executing under the single-host lock.", sum(r["status"] == "running" for r in rows))
        metric("runs_started_total", "counter", "Durable run reservations created.", len(rows))
        metric("run_transitions_total", "counter", "Recorded run state transitions; repeated saves do not increment.",
               samples=[(s, transitions.get(s, 0)) for s in sorted(statuses)])
        metric("runs", "gauge", "Retained run records grouped by current state.",
               samples=[(s, sum(r["status"] == s for r in rows)) for s in sorted(statuses)])
        finished = [r for r in rows if r.get("finished")]
        last = max(finished, key=lambda r: r["finished"]) if finished else None
        duration = (dt.datetime.fromisoformat(last["finished"]) - dt.datetime.fromisoformat(last["started"])).total_seconds() if last else 0
        metric("last_run_duration_seconds", "gauge", "Duration of the most recently finished execution before final Trello reconciliation.", max(0, duration))
        destination = Path(path).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=destination.parent, prefix=".trello-cursor-", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                output.write("\n".join(lines) + "\n")
                output.flush()
                os.fsync(output.fileno())
                # The existing node-exporter must be able to read this non-secret metrics file.
                os.fchmod(output.fileno(), 0o644)
            os.replace(temporary, destination)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
        self.last_heartbeat = time.monotonic()

    def paused(self):
        return (self.state / "PAUSED").exists()

    def command(self, argv, cwd=None, run=None, timeout=120, interruptible=False):
        env = os.environ.copy()
        env.update(self.config.get("environment", {}))
        for key in ("TRELLO_API_KEY", "TRELLO_TOKEN"):
            env.pop(key, None)
        env["GIT_TERMINAL_PROMPT"] = "0"
        self.step += 1
        log_path = Path(run["run_dir"]) / f"step-{self.step:03d}.log" if run else None
        with (log_path.open("w+") if log_path else tempfile.TemporaryFile(mode="w+")) as output:
            process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
                                       pass_fds=(self.lock_fd,) if self.lock_fd is not None else ())
            deadline = min(time.monotonic() + timeout, self.deadline or float("inf"))
            try:
                while process.poll() is None:
                    if time.monotonic() - self.last_heartbeat >= 30:
                        self.telemetry()
                    if interruptible and self.paused():
                        raise Failure("Runner paused; active command stopped. Explicit retry/handoff required")
                    if time.monotonic() > deadline:
                        raise Failure(f"Command timed out after {timeout}s; evidence: {log_path}")
                    time.sleep(0.2)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
            output.seek(0)
            text = output.read(2_000_000)
            if process.returncode:
                raise Failure(f"{argv[0]} {argv[1] if len(argv) > 1 else ''} exited {process.returncode}; evidence: {log_path or 'command output'}")
            return text.strip()

    def candidates(self):
        cards = self.api.request("GET", f"/lists/{self.config['lists']['in_progress']}/cards", fields=FIELDS, filter="open")
        return sorted((c for c in cards if eligible(c, self.config)), key=lambda c: c.get("pos", 0))

    def preview(self):
        result = []
        candidates = self.candidates()
        board_cards = self.api.request("GET", f"/boards/{self.config['board_id']}/cards", fields=FIELDS, filter="all") if candidates else []
        for card in candidates:
            item = {"id": card["id"], "name": card["name"], "url": card.get("shortUrl")}
            try:
                project = project_for(card, self.config)
                dependency_cards(card, board_cards, self.config)
                item["project"] = project["name"]
                item["readiness"] = "eligible; full preflight happens before execution"
            except Failure as exc:
                item["readiness"] = "blocked: " + str(exc)
            if self.store and self.store.latest(card["id"]) and self.store.latest(card["id"])["status"] != "released":
                item["readiness"] = "recorded run exists; explicit retry required"
            result.append(item)
        return {"paused": self.paused(), "board": self.config["board_id"], "candidates": result}

    def consume_label(self, card):
        for label in card.get("labels", []):
            if label["name"] == self.config["ready_label"]:
                self.api.request("DELETE", f"/cards/{card['id']}/idLabels/{label['id']}")

    def finalize(self, run):
        target = "review" if run["status"] == "review_pending" else "blocked"
        card = self.api.card(run["card_id"])
        if card.get("closed") or card["idBoard"] != self.config["board_id"] or card["idList"] not in {
            self.config["lists"]["in_progress"], self.config["lists"][target]
        }:
            run["status"] = "cancelled"
            run["message"] += "\nOwner moved/archived card; runner preserved that state."
            self.store.save(run)
            return
        self.api.comment_once(run["card_id"], f"[cursor-run:{run['run_id']}:result]", run["message"])
        self.consume_label(card)
        if card["idList"] != self.config["lists"][target]:
            self.api.request("PUT", f"/cards/{run['card_id']}", idList=self.config["lists"][target])
        run["status"] = target
        self.store.save(run)
        self.telemetry()

    def tick(self):
        self.telemetry(poll=True)
        # Holding the OS lock proves no old command inheriting that lock remains active.
        for run in self.store.all():
            if run["status"] == "running":
                run["status"] = "blocked_pending"
                run["finished"] = now()
                run["message"] = ("Interrupted Cursor run. No automatic execution retry. Inspect preserved artifacts and remote PR before retry/handoff.\n"
                                  + self.location(run))
                self.store.save(run)
            if run["status"] in {"blocked_pending", "review_pending"}:
                self.finalize(run)
        if self.paused():
            return {"status": "paused"}
        candidates = self.candidates()
        available = [c for c in candidates if not self.store.latest(c["id"]) or self.store.latest(c["id"])["status"] == "released"]
        self.telemetry(success=True, queue=len(available))
        for candidate in candidates:
            previous = self.store.latest(candidate["id"])
            if previous and previous["status"] != "released":
                continue
            card = self.api.card(candidate["id"], full=True)
            if not eligible(card, self.config):
                continue
            run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
            run_dir = self.state / "runs" / run_id
            run_dir.mkdir(parents=True, mode=0o700)
            run = {"run_id": run_id, "card_id": card["id"], "card": card, "status": "running", "started": now(),
                   "model": self.config["model"],
                   "run_dir": str(run_dir), "branch": "cursor/" + card["id"] + "-" + run_id,
                   "worktree": str(self.state / "worktrees" / run_id), "message": ""}
            self.store.save(run)  # Durable reservation precedes every remote claim and launch.
            self.telemetry(queue=max(0, len(available) - 1))
            self.deadline = time.monotonic() + self.config.get("run_timeout_seconds", 7200)
            try:
                self.consume_label(card)
                self.api.comment_once(card["id"], f"[cursor-run:{run_id}:claim]",
                                      f"Claimed by Cursor; model {self.config['model']}; {run['started']}.\n" + self.location(run))
                self.execute(run)
            except (Failure, OSError, ValueError, KeyError) as exc:
                run["status"] = "blocked_pending"
                run["message"] = f"Cursor execution blocked: {exc}\nNo automatic retry.\n" + self.location(run)
                run["finished"] = now()
                self.store.save(run)
                self.telemetry()
            self.finalize(run)
            return {"status": run["status"], "run_id": run_id, "card_id": card["id"], "pr_url": run.get("pr_url")}
        return {"status": "idle", "eligible_cards": 0}

    @staticmethod
    def location(run):
        return f"Run: {run['run_id']}\nBranch: {run.get('branch', 'not prepared')}\nWorktree: {run.get('worktree', 'not prepared')}\nEvidence: {run['run_dir']}\nPR: {run.get('pr_url', 'not published')}"

    def context(self, run):
        card = run["card"]
        project = project_for(card, self.config)
        cards = self.api.request("GET", f"/boards/{self.config['board_id']}/cards", fields=FIELDS, filter="all")
        dependencies = dependency_cards(card, cards, self.config)
        if not re.search(r"(?im)^#{1,6}\s+(?:Acceptance criteria|Current acceptance|Definition of Done|Outcome)\b", card.get("desc", "")) and not card.get("checklists"):
            raise Failure("Card needs explicit acceptance criteria or an acceptance checklist")
        briefs = [self.api.card(card_id, full=True) for card_id in project["context_cards"]]
        # Respect additional explicitly linked execution-contract cards on this board.
        contract_sections = re.findall(r"(?ims)^#{1,6}\s+Execution contract\s*\n(.*?)(?=^#{1,6}\s|\Z)", card.get("desc", ""))
        contract_sections += re.findall(r"(?im)^Execution contract:\s*(.+)$", card.get("desc", ""))
        known = {b["id"] for b in briefs}
        for short_id in set(re.findall(r"https://trello\.com/c/([A-Za-z0-9]+)", "\n".join(contract_sections))):
            extra = self.api.card(short_id, full=True)
            if extra["id"] not in known:
                briefs.append(extra)
                known.add(extra["id"])
        for brief in briefs:
            if brief["idBoard"] != self.config["board_id"] or brief.get("closed") or not brief.get("desc", "").strip():
                raise Failure("Project brief/contract is missing, archived, empty or on another board")
        comments = self.api.request("GET", f"/cards/{card['id']}/actions", filter="commentCard", limit=50)
        return project, {"task": card, "project_context": briefs, "dependencies": dependencies, "recent_comments": comments}

    def cursor(self, run, instructions, name, timeout):
        folder = Path(run["run_dir"])
        (folder / f"{name}-prompt.txt").write_text(instructions)
        argv = [self.config["cursor_binary"], "--print", "--force", "--trust", "--model", self.config["model"],
                "--output-format", "stream-json", "--workspace", run["worktree"], instructions]
        output = self.command(argv, cwd=run["worktree"], run=run, timeout=timeout, interruptible=True)
        (folder / f"{name}-output.jsonl").write_text(output)

    def execute(self, run):
        project, bundle = self.context(run)
        run["project"] = project
        self.store.save(run)
        repo = str(Path(project["checkout"]).expanduser())
        actual = remote_repository(self.command(["git", "remote", "get-url", "origin"], cwd=repo, run=run))
        if actual != project["repository"]:
            raise Failure("Checkout remote differs from configured repository")
        self.command(["git", "fetch", "origin", project["base"]], cwd=repo, run=run, timeout=180)
        base_sha = self.command(["git", "rev-parse", "FETCH_HEAD"], cwd=repo, run=run)
        self.command(["git", "worktree", "add", "-b", run["branch"], run["worktree"], base_sha], cwd=repo, run=run)
        folder = Path(run["run_dir"])
        context_path = folder / "context.json"
        context_path.write_text(json.dumps(bundle, indent=2))
        skill = Path(self.config["skill_dir"]) / "SKILL.md"
        # Pin a copy for evidence as well as passing the currently installed contract explicitly.
        (folder / "shared-contract.md").write_text(skill.read_text())
        report_path = folder / "result.json"
        prompt = f"""Carry out exactly the Trello assignment in {context_path}.
Read that full context and the shared skill at {skill}, including relevant references.
You are Cursor, model selection {self.config['model']}. Repository {project['repository']}; base {project['base']}.
Work only in this isolated worktree: {run['worktree']}.
Inspect repository instructions and current code, then design and plan as needed for ALL current acceptance criteria.
Write the short plan to {folder / 'plan.md'} before implementation. Read supersession notes carefully.
Implement and run task-specific checks and required acceptance verification; fix issues within scope.
The runner owns git publication and Trello status updates: DO NOT commit, push, open/merge PRs,
change branches, or mutate Trello yourself. The owner has authorized this implementation through a PR.
Do not merge/deploy, change secrets/shared infrastructure or make paid live experiments without their applicable authorization.
Install worktree-local development dependencies as needed using the project's pinned runtime and instructions.
Do not copy production .env files or change the source checkout/its venv. Use source venv tools read-only if appropriate.
Keep credentials out of files, prompts and logs. Stop and report missing decisions/permissions or unmet prerequisites.
When finished, write a JSON object to {report_path} with:
{{"status":"complete" or "blocked", "summary":"...", "acceptance":[{{"criterion":"...","passed":true,"evidence":"actual check/result"}}],
 "checks":["actual commands and results"],"limitations":["..."],"blocker":"if blocked"}}.
Complete requires every current acceptance criterion demonstrated; report real evidence, not targets.
Do not write the report or runner artifacts into the repository. No automatic scope reduction or fallback.
"""
        self.cursor(run, prompt, "implementation", self.config["agent_timeout_seconds"])
        report = self.validate_report(report_path)
        for attempt in range(self.config.get("max_repairs", 1) + 1):
            try:
                try:
                    self.verify(project, run)
                except Failure as exc:
                    raise Repairable(str(exc)) from None
                self.assert_task_active(run)
                self.publish(project, run, report)
                return
            except Repairable as exc:
                if attempt >= self.config.get("max_repairs", 1):
                    raise
                self.assert_task_active(run)
                repair_path = folder / f"result-repair-{attempt + 1}.json"
                self.cursor(run, prompt + f"\nThis is a bounded repair attempt. Verification failed: {exc}.\n"
                            f"Read existing evidence in {folder}; inspect failing CI using gh read-only commands if a PR exists.\n"
                            f"Current PR: {run.get('pr_url', 'not published')}. Fix within scope; the runner still owns commits/push/PR.\n"
                            f"Write the fresh final report to {repair_path} (instead of the earlier result path).",
                            f"repair-{attempt + 1}", self.config.get("repair_timeout_seconds", 1200))
                report = self.validate_report(repair_path)

    @staticmethod
    def validate_report(path):
        report = json.loads(path.read_text())
        if report.get("status") != "complete":
            raise Failure("Executor reported a blocker: " + str(report.get("blocker", report.get("summary", "missing report")))[:1500])
        criteria = report.get("acceptance")
        if not report.get("summary") or not isinstance(criteria, list) or not criteria:
            raise Failure("Executor report lacks acceptance evidence")
        if any(c.get("passed") is not True or not c.get("criterion") or not c.get("evidence") for c in criteria):
            raise Failure("Not all reported acceptance criteria have passing evidence")
        if not isinstance(report.get("checks"), list) or not report["checks"]:
            raise Failure("Executor report lacks actual checks")
        return report

    def verify(self, project, run):
        self.command(["git", "diff", "--check"], cwd=run["worktree"], run=run)
        self.command(["git", "diff", "--cached", "--check"], cwd=run["worktree"], run=run)
        for check in project["checks"]:
            self.command(check, cwd=run["worktree"], run=run,
                         timeout=self.config["check_timeout_seconds"], interruptible=True)

    def assert_task_active(self, run):
        card = self.api.card(run["card_id"])
        if self.paused() or card.get("closed") or card["idBoard"] != self.config["board_id"] or card["idList"] != self.config["lists"]["in_progress"]:
            raise Failure("Task paused/moved/archived; publication stopped and work preserved")
        if any(card.get(key) != run["card"].get(key) for key in ("name", "desc")):
            raise Failure("Task specification changed during execution; review the updated scope before continuing")
        if self.deadline and time.monotonic() >= self.deadline:
            raise Failure("Overall run deadline exceeded; preserve work for explicit retry/handoff")

    def publish(self, project, run, report):
        worktree = run["worktree"]
        if self.command(["git", "branch", "--show-current"], cwd=worktree, run=run) != run["branch"]:
            raise Failure("Executor changed task branch")
        if remote_repository(self.command(["git", "remote", "get-url", "origin"], cwd=worktree, run=run)) != project["repository"]:
            raise Failure("Repository identity changed")
        if not self.command(["git", "status", "--porcelain"], cwd=worktree, run=run):
            raise Failure("No changes to publish; reconcile existing work manually")
        self.command(["git", "add", "--all"], cwd=worktree, run=run)
        prepared_tree = self.command(["git", "write-tree"], cwd=worktree, run=run)
        title = "feat: " + run["card"]["name"][:110]
        self.command(["git", "commit", "-m", title], cwd=worktree, run=run, timeout=300)
        # Commit hooks may edit code: recheck the final tree before publishing.
        if self.command(["git", "status", "--porcelain"], cwd=worktree, run=run):
            raise Failure("Commit hooks left uncommitted changes; inspect before retry")
        if self.command(["git", "rev-parse", "HEAD^{tree}"], cwd=worktree, run=run) != prepared_tree:
            try:
                self.verify(project, run)
            except Failure as exc:
                raise Repairable("Commit hooks changed the verified tree: " + str(exc)) from None
        head = self.command(["git", "rev-parse", "HEAD"], cwd=worktree, run=run)
        run["head"] = head
        self.store.save(run)
        self.assert_task_active(run)
        self.command(["git", "push", "--set-upstream", "origin", run["branch"]], cwd=worktree, run=run, timeout=180)
        body = self.pr_body(run, report)
        body_path = Path(run["run_dir"]) / "pr-body.md"
        body_path.write_text(body)
        existing = json.loads(self.command(["gh", "pr", "list", "--repo", project["repository"], "--head", run["branch"],
                                            "--state", "all", "--json", "url,state"], run=run))
        if existing:
            if len(existing) != 1 or existing[0]["state"] != "OPEN":
                raise Failure("Existing branch PR is closed or ambiguous")
            url = existing[0]["url"]
            self.command(["gh", "pr", "edit", url, "--repo", project["repository"], "--body-file", str(body_path)], run=run)
        else:
            url = self.command(["gh", "pr", "create", "--repo", project["repository"], "--base", project["base"],
                                "--head", run["branch"], "--title", title, "--body-file", str(body_path)], run=run, timeout=120)
        run["pr_url"] = url
        self.store.save(run)  # Persist remote identity before any later API update.
        self.wait_ci(project, run)
        run["status"] = "review_pending"
        run["finished"] = now()
        run["message"] = body + "\n\n" + self.location(run) + f"\nVerified head: {run['head']}\nCI: {run['ci_result']}."
        self.store.save(run)

    @staticmethod
    def pr_body(run, report):
        evidence = "\n".join(f"- {c['criterion']}: {c['evidence']}" for c in report["acceptance"])
        checks = "\n".join("- " + str(c) for c in report["checks"])
        limits = "\n".join("- " + str(c) for c in report.get("limitations", [])) or "None reported."
        return (f"{report['summary']}\n\nTrello: {run['card'].get('shortUrl', run['card_id'])}\n\n"
                f"Acceptance evidence:\n{evidence}\n\nValidation:\n{checks}\n\nLimitations:\n{limits}\n\n"
                f"Cursor run: {run['run_id']}; model selection {run.get('model', 'auto')}. Independent configured checks were rerun by the wrapper.")

    def wait_ci(self, project, run):
        started = time.monotonic()
        deadline = time.monotonic() + self.config["ci_timeout_seconds"]
        while True:
            self.telemetry()
            self.assert_task_active(run)
            pr = json.loads(self.command(["gh", "pr", "view", run["pr_url"], "--repo", project["repository"],
                                         "--json", "url,state,baseRefName,headRefName,headRefOid,statusCheckRollup"], run=run))
            if (pr["state"] != "OPEN" or pr["baseRefName"] != project["base"] or pr["headRefName"] != run["branch"] or pr["headRefOid"] != run["head"]):
                raise Failure("Remote PR state/base/branch/head does not match this run")
            state = ci_state(pr["statusCheckRollup"], project.get("required_checks", []))
            (Path(run["run_dir"]) / "remote-pr.json").write_text(json.dumps(pr, indent=2))
            if state == "passed":
                run["ci_result"] = "passed on the recorded PR head"
                return
            if state == "failed":
                raise Repairable("PR CI failed. Inspect remote-pr.json and failing Actions logs")
            if not pr["statusCheckRollup"] and not project.get("required_checks") and project.get("allow_no_ci") and time.monotonic() - started >= self.config.get("ci_discovery_seconds", 60):
                run["ci_result"] = "no remote checks configured/reported; configured local checks passed"
                return
            if time.monotonic() >= deadline:
                raise Failure("PR CI did not pass within the configured timeout; evidence preserved")
            time.sleep(self.config.get("ci_poll_seconds", 30))


def ci_state(checks, required):
    names = {c.get("name", c.get("context")) for c in checks}
    if not checks or not set(required).issubset(names):
        return "pending"
    for c in checks:
        conclusion = c.get("conclusion") if c.get("__typename") == "CheckRun" else c.get("state")
        if conclusion in {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"}:
            return "failed"
    if all((c.get("status") == "COMPLETED" and c.get("conclusion") in {"SUCCESS", "NEUTRAL", "SKIPPED"})
           if c.get("__typename") == "CheckRun" else c.get("state") == "SUCCESS" for c in checks):
        # Explicitly required checks cannot pass solely by being skipped/neutral.
        if any(c.get("name", c.get("context")) in required and c.get("conclusion", c.get("state")) != "SUCCESS" for c in checks):
            return "failed"
        return "passed"
    return "pending"


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("command", choices=["preview", "run", "status", "pause", "resume", "retry", "handoff"])
    parser.add_argument("card_id", nargs="?")
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    state = Path(config["state_dir"]).expanduser()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.command == "pause":
        (state / "PAUSED").touch(mode=0o600)
        print("Paused. Active commands stop when the runner next checks the pause marker.")
        return
    if args.command == "resume":
        (state / "PAUSED").unlink(missing_ok=True)
        print("Resumed polling. Blocked/interrupted cards still require deliberate retry or handoff.")
        return
    if args.command == "status":
        store = Store(state / "runs.sqlite3")
        print(json.dumps({"paused": (state / "PAUSED").exists(), "runs": [
            {k: r.get(k) for k in ("run_id", "card_id", "status", "started", "updated", "branch", "worktree", "pr_url", "message")}
            for r in store.all()]}, indent=2))
        return
    api = Trello(load_credentials(Path(config["credential_file"]).expanduser()))
    if args.command == "preview":
        store = Store(state / "runs.sqlite3") if (state / "runs.sqlite3").exists() else None
        print(json.dumps(Runner(config, api, store).preview(), indent=2))
        return
    with process_lock(state) as fd:
        store = Store(state / "runs.sqlite3")
        runner = Runner(config, api, store, fd)
        if args.command in {"retry", "handoff"}:
            if not args.card_id:
                raise Failure("Supply a card ID")
            run = store.latest(args.card_id)
            if not run or run["status"] not in TERMINAL:
                raise Failure("Only a reconciled terminal run can be retried/handed off")
            if args.command == "retry":
                project = run.get("project") or project_for(run["card"], config)
                prs = json.loads(runner.command(["gh", "pr", "list", "--repo", project["repository"], "--head", run["branch"],
                                                "--state", "all", "--json", "url,state"]))
                if prs or run.get("pr_url"):
                    raise Failure("A PR already exists. Use handoff to continue its branch/PR; do not launch a duplicate implementation")
                # This command releases local suppression; the owner must explicitly restore the two Trello gates.
                run["status"] = "released"
                store.save(run)
                print("Retry allowed. Inspect preserved work, then move the card to In Progress and add ready_for_cursor.")
            else:
                card = api.card(run["card_id"])
                runner.consume_label(card)
                api.comment_once(run["card_id"], f"[cursor-run:{run['run_id']}:handoff]",
                                 "Manual handoff: automatic runner released ownership. Choose the backup/model explicitly.\n" + runner.location(run))
                run["status"] = "handoff"
                store.save(run)
                print("Ownership handed off for manual execution; automatic pickup remains suppressed.")
        else:
            try:
                print(json.dumps(runner.tick()))
            except Exception:
                runner.telemetry(error=True)
                raise
            finally:
                runner.telemetry()
                store.close()


if __name__ == "__main__":
    try:
        main()
    except Failure as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
