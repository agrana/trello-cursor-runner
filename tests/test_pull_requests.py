import copy
import datetime as dt
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from runner import Failure, Runner, Store, ci_state
from test_runner import FakeTrello, card, configuration


def check(conclusion="success", name="quality", ident=1):
    return {"id": ident, "name": name, "status": "completed", "conclusion": conclusion, "app": {"id": 1}}


def comment(ident=1, body="Please fix the boundary case.", **extra):
    return {"id": ident, "body": body, "user": {"login": "reviewer"}, "author_association": "OWNER",
            "updated_at": "2026-01-01T00:00:00Z", "created_at": "2026-01-01T00:00:00Z", **extra}


class Board(FakeTrello):
    def __init__(self):
        super().__init__()
        self.attachments = []
        self.fail_review_move = False

    def request(self, method, path, **params):
        if path.endswith("/attachments"):
            if method == "GET":
                return copy.deepcopy(self.attachments)
            self.attachments.append(params)
            return params
        if method == "PUT" and params.get("idList") == "review" and self.fail_review_move:
            self.fail_review_move = False
            raise Failure("Simulated final card move failure")
        return super().request(method, path, **params)


class GitHub:
    def __init__(self):
        self.pr = {"state": "open", "merged": False, "merge_commit_sha": None, "body": "Trello: https://trello.com/c/task",
                   "head": {"sha": "a" * 40, "ref": "cursor/task", "repo": {"full_name": "acme/widget"}},
                   "base": {"ref": "main"}}
        self.issues = []
        self.inline = []
        self.reviews = []
        self.thread_states = {}
        self.checks = {"a" * 40: [check()], "b" * 40: [check()]}
        self.resolutions = []
        self.metadata_reads = 0
        self.lose_post = False

    def command(self, argv):
        endpoint = argv[2]
        fields = dict(argv[i + 1].split("=", 1) for i, token in enumerate(argv) if token == "-f")
        method = argv[argv.index("--method") + 1] if "--method" in argv else "POST"
        if endpoint == "graphql":
            if "--input" in argv:
                payload = json.loads(Path(argv[argv.index("--input") + 1]).read_text())
                # Pagination is deliberately exercised even for a small fixture.
                cursor = payload["variables"]["cursor"]
                nodes = [{"id": tid, "isResolved": resolved, "comments": {"nodes": [{"databaseId": root}]}}
                         for root, (tid, resolved) in self.thread_states.items()]
                connection = {"nodes": nodes[:1] if cursor is None else nodes[1:],
                              "pageInfo": {"hasNextPage": cursor is None and len(nodes) > 1, "endCursor": "next"}}
                return {"data": {"repository": {"pullRequest": {"reviewThreads": connection}}}}
            for root, (tid, resolved) in self.thread_states.items():
                if tid == fields["id"]:
                    self.thread_states[root] = (tid, True)
                    self.resolutions.append(tid)
                    return {"data": {"resolveReviewThread": {"thread": {"id": tid, "isResolved": True}}}}
            raise AssertionError(fields)
        if endpoint.endswith("/pulls/1"):
            self.metadata_reads += 1
            return copy.deepcopy(self.pr)
        if "/commits/" in endpoint:
            sha = endpoint.split("/commits/")[1].split("/")[0]
            return [{"check_runs": self.checks.get(sha, [])}] if "/check-runs" in endpoint else [{"statuses": []}]
        if method == "POST":
            value = comment(1000 + len(self.issues) + len(self.inline), fields["body"])
            if "/replies" in endpoint:
                value["in_reply_to_id"] = int(endpoint.split("/comments/")[1].split("/")[0])
                self.inline.append(value)
            else:
                self.issues.append(value)
            if self.lose_post:
                self.lose_post = False
                raise Failure("Write succeeded but response was lost")
            return value
        values = self.issues if "/issues/" in endpoint else self.reviews if "/reviews" in endpoint else self.inline
        return [copy.deepcopy(values[:1]), copy.deepcopy(values[1:])]


