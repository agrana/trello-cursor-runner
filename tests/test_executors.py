import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from pull_requests import ownership_from
from runner import (Failure, Runner, Store, eligible, load_config, matching_executors,
                    model_display, normalize_executors, recorded_model, resolve_executor)
from test_pull_requests import Board, GitHub, LifecycleRunner, check, comment
from test_runner import FakeTrello, SimulatedRunner, card, configuration


def dual_executors(root, kiro_model=None, agent=None):
    config = configuration(root)
    kiro = {"label": "ready_for_kiro", "binary": "kiro-cli"}
    if kiro_model is not None:
        kiro["model"] = kiro_model
    if agent is not None:
        kiro["agent"] = agent
    config["executors"] = {
        "cursor": {"label": "ready_for_cursor", "binary": config["cursor_binary"], "model": "auto"},
        "kiro": kiro,
    }
    return normalize_executors(config)


class ExecutorConfigTests(unittest.TestCase):
    def test_legacy_ready_label_binary_model_synthesize_cursor_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "config.json"
            path.write_text(json.dumps({
                "board_id": "board", "ready_label": "ready_for_cursor", "cursor_binary": "~/bin/cursor-agent",
                "model": "auto", "state_dir": str(root / "state"), "lists": {"in_progress": "a", "review": "b", "blocked": "c", "done": "d"},
                "projects": []
            }))
            with patch.dict(os.environ, {"HOME": str(root)}):
                loaded = load_config(path)
            self.assertEqual(set(loaded["executors"]), {"cursor"})
            self.assertEqual(loaded["executors"]["cursor"]["label"], "ready_for_cursor")
            self.assertEqual(loaded["executors"]["cursor"]["binary"], str(root / "bin/cursor-agent"))
            self.assertEqual(loaded["executors"]["cursor"]["model"], "auto")

    def test_kiro_auto_model_is_rejected(self):
        with self.assertRaisesRegex(Failure, "never pass Cursor auto"):
            normalize_executors({"executors": {"kiro": {"label": "ready_for_kiro", "binary": "kiro-cli", "model": "auto"}},
                                 "state_dir": "/tmp", "lists": {}, "board_id": "b", "projects": []})

    def test_unsupported_executor_fails_closed(self):
        with self.assertRaisesRegex(Failure, "Unsupported executor"):
            normalize_executors({"executors": {"pi": {"label": "ready_for_pi", "binary": "pi"}},
                                 "state_dir": "/tmp", "lists": {}, "board_id": "b", "projects": []})


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = dual_executors(self.root)

    def test_exact_kiro_label_routes_and_cursor_label_still_works(self):
        kiro_card = dict(card(), labels=[{"id": "k", "name": "ready_for_kiro"}])
        self.assertTrue(eligible(kiro_card, self.config))
        self.assertEqual(resolve_executor(kiro_card, self.config)[0], "kiro")
        self.assertTrue(eligible(card(), self.config))
        self.assertEqual(resolve_executor(card(), self.config)[0], "cursor")

    def test_ambiguous_double_executor_labels_fail_closed(self):
        both = dict(card(), labels=[{"id": "c", "name": "ready_for_cursor"}, {"id": "k", "name": "ready_for_kiro"}])
        self.assertFalse(eligible(both, self.config))
        self.assertEqual(len(matching_executors(both, self.config)), 2)
        with self.assertRaisesRegex(Failure, "Ambiguous executor labels"):
            resolve_executor(both, self.config)

    def test_case_sensitive_label_matching_is_preserved(self):
        wrong = dict(card(), labels=[{"name": "Ready_for_kiro"}])
        self.assertFalse(eligible(wrong, self.config))

    def test_preview_reports_ambiguous_without_launch(self):
        api = FakeTrello()
        api.cards["task"]["labels"] = [{"id": "c", "name": "ready_for_cursor"}, {"id": "k", "name": "ready_for_kiro"}]
        runner = SimulatedRunner(self.config, api, Store(self.root / "state.db"))
        preview = runner.preview()
        self.assertIn("Ambiguous executor labels", preview["candidates"][0]["readiness"])
        self.assertEqual(runner.tick()["status"], "idle")
        self.assertEqual(runner.executions, 0)


class OwnershipPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = dual_executors(self.root)
        self.api = FakeTrello()
        self.api.cards["task"]["labels"] = [{"id": "k", "name": "ready_for_kiro"}]
        self.store = Store(self.root / "state.db")
        self.addCleanup(self.store.close)
        self.runner = SimulatedRunner(self.config, self.api, self.store)

    def test_claim_persists_executor_and_unset_kiro_model_truthfully(self):
        self.assertEqual(self.runner.tick()["status"], "review")
        run = self.store.latest("task")
        self.assertEqual(run["executor"], "kiro")
        self.assertIn("model", run)
        self.assertIsNone(run["model"])
        self.assertEqual(model_display(run), "CLI default")
        self.assertTrue(run["branch"].startswith("kiro/"))
        claim = next(c for c in self.api.comments if c[1].endswith(":claim]"))
        self.assertIn("Claimed by Kiro", claim[2])
        self.assertIn("CLI default", claim[2])

    def test_historical_records_default_to_cursor(self):
        legacy = {"run_id": "old", "card_id": "task", "status": "review", "started": "2026-01-01"}
        self.assertEqual(recorded_model(legacy), "auto")
        self.assertEqual(ownership_from(legacy)[0], "cursor")
        self.assertEqual(ownership_from(legacy)[1], "auto")

    def test_repair_and_pr_feedback_keep_original_executor(self):
        run = {"executor": "kiro", "model": None, "agent": "builder", "worktree": "/tmp/w", "project": self.config["projects"][0]}
        link = {}
        executor, model, agent = ownership_from(run, link)
        self.assertEqual((executor, model, agent), ("kiro", None, "builder"))
        link = {"executor": "kiro", "model": None, "agent": "builder"}
        source = {"executor": "cursor", "model": "auto"}
        self.assertEqual(ownership_from(source, link)[:2], ("kiro", None))


class KiroInvocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = dual_executors(self.root, agent="delivery")
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        self.store = Store(self.root / "state.db")
        self.addCleanup(self.store.close)
        self.runner = Runner(self.config, FakeTrello(), self.store)

    def test_kiro_argv_omits_model_when_unset_and_uses_worktree_cwd(self):
        run = {"run_id": "r1", "executor": "kiro", "model": None, "agent": "delivery",
               "worktree": str(self.worktree), "run_dir": str(self.run_dir)}
        argv = self.runner.executor_argv(run, "do the work")
        self.assertEqual(argv[:6], ["kiro-cli", "chat", "--no-interactive", "--trust-all-tools",
                                    "--trust-workspace", "--output-format"])
        self.assertEqual(argv[6], "stream-json")
        self.assertNotIn("--model", argv)
        self.assertEqual(argv[7:9], ["--agent", "delivery"])
        self.assertEqual(argv[-1], "do the work")
        seen = {}

        def capture(argv, cwd=None, run=None, timeout=120, interruptible=False):
            seen["argv"] = argv
            seen["cwd"] = cwd
            return '{"ok":true}'

        self.runner.command = capture
        self.runner.invoke(run, "do the work", "implementation", 10)
        self.assertEqual(seen["cwd"], str(self.worktree))
        self.assertEqual(seen["argv"], argv)
        self.assertEqual((self.run_dir / "implementation-prompt.txt").read_text(), "do the work")

    def test_kiro_passes_explicit_model_and_refuses_auto(self):
        run = {"executor": "kiro", "model": "kiro-model", "worktree": str(self.worktree), "run_dir": str(self.run_dir)}
        argv = self.runner.executor_argv(run, "prompt")
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "kiro-model")
        with self.assertRaisesRegex(Failure, "Refusing to pass Cursor auto"):
            self.runner.executor_argv({"executor": "kiro", "model": "auto", "worktree": str(self.worktree)}, "x")

    def test_cursor_argv_unchanged_and_requires_model(self):
        run = {"executor": "cursor", "model": "auto", "worktree": str(self.worktree), "run_dir": str(self.run_dir)}
        argv = self.runner.executor_argv(run, "prompt text")
        self.assertEqual(argv[0], self.config["executors"]["cursor"]["binary"])
        self.assertEqual(argv[1:5], ["--print", "--force", "--trust", "--model"])
        self.assertEqual(argv[5], "auto")
        self.assertIn("--workspace", argv)
        with self.assertRaisesRegex(Failure, "require an explicit model"):
            self.runner.executor_argv({"executor": "cursor", "model": None, "worktree": str(self.worktree)}, "x")

    def test_repair_reuses_recorded_kiro_executor(self):
        run = {"executor": "kiro", "model": None, "worktree": str(self.worktree), "run_dir": str(self.run_dir),
               "project": self.config["projects"][0], "card": card(), "card_id": "task", "branch": "kiro/task"}
        calls = []

        def fake_invoke(current, prompt, name, timeout):
            calls.append((current.get("executor"), recorded_model(current), name, self.runner.executor_argv(current, "x")[:6]))
            path = Path(current["run_dir"]) / f"result-{name}.json"
            path.write_text(json.dumps({"status": "complete", "summary": "fixed",
                                        "acceptance": [{"criterion": "Feature works", "passed": True, "evidence": "ok"}],
                                        "checks": ["ok"], "limitations": []}))

        attempts = {"n": 0}

        def verify(project, current):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise Failure("checks failed")

        self.runner.invoke = fake_invoke
        self.runner.verify = verify
        self.runner.assert_task_active = lambda current: None
        self.runner.publish = lambda project, current, report: current.update(status="review_pending")
        self.config["max_repairs"] = 1
        report = {"status": "complete", "summary": "done",
                  "acceptance": [{"criterion": "Feature works", "passed": True, "evidence": "ok"}],
                  "checks": ["ok"], "limitations": []}
        self.runner.complete_execution(run, "base prompt", report)
        self.assertEqual([c[0] for c in calls], ["kiro"])
        self.assertTrue(calls[0][2].startswith("repair-"))
        self.assertIsNone(calls[0][1])
        self.assertEqual(calls[0][3], ["kiro-cli", "chat", "--no-interactive", "--trust-all-tools",
                                        "--trust-workspace", "--output-format"])


