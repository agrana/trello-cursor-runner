import copy
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from runner import (Failure, Runner, Store, ci_state, dependency_cards, eligible,
                    default_config_path, load_config, load_credentials, main,
                    process_lock, project_for, remote_repository)


def configuration(root):
    return {
        "board_id": "board", "ready_label": "ready_for_cursor",
        "lists": {"in_progress": "active", "review": "review", "blocked": "blocked", "done": "done"},
        "state_dir": str(root / "state"), "model": "auto", "environment": {},
        "agent_timeout_seconds": 10, "check_timeout_seconds": 10,
        "ci_timeout_seconds": 1, "ci_poll_seconds": 0, "ci_discovery_seconds": 0, "max_repairs": 0,
        "projects": [{"name": "Widget", "card_pattern": r"^\[TASK-\d+\]", "repository": "acme/widget",
                      "checkout": str(root / "source"), "base": "main", "context_cards": ["brief"],
                      "checks": [["python3", "-c", "from pathlib import Path; assert Path('feature.txt').read_text() == 'implemented\\n'"]],
                      "required_checks": ["quality"]}]
    }


def card():
    return {"id": "task", "name": "[TASK-1] Implement feature", "desc": "## Dependencies\nNone\n## Acceptance criteria\nFeature works.",
            "idBoard": "board", "idList": "active", "labels": [{"id": "ready", "name": "ready_for_cursor"}],
            "closed": False, "pos": 1, "shortUrl": "https://trello.com/c/task", "checklists": []}


class FakeTrello:
    def __init__(self):
        self.cards = {"task": card(), "brief": dict(card(), id="brief", name="Brief", desc="Project contract.", idList="briefs", labels=[])}
        self.comments = []
        self.calls = []
        self.fail_move = False

    def card(self, card_id, full=False):
        return copy.deepcopy(self.cards[card_id])

    def comment_once(self, card_id, marker, message):
        if not any(item[1] == marker for item in self.comments):
            self.comments.append((card_id, marker, message))

    def request(self, method, path, **params):
        self.calls.append((method, path, params))
        if method == "GET" and path.startswith("/lists/"):
            return [copy.deepcopy(c) for c in self.cards.values() if c["idList"] == "active"]
        if method == "GET" and path.startswith("/boards/"):
            return copy.deepcopy(list(self.cards.values()))
        if method == "GET" and path.endswith("/actions"):
            return []
        if method == "DELETE" and "/idLabels/" in path:
            self.cards[path.split("/")[2]]["labels"] = []
            return None
        if method == "PUT":
            if self.fail_move:
                self.fail_move = False
                raise Failure("Simulated lost/failed Trello move")
            self.cards[path.split("/")[2]]["idList"] = params["idList"]
            return None
        raise AssertionError((method, path, params))


class SimulatedRunner(Runner):
    executions = 0
    failure = None

    def execute(self, run):
        self.executions += 1
        if self.failure:
            raise Failure(self.failure)
        run["status"] = "review_pending"
        run["pr_url"] = "https://github.com/acme/widget/pull/1"
        run["message"] = "Verified PR and acceptance evidence."
        self.store.save(run)