class LifecycleRunner(Runner):
    def __init__(self, config, api, store, github):
        super().__init__(config, api, store)
        self.github = github
        self.executions = 0

    def command(self, argv, **kwargs):
        if argv[:2] == ["gh", "api"]:
            return json.dumps(self.github.command(argv))
        return super().command(argv, **kwargs)

    def execute_review(self, run):
        self.executions += 1
        self.github.pr["head"]["sha"] = "b" * 40
        run.update(head="b" * 40, status="feedback_pending", message="Feedback verified.",
                   feedback_responses={f["key"]: {"outcome": "addressed", "response": "Fixed and checked.", "evidence": "Regression test passed."}
                                       for f in run["feedback"]})
        self.store.save(run)


class PullRequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = configuration(root)
        self.config["pull_requests"] = {"enabled": True, "debounce_seconds": 0}
        self.api = Board()
        self.api.cards["task"].update(idList="review", labels=[])
        self.gh = GitHub()
        self.store = Store(root / "state.sqlite3")
        self.addCleanup(self.store.close)
        self.runner = LifecycleRunner(self.config, self.api, self.store, self.gh)
        self.source = {"run_id": "initial", "card_id": "task", "card": card(), "status": "review", "started": "2026-01-01T00:00:00+00:00",
                       "project": self.config["projects"][0], "head": "a" * 40, "branch": "cursor/task", "worktree": str(root / "worktree"),
                       "pr_url": "https://github.com/acme/widget/pull/1", "run_dir": str(root), "message": "Initial delivery."}
        self.store.save(self.source)

    def merged(self, conclusion="success"):
        self.gh.pr.update(state="closed", merged=True, merge_commit_sha="c" * 40)
        self.gh.checks["c" * 40] = [check(conclusion)]

    def test_links_are_bidirectional_and_idempotent(self):
        self.gh.pr["body"] = "No card link yet"
        self.runner.tick()
        self.runner.tick()
        self.assertEqual(len(self.api.attachments), 1)
        self.assertEqual(len(self.gh.issues), 1)
        self.assertIn("https://trello.com/c/task", self.gh.issues[0]["body"])
        self.assertIsNotNone(self.store.link(self.source["pr_url"]))
        self.assertEqual(self.runner.executions, 0)
        self.assertEqual(self.gh.metadata_reads, 2)

    def test_comments_resume_same_pr_and_are_not_reprocessed(self):
        self.gh.issues = [comment()]
        self.runner.tick()
        latest = self.store.latest("task")
        self.assertEqual(latest["status"], "review")
        self.assertEqual(latest["pr_url"], self.source["pr_url"])
        self.assertEqual(latest["branch"], self.source["branch"])
        self.assertEqual(latest["worktree"], self.source["worktree"])
        self.runner.tick()
        self.assertEqual(self.runner.executions, 1)
        self.assertEqual(len(self.gh.issues), 2)

    def test_edited_comment_is_a_new_feedback_batch(self):
        self.gh.issues = [comment()]
        self.runner.tick()
        self.gh.issues[0]["body"] = "Please also handle the empty value."
        self.runner.tick()
        self.assertEqual(self.runner.executions, 2)

    def test_untrusted_comments_and_draft_reviews_do_not_launch(self):
        self.gh.issues = [comment(author_association="NONE")]
        self.gh.inline = [comment(2, pull_request_review_id=5)]
        self.gh.reviews = [comment(5, state="PENDING")]
        self.gh.thread_states = {2: ("thread", False)}
        self.runner.tick()
        self.assertEqual(self.runner.executions, 0)

    def test_explicitly_allowed_bot_can_supply_feedback(self):
        self.config["pull_requests"]["allowed_reviewers"] = ["review-bot"]
        self.gh.issues = [comment(author_association="NONE", user={"login": "review-bot"})]
        self.runner.tick()
        self.assertEqual(self.runner.executions, 1)

    def test_published_review_body_is_handled_once(self):
        self.gh.reviews = [comment(5, "Approved, please answer this question.", state="APPROVED")]
        self.runner.tick()
        self.runner.tick()
        self.assertEqual(self.runner.executions, 1)
        self.assertEqual(len(self.gh.issues), 1)

    def test_paginated_inline_threads_resolve_after_verification(self):
        self.gh.inline = [comment(1), comment(2)]
        self.gh.thread_states = {1: ("thread-1", False), 2: ("thread-2", False)}
        self.runner.tick()
        self.assertEqual(set(self.gh.resolutions), {"thread-1", "thread-2"})
        self.assertEqual(len(self.gh.inline), 4)

    def test_new_comment_during_run_prevents_premature_thread_resolution(self):
        self.gh.inline = [comment(1)]
        self.gh.thread_states = {1: ("thread-1", False)}
        original = self.runner.execute_review
        def execute(run):
            original(run)
            self.gh.inline.append(comment(2, "One more point.", in_reply_to_id=1))
        self.runner.execute_review = execute
        self.runner.tick()
        self.assertEqual(self.gh.resolutions, [])
        self.assertEqual(self.store.latest("task")["status"], "review")

    def test_reply_response_loss_recovers_without_new_execution_or_duplicate_reply(self):
        self.gh.issues = [comment()]
        self.gh.lose_post = True
        with self.assertRaises(Failure):
            self.runner.tick()
        self.assertEqual(self.store.latest("task")["status"], "feedback_pending")
        self.runner.tick()
        self.assertEqual(self.runner.executions, 1)
        self.assertEqual(len(self.gh.issues), 2)
        self.assertEqual(self.store.latest("task")["status"], "review")

    def test_final_trello_failure_recovers_without_new_execution(self):
        self.gh.issues = [comment()]
        self.api.fail_review_move = True
        with self.assertRaises(Failure):
            self.runner.tick()
        self.runner.tick()
        self.assertEqual(self.runner.executions, 1)
        self.assertEqual(self.store.latest("task")["status"], "review")

    def test_stale_pr_head_never_resolves_threads(self):
        self.gh.inline = [comment(1)]
        self.gh.thread_states = {1: ("thread-1", False)}
        original = self.runner.execute_review
        def execute(run):
            original(run)
            self.gh.pr["head"]["sha"] = "d" * 40
        self.runner.execute_review = execute
        self.runner.tick()
        self.assertEqual(self.gh.resolutions, [])
        self.assertEqual(self.store.latest("task")["status"], "blocked")

    def test_owner_move_before_feedback_delivery_cancels_resolution(self):
        self.gh.inline = [comment(1)]
        self.gh.thread_states = {1: ("thread-1", False)}
        original = self.runner.execute_review
        def execute(run):
            original(run)
            self.api.cards["task"]["idList"] = "backlog"
        self.runner.execute_review = execute
        self.runner.tick()
        self.assertEqual(self.gh.resolutions, [])
        self.assertEqual(self.store.latest("task")["status"], "cancelled")

    def test_failed_ci_prevents_feedback_resolution(self):
        self.gh.inline = [comment(1)]
        self.gh.thread_states = {1: ("thread-1", False)}
        self.gh.checks["b" * 40] = [check("failure")]
        self.runner.tick()
        self.assertEqual(self.gh.resolutions, [])
        self.assertEqual(len(self.gh.inline), 1)
        self.assertEqual(self.store.latest("task")["status"], "blocked")

    def test_feedback_ci_rerun_delivers_pending_replies_without_reimplementation(self):
        self.gh.inline = [comment(1)]
        self.gh.thread_states = {1: ("thread-1", False)}
        self.gh.checks["b" * 40] = [check("failure")]
        self.runner.tick()
        self.gh.checks["b" * 40] = [check("success", ident=2)]
        self.runner.tick()
        self.assertEqual(self.gh.resolutions, ["thread-1"])
        self.assertEqual(self.runner.executions, 1)
        self.assertEqual(self.store.latest("task")["status"], "review")

    def test_closed_unmerged_pr_blocks_card_and_stops_polling(self):
        self.gh.pr["state"] = "closed"
        self.runner.tick()
        self.assertEqual(self.api.cards["task"]["idList"], "blocked")
        reads = self.gh.metadata_reads
        self.runner.tick()
        self.assertEqual(self.gh.metadata_reads, reads)
        self.assertEqual(self.store.latest("task")["status"], "blocked")

    def test_open_pr_with_passing_ci_never_completes_card(self):
        self.runner.tick()
        self.assertEqual(self.api.cards["task"]["idList"], "review")

    def test_merged_pr_waits_for_both_final_head_and_merge_ci(self):
        self.merged()
        self.gh.checks["c" * 40] = []
        self.runner.tick()
        self.assertEqual(self.api.cards["task"]["idList"], "review")
        self.gh.checks["c" * 40] = [check()]
        self.gh.checks["a" * 40] = []
        self.runner.tick()
        self.assertEqual(self.api.cards["task"]["idList"], "review")
        self.gh.checks["a" * 40] = [check()]
        self.runner.tick()
        self.assertEqual(self.api.cards["task"]["idList"], "done")
        self.assertEqual(self.store.latest("task")["status"], "done")

    def test_failed_merge_ci_blocks_then_successful_rerun_completes(self):
        self.merged("failure")
        self.runner.tick()
        self.assertEqual(self.api.cards["task"]["idList"], "blocked")
        self.assertEqual(self.runner.executions, 0)
        self.gh.checks["c" * 40] = [check("success", ident=2), check("failure", ident=1)]
        self.runner.tick()
        self.assertEqual(self.api.cards["task"]["idList"], "done")

    def test_done_move_failure_rechecks_ci_before_retry(self):
        self.merged()
        self.api.fail_move = True
        with self.assertRaises(Failure):
            self.runner.tick()
        self.assertEqual(self.store.latest("task")["status"], "done_pending")
        self.gh.checks["c" * 40] = [check("failure", ident=2)]
        self.runner.tick()
        self.assertEqual(self.api.cards["task"]["idList"], "blocked")
        self.assertNotEqual(self.store.latest("task")["status"], "done")

    def test_missing_ci_is_not_a_merge_success_even_with_no_ci_opt_in(self):
        self.source["project"].update(required_checks=[], allow_no_ci=True)
        self.store.save(self.source)
        self.merged()
        self.gh.checks.clear()
        self.runner.tick()
        self.assertEqual(self.api.cards["task"]["idList"], "review")

    def test_all_skipped_jobs_are_not_passing_merge_evidence(self):
        self.source["project"]["required_checks"] = []
        self.store.save(self.source)
        self.merged("skipped")
        self.gh.checks["a" * 40] = [check("skipped")]
        self.runner.tick()
        self.assertEqual(self.api.cards["task"]["idList"], "review")

    def test_external_owner_move_and_wrong_repository_are_preserved(self):
        self.merged()
        self.api.cards["task"]["idList"] = "backlog"
        self.runner.tick()
        self.assertEqual(self.api.cards["task"]["idList"], "backlog")
        self.gh.pr["head"]["repo"]["full_name"] = "other/repo"
        with self.assertRaises(Failure):
            self.runner.tick()

    def test_known_failure_wins_even_when_another_required_check_is_missing(self):
        self.assertEqual(ci_state([{"__typename": "CheckRun", "name": "other", "status": "COMPLETED", "conclusion": "FAILURE"}], ["quality"]), "failed")