class KiroGitDeliveryTests(unittest.TestCase):
    def test_end_to_end_kiro_delivers_pr_and_feedback_keeps_executor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = dual_executors(root)
            config["pull_requests"] = {"enabled": True, "debounce_seconds": 0}
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
            fake_kiro = root / "kiro-cli"
            fake_kiro.write_text('''#!/usr/bin/python3
import json, re, sys
from pathlib import Path
assert sys.argv[1:7] == ["chat", "--no-interactive", "--trust-all-tools", "--trust-workspace", "--output-format", "stream-json"]
assert "--model" not in sys.argv
prompt = sys.argv[-1]
report = re.search(r"When finished, write a JSON object to (.*?) with:", prompt)[1]
plan = re.search(r"Write the short plan to (.*?) before implementation", prompt)[1]
Path(plan).write_text("Implement feature and verify content.")
Path("feature.txt").write_text("implemented\\n")
Path(report).write_text(json.dumps({"status":"complete","summary":"Adds the requested feature.","acceptance":[{"criterion":"Feature works","passed":True,"evidence":"Content verified by a separate wrapper check"}],"checks":["fixture content check"],"limitations":[]}))
''')
            fake_kiro.chmod(0o700)
            skill = root / "skill"
            skill.mkdir()
            (skill / "SKILL.md").write_text("Fixture delivery contract.")
            config["executors"]["kiro"]["binary"] = str(fake_kiro)
            config["skill_dir"] = str(skill)
            config["projects"][0]["checks"] = [["python3", "-c", "from pathlib import Path; assert Path('feature.txt').read_text() == 'implemented\\n'"]]
            api = FakeTrello()
            api.cards["task"]["labels"] = [{"id": "k", "name": "ready_for_kiro"}]
            store = Store(root / "state.sqlite3")
            self.addCleanup(store.close)
            branch_holder = {}

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
                        return json.dumps({"url": run["pr_url"], "state": "OPEN", "baseRefName": "main",
                                           "headRefName": run["branch"], "headRefOid": head,
                                           "statusCheckRollup": [{"__typename": "CheckRun", "name": "quality",
                                                                  "status": "COMPLETED", "conclusion": "SUCCESS"}]})
                    return super().command(argv, cwd=cwd, run=run, **kwargs)

            runner = LocalRunner(config, api, store)
            result = runner.tick()
            self.assertEqual(result["status"], "review", store.latest("task")["message"])
            self.assertEqual(result["executor"], "kiro")
            run = store.latest("task")
            branch_holder["branch"] = run["branch"]
            self.assertEqual(run["executor"], "kiro")
            self.assertIsNone(run["model"])
            self.assertTrue(run["branch"].startswith("kiro/"))
            link = store.link(run["pr_url"])
            self.assertEqual(link["executor"], "kiro")
            self.assertIsNone(link["model"])
            prompt = (Path(run["run_dir"]) / "implementation-prompt.txt").read_text()
            self.assertIn("You are Kiro", prompt)
            self.assertIn("CLI default", prompt)

            worktree = Path(run["worktree"])
            gh = GitHub()
            gh.pr.update(head={"sha": run["head"], "ref": run["branch"], "repo": {"full_name": "acme/widget"}},
                         base={"ref": "main"})
            gh.checks[run["head"]] = [check()]
            gh.issues = [comment()]
            fake_kiro.write_text('''#!/usr/bin/python3
import json,re,sys
from pathlib import Path
assert sys.argv[1:7] == ["chat", "--no-interactive", "--trust-all-tools", "--trust-workspace", "--output-format", "stream-json"]
assert "--model" not in sys.argv
prompt=sys.argv[-1]
context=json.loads(Path(re.search(r"Address the PR feedback in (.*?) on the EXISTING branch",prompt)[1]).read_text())
report=re.search(r"When finished, write a JSON object to (.*?) with:",prompt)[1]
Path(re.search(r"Write the short plan to (.*?) before implementation",prompt)[1]).write_text("Fix the feedback and verify.")
Path('feature.txt').write_text('fixed\\n')
Path(report).write_text(json.dumps({'status':'complete','summary':'Fixes the boundary case.','acceptance':[{'criterion':'Feature works','passed':True,'evidence':'Content regression passed'}],'checks':['Content regression passed'],'feedback':[{'key':f['key'],'outcome':'addressed','response':'Fixed the boundary case.','evidence':'Regression passed'} for f in context['feedback']]}))
''')
            fake_kiro.chmod(0o700)
            config["projects"][0]["checks"] = [["python3", "-c", "from pathlib import Path; assert Path('feature.txt').read_text() == 'fixed\\n'"]]
            board = Board()
            board.cards["task"].update(idList="review", labels=[])
            seen = {"executor": None, "model": object()}

            class ReviewRunner(LifecycleRunner):
                def execute_review(self, current):
                    self.executions += 1
                    seen["executor"] = current["executor"]
                    seen["model"] = current["model"]
                    return Runner.execute_review(self, current)

                def command(self, argv, **kwargs):
                    current = kwargs.get("run")
                    if argv[:3] == ["git", "fetch", "origin"]:
                        argv = ["git", "fetch", str(bare), branch_holder["branch"]]
                    elif argv[:2] == ["git", "push"]:
                        argv = ["git", "push", str(bare), branch_holder["branch"]]
                        result = super().command(argv, **kwargs)
                        gh.pr["head"]["sha"] = git("--git-dir", str(bare), "rev-parse",
                                                   "refs/heads/" + branch_holder["branch"])
                        gh.checks[gh.pr["head"]["sha"]] = [check()]
                        return result
                    elif argv[:3] == ["gh", "pr", "view"]:
                        return json.dumps({"state": "OPEN", "baseRefName": "main",
                                           "headRefName": branch_holder["branch"],
                                           "headRefOid": gh.pr["head"]["sha"],
                                           "statusCheckRollup": [{"__typename": "CheckRun", "name": "quality",
                                                                  "status": "COMPLETED", "conclusion": "SUCCESS"}]})
                    return super().command(argv, **kwargs)

            review_runner = ReviewRunner(config, board, store, gh)
            review_runner.tick()
            follow = store.latest("task")
            self.assertEqual(follow["status"], "review", follow["message"])
            self.assertEqual(seen["executor"], "kiro")
            self.assertIsNone(seen["model"])
            self.assertEqual(follow["executor"], "kiro")
            self.assertIsNone(follow["model"])
            self.assertEqual((worktree / "feature.txt").read_text(), "fixed\n")
            self.assertEqual(review_runner.executions, 1)


if __name__ == "__main__":
    unittest.main()