class ConfigurationTests(unittest.TestCase):
    def test_user_settings_are_selected_independently_of_checkout(self):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "/tmp/example-user-config"}, clear=True):
            self.assertEqual(default_config_path(), Path("/tmp/example-user-config/trello-cursor-runner/config.json"))
        with patch.dict(os.environ, {"HOME": "/tmp/example-home"}, clear=True):
            self.assertEqual(default_config_path(), Path("/tmp/example-home/.config/trello-cursor-runner/config.json"))
        with patch.dict(os.environ, {"TRELLO_CURSOR_CONFIG": "/tmp/explicit.json", "XDG_CONFIG_HOME": "/tmp/unused"}, clear=True):
            self.assertEqual(default_config_path(), Path("/tmp/explicit.json"))

    def test_explicit_config_overrides_environment_in_real_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selected = root / "selected.json"
            selected.write_text(json.dumps(configuration(root)))
            with patch.dict(os.environ, {"TRELLO_CURSOR_CONFIG": str(root / "missing.json")}):
                with patch.object(sys, "argv", ["runner.py", "--config", str(selected), "status"]):
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        main()
            self.assertEqual(json.loads(output.getvalue())["runs"], [])
            self.assertTrue((root / "state" / "runs.sqlite3").exists())

    def test_local_paths_expand_and_check_arguments_remain_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = configuration(root)
            config.update(state_dir="~/state", cursor_binary="~/.local/bin/cursor-agent")
            config["projects"][0].update(checkout="~/checkout", checks=[["~/venv/bin/python", "-c", "print('literal $VALUE')"]])
            path = root / "settings.json"
            path.write_text(json.dumps(config))
            with patch.dict(os.environ, {"HOME": str(root)}):
                loaded = load_config(path)
            self.assertEqual(loaded["state_dir"], str(root / "state"))
            self.assertEqual(loaded["cursor_binary"], str(root / ".local/bin/cursor-agent"))
            self.assertEqual(loaded["projects"][0]["checks"][0], [str(root / "venv/bin/python"), "-c", "print('literal $VALUE')"])

    def test_missing_config_has_setup_guidance_and_no_repository_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(Failure, "config.example.json"):
                load_config(Path(tmp) / "missing.json")

    def test_credentials_can_come_only_from_environment(self):
        with patch.dict(os.environ, {"TRELLO_API_KEY": "fixture-key", "TRELLO_TOKEN": "fixture-token"}, clear=True):
            self.assertEqual(load_credentials(None), {"TRELLO_API_KEY": "fixture-key", "TRELLO_TOKEN": "fixture-token"})


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = configuration(self.root)
        self.api = FakeTrello()
        self.store = Store(self.root / "state.db")
        self.addCleanup(self.store.close)
        self.runner = SimulatedRunner(self.config, self.api, self.store)

    def test_both_gates_are_required_in_either_order(self):
        current = card()
        self.assertTrue(eligible(current, self.config))
        for update in ({"labels": []}, {"idList": "ready"}, {"closed": True}, {"idBoard": "elsewhere"},
                       {"labels": [{"name": "Ready_for_cursor"}]}):
            self.assertFalse(eligible(dict(current, **update), self.config))
        current["labels"] = []
        self.assertFalse(eligible(current, self.config))
        current["labels"] = card()["labels"]
        self.assertTrue(eligible(current, self.config))

    def test_preview_has_no_trello_writes_and_no_agent(self):
        result = self.runner.preview()
        self.assertEqual(result["candidates"][0]["project"], "Widget")
        self.assertTrue(all(call[0] == "GET" for call in self.api.calls))
        self.assertEqual(self.runner.executions, 0)
        self.assertEqual(self.store.all(), [])

    def test_empty_queue_never_launches(self):
        self.api.cards["task"]["labels"] = []
        self.assertEqual(self.runner.tick()["status"], "idle")
        self.assertEqual(self.runner.executions, 0)
        self.assertEqual(self.store.all(), [])

    def test_success_and_readded_label_do_not_duplicate(self):
        self.assertEqual(self.runner.tick()["status"], "review")
        self.api.cards["task"].update(idList="active", labels=card()["labels"])
        self.assertEqual(self.runner.tick()["status"], "idle")
        self.assertEqual(self.runner.executions, 1)

    def test_failed_run_requires_deliberate_retry(self):
        self.runner.failure = "Missing prerequisite"
        self.assertEqual(self.runner.tick()["status"], "blocked")
        self.api.cards["task"].update(idList="active", labels=card()["labels"])
        self.runner.tick()
        self.assertEqual(self.runner.executions, 1)
        self.assertIn("Missing prerequisite", self.store.latest("task")["message"])

    def test_manual_handoff_suppresses_automatic_pickup(self):
        self.runner.failure = "Requires a chosen model"
        self.runner.tick()
        run = self.store.latest("task")
        run["status"] = "handoff"
        self.store.save(run)
        self.api.cards["task"].update(idList="active", labels=card()["labels"])
        self.runner.tick()
        self.assertEqual(self.runner.executions, 1)

    def test_deliberate_retry_creates_new_history_without_discarding_old_run(self):
        self.runner.failure = "Missing prerequisite"
        self.runner.tick()
        first = self.store.latest("task")
        first["status"] = "released"
        self.store.save(first)
        self.api.cards["task"].update(idList="active", labels=card()["labels"])
        self.runner.failure = None
        self.assertEqual(self.runner.tick()["status"], "review")
        self.assertEqual(len(self.store.all()), 2)
        self.assertEqual(self.runner.executions, 2)

    def test_retry_uses_corrected_live_title_when_initial_mapping_failed(self):
        self.check_retry_after_mapping_failure([])

    def test_retry_still_refuses_an_existing_pr_after_title_correction(self):
        self.check_retry_after_mapping_failure([{"url": "https://github.com/acme/widget/pull/1", "state": "OPEN"}])

    def check_retry_after_mapping_failure(self, prs):
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(self.config))
        state = Path(self.config["state_dir"])
        state.mkdir()
        store = Store(state / "runs.sqlite3")
        old_card = dict(card(), name="Investigate telemetry")
        run = {"run_id": "failed-routing", "card_id": "task", "card": old_card,
               "status": "blocked", "started": "2026-11-08T15:24:03+00:00", "branch": "cursor/failed-routing"}
        store.save(run)
        store.close()
        argv = ["runner.py", "--config", str(config_path), "retry", "task"]
        with patch.object(sys, "argv", argv), patch("runner.load_credentials", return_value={}), \
                patch("runner.Trello", return_value=self.api), \
                patch.object(Runner, "command", return_value=json.dumps(prs)) as command, \
                contextlib.redirect_stdout(io.StringIO()):
            if prs:
                with self.assertRaisesRegex(Failure, "A PR already exists"):
                    main()
            else:
                main()
        command.assert_called_once_with(["gh", "pr", "list", "--repo", "acme/widget", "--head",
                                         "cursor/failed-routing", "--state", "all", "--json", "url,state"])
        store = Store(state / "runs.sqlite3")
        self.addCleanup(store.close)
        saved = store.latest("task")
        self.assertEqual(saved["status"], "blocked" if prs else "released")
        self.assertEqual(saved["card"]["name"], old_card["name"])
        self.assertEqual(self.api.cards["task"], card())
        self.assertEqual(self.api.comments, [])

    def test_reservation_is_durable_before_executor(self):
        def execute(run):
            self.assertEqual(self.store.latest("task")["status"], "running")
            self.assertEqual(self.api.cards["task"]["labels"], [])
            raise Failure("Fixture stop after reservation")
        self.runner.execute = execute
        self.runner.tick()
        self.assertEqual(self.store.latest("task")["status"], "blocked")

    def test_result_update_failure_recovers_without_reimplementation(self):
        self.api.fail_move = True
        with self.assertRaises(Failure):
            self.runner.tick()
        self.assertEqual(self.store.latest("task")["status"], "review_pending")
        self.runner.tick()
        self.assertEqual(self.store.latest("task")["status"], "review")
        self.assertEqual(self.runner.executions, 1)
        self.assertEqual(len([c for c in self.api.comments if ":result]" in c[1]]), 1)

    def test_interrupted_claim_is_blocked_without_launch(self):
        self.store.save({"run_id": "old", "card_id": "task", "status": "running", "started": "2026-01-01",
                         "run_dir": "/tmp/evidence", "branch": "old-branch", "worktree": "/tmp/work"})
        self.runner.tick()
        self.assertEqual(self.store.latest("task")["status"], "blocked")
        self.assertEqual(self.runner.executions, 0)

    def test_pause_prevents_pickup(self):
        state = Path(self.config["state_dir"])
        state.mkdir()
        (state / "PAUSED").touch()
        self.assertEqual(self.runner.tick()["status"], "paused")
        self.assertEqual(self.runner.executions, 0)

    def test_owner_move_is_preserved_on_finalization(self):
        def execute(run):
            run.update(status="review_pending", message="Delivered")
            self.store.save(run)
            self.api.cards["task"]["idList"] = "backlog"
        self.runner.execute = execute
        self.assertEqual(self.runner.tick()["status"], "cancelled")
        self.assertEqual(self.api.cards["task"]["idList"], "backlog")

    def test_unknown_or_ambiguous_project_is_rejected(self):
        with self.assertRaises(Failure):
            project_for(dict(card(), name="Unmapped task"), self.config)
        self.config["projects"] *= 2
        with self.assertRaises(Failure):
            project_for(card(), self.config)

    def test_dependencies_must_be_done(self):
        current = dict(card(), desc="## Dependencies\nTASK-2 https://trello.com/c/dep\n## Acceptance criteria\nWorks")
        dep = dict(card(), id="dep", name="[TASK-2] Prerequisite", shortUrl="https://trello.com/c/dep")
        with self.assertRaises(Failure):
            dependency_cards(current, [dep], self.config)
        dep["idList"] = "done"
        self.assertEqual(len(dependency_cards(current, [dep], self.config)), 1)
        with self.assertRaises(Failure):
            dependency_cards(current, [], self.config)

    def test_depends_on_heading_is_enforced_for_existing_board_cards(self):
        current = dict(card(), desc="## Depends on\nTASK-2 (Done).\n## Unified acceptance criteria\nWorks")
        dependency = dict(card(), id="dep", name="[TASK-2] Prerequisite", idList="active")
        with self.assertRaisesRegex(Failure, "not Done"):
            dependency_cards(current, [dependency], self.config)
        dependency["idList"] = "done"
        self.assertEqual(dependency_cards(current, [dependency], self.config)[0]["id"], "dep")

    def test_changed_task_stops_publication(self):
        run = {"card_id": "task", "card": card()}
        self.api.cards["task"]["desc"] += "\nChanged requirement"
        with self.assertRaisesRegex(Failure, "specification changed"):
            self.runner.assert_task_active(run)

    def test_os_lock_prevents_a_second_owner(self):
        with process_lock(self.root / "lockstate"):
            with self.assertRaisesRegex(Failure, "busy"):
                with process_lock(self.root / "lockstate"):
                    self.fail("Second owner acquired lock")

    def test_command_pause_terminates_child(self):
        state = Path(self.config["state_dir"])
        state.mkdir()
        (state / "PAUSED").touch()
        with self.assertRaisesRegex(Failure, "paused"):
            self.runner.command(["python3", "-c", "import time; time.sleep(20)"], interruptible=True)

    def test_credentials_are_selected_without_executing_source(self):
        path = self.root / "credentials"
        path.write_text("TRELLO_API_KEY='test-key'\nTRELLO_TOKEN=test-token\nUNRELATED=$(touch /tmp/not-executed)\n")
        from unittest.mock import patch
        with patch.dict(os.environ, {}, clear=True):
            values = load_credentials(path)
        self.assertEqual(values, {"TRELLO_API_KEY": "test-key", "TRELLO_TOKEN": "test-token"})

    def test_metrics_are_atomic_nonsecret_and_transitions_are_monotonic(self):
        destination = self.root / "metrics" / "runner.prom"
        self.config["metrics_file"] = str(destination)
        self.runner.tick()
        metrics = destination.read_text()
        self.assertIn('trello_cursor_runs{status="review"} 1', metrics)
        self.assertIn('trello_cursor_run_transitions_total{status="review"} 1', metrics)
        self.assertIn('trello_cursor_active_runs 0', metrics)
        self.assertNotIn('https://', metrics)
        self.assertNotIn('task', metrics)
        run = self.store.latest("task")
        run["status"] = "released"
        self.store.save(run)
        self.runner.telemetry(error=True)
        metrics = destination.read_text()
        self.assertIn('trello_cursor_run_transitions_total{status="review"} 1', metrics)
        self.assertIn('trello_cursor_poll_errors_total 1', metrics)
        self.assertEqual(list(destination.parent.glob('*.tmp')), [])

    def test_missing_or_failed_report_cannot_publish(self):
        path = self.root / "result.json"
        for report in ({"status": "blocked", "blocker": "No access"}, {"status": "complete", "summary": "Looks good"},
                       {"status": "complete", "summary": "Changed", "acceptance": [{"criterion": "Works", "passed": False, "evidence": "Failed"}]}):
            path.write_text(json.dumps(report))
            with self.assertRaises(Failure):
                self.runner.validate_report(path)