class RealReviewDeliveryTests(unittest.TestCase):
    def test_real_git_feedback_updates_existing_pr_without_replacing_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = configuration(root)
            config["pull_requests"] = {"enabled": True, "debounce_seconds": 0}
            source = root / "source"
            source.mkdir()
            def git(*args, cwd=source):
                return subprocess.check_output(["git", *args], cwd=cwd, stderr=subprocess.STDOUT, text=True).strip()
            git("init", "-b", "main")
            git("config", "user.name", "Fixture")
            git("config", "user.email", "fixture@example.invalid")
            (source / "feature.txt").write_text("old\n")
            git("add", ".")
            git("commit", "-m", "Initial")
            worktree = root / "worktree"
            git("worktree", "add", "-b", "cursor/task", str(worktree))
            bare = root / "remote.git"
            git("clone", "--bare", str(source), str(bare))
            git("remote", "add", "origin", "https://github.com/acme/widget.git")
            gh = GitHub()
            gh.pr["head"]["sha"] = git("rev-parse", "HEAD", cwd=worktree)
            gh.checks[gh.pr["head"]["sha"]] = [check()]
            gh.issues = [comment()]
            body = gh.pr["body"]
            cursor = root / "cursor"
            cursor.write_text('''#!/usr/bin/python3
import json,re,sys
from pathlib import Path
prompt=sys.argv[-1]
context=json.loads(Path(re.search(r"Address the PR feedback in (.*?) on the EXISTING branch",prompt)[1]).read_text())
report=re.search(r"When finished, write a JSON object to (.*?) with:",prompt)[1]
Path(re.search(r"Write the short plan to (.*?) before implementation",prompt)[1]).write_text("Fix the feedback and verify.")
Path('feature.txt').write_text('fixed\\n')
Path(report).write_text(json.dumps({'status':'complete','summary':'Fixes the boundary case.','acceptance':[{'criterion':'Boundary handled','passed':True,'evidence':'Content regression passed'}],'checks':['Content regression passed'],'feedback':[{'key':f['key'],'outcome':'addressed','response':'Fixed the boundary case.','evidence':'Regression passed'} for f in context['feedback']]}))
''')
            cursor.chmod(0o700)
            skill = root / "skill"
            skill.mkdir()
            (skill / "SKILL.md").write_text("Fixture delivery contract")
            config.update(cursor_binary=str(cursor), skill_dir=str(skill))
            config["projects"][0]["checks"] = [["python3", "-c", "from pathlib import Path; assert Path('feature.txt').read_text() == 'fixed\\n'"]]
            board = Board()
            board.cards["task"].update(idList="review", labels=[])
            store = Store(root / "state.sqlite3")
            self.addCleanup(store.close)
            store.save({"run_id":"initial", "card_id":"task", "card":card(), "status":"review", "started":"2026-01-01T00:00:00+00:00",
                        "project":config["projects"][0], "branch":"cursor/task", "worktree":str(worktree),
                        "pr_url":"https://github.com/acme/widget/pull/1", "head":gh.pr["head"]["sha"], "run_dir":str(root), "message":"Initial"})

            class LocalRunner(LifecycleRunner):
                def execute_review(self, run):
                    self.executions += 1
                    return Runner.execute_review(self, run)

                def command(self, argv, **kwargs):
                    if argv[:3] == ["git", "fetch", "origin"]:
                        argv = ["git", "fetch", str(bare), "cursor/task"]
                    elif argv[:2] == ["git", "push"]:
                        argv = ["git", "push", str(bare), "cursor/task"]
                        result = super().command(argv, **kwargs)
                        gh.pr["head"]["sha"] = git("--git-dir", str(bare), "rev-parse", "refs/heads/cursor/task")
                        gh.checks[gh.pr["head"]["sha"]] = [check()]
                        return result
                    elif argv[:3] == ["gh", "pr", "view"]:
                        return json.dumps({"state":"OPEN", "baseRefName":"main", "headRefName":"cursor/task", "headRefOid":gh.pr["head"]["sha"],
                                           "statusCheckRollup":[{"__typename":"CheckRun", "name":"quality", "status":"COMPLETED", "conclusion":"SUCCESS"}]})
                    return super().command(argv, **kwargs)

            runner = LocalRunner(config, board, store, gh)
            runner.tick()
            self.assertEqual(store.latest("task")["status"], "review", store.latest("task")["message"])
            self.assertEqual((worktree / "feature.txt").read_text(), "fixed\n")
            self.assertEqual((source / "feature.txt").read_text(), "old\n")
            self.assertEqual(gh.pr["body"], body)
            self.assertEqual(store.latest("task")["pr_url"], "https://github.com/acme/widget/pull/1")
            runner.tick()
            self.assertEqual(runner.executions, 1)


if __name__ == "__main__":
    unittest.main()