class CITests(unittest.TestCase):
    def test_pending_missing_and_skipped_required_checks_cannot_pass(self):
        check = {"__typename": "CheckRun", "name": "quality", "status": "IN_PROGRESS", "conclusion": ""}
        self.assertEqual(ci_state([], ["quality"]), "pending")
        self.assertEqual(ci_state([check], ["quality"]), "pending")
        check.update(status="COMPLETED", conclusion="SKIPPED")
        self.assertEqual(ci_state([check], ["quality"]), "failed")
        check["conclusion"] = "FAILURE"
        self.assertEqual(ci_state([check], ["quality"]), "failed")
        check["conclusion"] = "SUCCESS"
        self.assertEqual(ci_state([check], ["quality"]), "passed")

    def test_remote_identity_is_restricted(self):
        self.assertEqual(remote_repository("git@github.com:acme/widget.git"), "acme/widget")
        self.assertEqual(remote_repository("https://github.com/acme/widget.git"), "acme/widget")
        self.assertIsNone(remote_repository("https://github.com.evil/acme/widget"))


class GitDeliveryTests(unittest.TestCase):
    """Real worktree/commit/push/checks with a fake Cursor executable and fake hosted APIs."""

    def test_end_to_end_delivers_one_pr_and_preserves_source_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = configuration(root)
            source = root / "source"
            source.mkdir()
            def git(*argv, cwd=source):
                return subprocess.check_output(["git", *argv], cwd=cwd, stderr=subprocess.STDOUT, text=True).strip()
            git("init", "-b", "main")
            git("config", "user.name", "Fixture")
            git("config", "user.email", "fixture@example.invalid")
            (source / "README.md").write_text("Original\n")
            git("add", ".")
            git("commit", "-m", "Initial")
            bare = root / "remote.git"
            git("clone", "--bare", str(source), str(bare))
            git("remote", "add", "origin", "https://github.com/acme/widget.git")
            (source / "README.md").write_text("Owner's uncommitted work\n")
            fake_cursor = root / "cursor"
            fake_cursor.write_text('''#!/usr/bin/python3
import json, re, sys
from pathlib import Path
prompt = sys.argv[-1]
report = re.search(r"When finished, write a JSON object to (.*?) with:", prompt)[1]
plan = re.search(r"Write the short plan to (.*?) before implementation", prompt)[1]
Path(plan).write_text("Implement feature and verify content.")
Path("feature.txt").write_text("implemented\\n")
Path(report).write_text(json.dumps({"status":"complete","summary":"Adds the requested feature.","acceptance":[{"criterion":"Feature works","passed":True,"evidence":"Content verified by a separate wrapper check"}],"checks":["fixture content check"],"limitations":[]}))
''')
            fake_cursor.chmod(0o700)
            skill = root / "skill"
            skill.mkdir()
            (skill / "SKILL.md").write_text("Fixture delivery contract.")
            config.update(cursor_binary=str(fake_cursor), skill_dir=str(skill))
            config["projects"][0]["checks"] = [["python3", "-c", "from pathlib import Path; assert Path('feature.txt').read_text() == 'implemented\\n'"]]
            api = FakeTrello()
            store = Store(root / "state.sqlite3")
            self.addCleanup(store.close)

            class LocalRunner(Runner):
                created = 0

                def command(self, argv, cwd=None, run=None, **kwargs):
                    if argv[:3] == ["git", "fetch", "origin"]:
                        argv = ["git", "fetch", str(bare), "main"]
                    elif argv[:2] == ["git", "push"]:
                        argv = ["git", "push", "--set-upstream", str(bare), run["branch"]]
                    if argv[:3] == ["gh", "pr", "list"]:
                        return "[]"
                    if argv[:3] == ["gh", "pr", "create"]:
                        self.created += 1
                        return "https://github.com/acme/widget/pull/1"
                    if argv[:3] == ["gh", "pr", "view"]:
                        head = git("--git-dir", str(bare), "rev-parse", "refs/heads/" + run["branch"])
                        return json.dumps({"url":run["pr_url"], "state":"OPEN", "baseRefName":"main",
                                           "headRefName":run["branch"], "headRefOid":head,
                                           "statusCheckRollup":[{"__typename":"CheckRun", "name":"quality", "status":"COMPLETED", "conclusion":"SUCCESS"}]})
                    return super().command(argv, cwd=cwd, run=run, **kwargs)

            runner = LocalRunner(config, api, store)
            result = runner.tick()
            self.assertEqual(result["status"], "review", store.latest("task")["message"])
            self.assertEqual(runner.created, 1)
            self.assertEqual((source / "README.md").read_text(), "Owner's uncommitted work\n")
            self.assertFalse((source / "feature.txt").exists())
            self.assertEqual(git("branch", "--show-current"), "main")
            run = store.latest("task")
            self.assertTrue((Path(run["run_dir"]) / "remote-pr.json").exists())
            self.assertEqual(git("--git-dir", str(bare), "rev-parse", "refs/heads/" + run["branch"]), run["head"])
            runner.tick()
            self.assertEqual(runner.created, 1)


if __name__ == "__main__":
    unittest.main()
