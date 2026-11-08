"""Durable linked-PR monitoring, feedback delivery and merge/close reconciliation."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import time
import uuid


MARKER = "[trello-cursor:"


def feedback_fingerprint(item):
    value = [item.get(k) for k in ("body", "updated_at", "path", "line")]
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class PullRequests:
    def __init__(self, runner, failure, check_state, timestamp):
        self.r = runner
        self.fail = failure
        self.check_state = check_state
        self.now = timestamp
        self.settings = runner.config.get("pull_requests", {})

    @property
    def enabled(self):
        return self.settings.get("enabled", False)

    def api(self, endpoint, method="GET", fields=None, paginate=False, run=None):
        argv = ["gh", "api", endpoint, "--method", method]
        if paginate:
            argv += ["--paginate", "--slurp"]
        for key, value in (fields or {}).items():
            argv += ["-f", f"{key}={value}"]
        return json.loads(self.r.command(argv, run=run))

    def listing(self, endpoint):
        pages = self.api(endpoint + ("&" if "?" in endpoint else "?") + "per_page=100", paginate=True)
        return [item for page in pages for item in page]

    def register(self, run):
        url = run.get("pr_url", "")
        match = re.fullmatch(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)", url)
        project = run.get("project")
        if not match or not project or match[1] != project["repository"]:
            raise self.fail("PR link does not match the assigned repository")
        link = self.r.store.link(url)
        if link:
            if link["card_id"] != run["card_id"] or link["branch"] != run["branch"]:
                raise self.fail("PR is already linked to a different card or branch")
            return link
        link = {"pr_url": url, "card_id": run["card_id"], "repository": match[1], "number": int(match[2]),
                "branch": run["branch"], "base": project["base"], "processed": {}, "rounds": 0, "status": "open"}
        self.r.store.save_link(link)
        return link

    def metadata(self, link):
        pr = self.api(f"repos/{link['repository']}/pulls/{link['number']}")
        if (pr["head"]["ref"] != link["branch"] or pr["base"]["ref"] != link["base"]
                or (pr["head"].get("repo") or {}).get("full_name") != link["repository"]):
            raise self.fail("Linked PR repository/base/head branch changed; manual reconciliation required")
        return pr

    def ensure_links(self, link, pr, card):
        attachments = self.r.api.request("GET", f"/cards/{link['card_id']}/attachments", fields="url,name")
        if not any(a.get("url", "").rstrip("/") == link["pr_url"].rstrip("/") for a in attachments):
            self.r.api.request("POST", f"/cards/{link['card_id']}/attachments", url=link["pr_url"], name="Pull request")
        card_url = card.get("shortUrl")
        if card_url and card_url not in (pr.get("body") or ""):
            marker = f"{MARKER}card:{link['card_id']}]"
            comments = self.listing(f"repos/{link['repository']}/issues/{link['number']}/comments")
            if not any(marker in c.get("body", "") for c in comments):
                self.api(f"repos/{link['repository']}/issues/{link['number']}/comments", "POST",
                         {"body": f"{marker}\nLinked Trello task: {card_url}"})

    def threads(self, link):
        owner, name = link["repository"].split("/")
        query = """query($owner:String!,$name:String!,$number:Int!,$cursor:String){
 repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100,after:$cursor){
 nodes{id isResolved comments(first:1){nodes{databaseId}}} pageInfo{hasNextPage endCursor}}}}}"""
        cursor = None
        result = {}
        while True:
            variables = {"owner": owner, "name": name, "number": link["number"], "cursor": cursor}
            # --input keeps GraphQL integer/null types intact.
            with self.r.input_json({"query": query, "variables": variables}) as path:
                response = json.loads(self.r.command(["gh", "api", "graphql", "--input", str(path)]))
            if response.get("errors"):
                raise self.fail("GitHub could not read PR review threads")
            connection = response["data"]["repository"]["pullRequest"]["reviewThreads"]
            for thread in connection["nodes"]:
                roots = thread["comments"]["nodes"]
                if roots:
                    result[roots[0]["databaseId"]] = {"id": thread["id"], "resolved": thread["isResolved"]}
            if not connection["pageInfo"]["hasNextPage"]:
                return result
            cursor = connection["pageInfo"]["endCursor"]

    def trusted(self, item):
        login = (item.get("user") or {}).get("login", "")
        allowed = self.settings.get("allowed_reviewers", [])
        associations = self.settings.get("trusted_associations", ["OWNER", "MEMBER", "COLLABORATOR"])
        return login in allowed or item.get("author_association") in associations

    def feedback(self, link):
        root = f"repos/{link['repository']}"
        inline = self.listing(f"{root}/pulls/{link['number']}/comments")
        issues = self.listing(f"{root}/issues/{link['number']}/comments")
        reviews = self.listing(f"{root}/pulls/{link['number']}/reviews")
        states = {r["id"]: r["state"] for r in reviews}
        threads = self.threads(link)
        result = []
        for kind, items in (("inline", inline), ("comment", issues), ("review", reviews)):
            for item in items:
                body = item.get("body") or ""
                if not body.strip() or MARKER in body or not self.trusted(item):
                    continue
                if kind == "review" and item.get("state") not in {"COMMENTED", "CHANGES_REQUESTED", "APPROVED"}:
                    continue
                thread = None
                root_id = item.get("in_reply_to_id", item["id"])
                if kind == "inline":
                    review_id = item.get("pull_request_review_id")
                    if review_id and states.get(review_id) not in {"COMMENTED", "CHANGES_REQUESTED", "APPROVED"}:
                        continue  # Draft/dismissed/unknown reviews do not release execution.
                    thread = threads.get(root_id)
                    if not thread:
                        raise self.fail("Published inline comment has no readable review thread")
                    if thread["resolved"]:
                        continue
                key = f"{kind}:{item['id']}"
                fingerprint = feedback_fingerprint(item)
                if link.get("processed", {}).get(key) == fingerprint:
                    continue
                result.append({"key": key, "fingerprint": fingerprint, "body": body, "author": item["user"]["login"],
                               "updated_at": item.get("updated_at") or item.get("submitted_at") or item.get("created_at"),
                               "path": item.get("path"), "line": item.get("line"), "thread_id": thread["id"] if thread else None,
                               "root_id": root_id, "kind": kind})
        return result

    def commit_checks(self, repository, sha):
        pages = self.api(f"repos/{repository}/commits/{sha}/check-runs?per_page=100", paginate=True)
        latest = {}
        for page in pages:
            for check in page["check_runs"]:
                key = (check["name"], check.get("app", {}).get("id"))
                if key not in latest or check["id"] > latest[key]["id"]:
                    latest[key] = check
        result = [{"__typename": "CheckRun", "name": c["name"], "status": c["status"].upper(),
                   "conclusion": (c.get("conclusion") or "").upper()} for c in latest.values()]
        pages = self.api(f"repos/{repository}/commits/{sha}/status?per_page=100", paginate=True)
        statuses = {}
        for page in pages:
            for status in page["statuses"]:
                if status["context"] not in statuses:
                    statuses[status["context"]] = status
        result += [{"__typename": "StatusContext", "context": s["context"], "state": s["state"].upper()} for s in statuses.values()]
        return result

    def merge_states(self, link, pr, project):
        required = project.get("required_checks", [])
        def state(sha):
            checks = self.commit_checks(link["repository"], sha)
            value = self.check_state(checks, required)
            if value == "passed" and not any(c.get("conclusion", c.get("state")) == "SUCCESS" for c in checks):
                return "pending"  # A set of skipped/neutral jobs is not passing CI evidence.
            return value
        head = state(pr["head"]["sha"])
        merge_sha = pr.get("merge_commit_sha")
        merged = state(merge_sha) if merge_sha else "pending"
        return head, merged

    def transition(self, link, run, target, reason, pr):
        run["lifecycle_transition"] = {"target": target, "reason": reason, "pr_head": pr["head"]["sha"],
                                       "merge_sha": pr.get("merge_commit_sha")}
        run["status"] = "done_pending" if target == "done" else "lifecycle_blocked_pending"
        self.r.store.save(run)
        self.finish_transition(run)

    def finish_transition(self, run):
        link = self.r.store.link(run["pr_url"])
        pr = self.metadata(link)
        transition = run["lifecycle_transition"]
        target = transition["target"]
        if target == "done":
            states = self.merge_states(link, pr, run["project"])
            if not pr.get("merged") or states != ("passed", "passed"):
                # A changed/rerun check revokes a pending Done move.
                run["status"] = "review"
                self.r.store.save(run)
                return
        elif pr.get("merged"):
            if "failed" not in self.merge_states(link, pr, run["project"]):
                run["status"] = "review"
                self.r.store.save(run)
                return
        elif pr["state"] != "closed":
            run["status"] = "review"
            self.r.store.save(run)
            return
        card = self.r.api.card(run["card_id"])
        permitted = {self.r.config["lists"][k] for k in ("in_progress", "review", "blocked", "done")}
        if card.get("closed") or card["idBoard"] != self.r.config["board_id"] or card["idList"] not in permitted:
            run["status"] = "cancelled"
            self.r.store.save(run)
            return
        marker = f"[cursor-pr:{link['number']}:{target}:{pr.get('merge_commit_sha') or pr['head']['sha']}]"
        self.r.api.comment_once(run["card_id"], marker,
                                f"{transition['reason']}\nPR: {run['pr_url']}\nFinal PR head: {pr['head']['sha']}\nMerge commit: {pr.get('merge_commit_sha') or 'not merged'}")
        if card["idList"] != self.r.config["lists"][target]:
            self.r.api.request("PUT", f"/cards/{run['card_id']}", idList=self.r.config["lists"][target])
        run["status"] = target
        self.r.store.save(run)
        link["status"] = "done" if target == "done" else ("merged_ci_failed" if pr.get("merged") else "closed")
        self.r.store.save_link(link)
        self.r.telemetry()

    def poll(self, execute=True):
        if not self.enabled:
            return False
        for run in self.r.store.all():
            if run.get("pr_url") and run.get("project"):
                self.register(run)
        for link in self.r.store.links():
            if link["status"] in {"closed", "done"}:
                continue
            source = self.r.store.latest(link["card_id"])
            if not source or source.get("pr_url") != link["pr_url"]:
                continue
            card = self.r.api.card(link["card_id"])
            if card.get("closed") or card["idBoard"] != self.r.config["board_id"]:
                continue
            pr = self.metadata(link)
            self.ensure_links(link, pr, card)
            if pr.get("merged"):
                states = self.merge_states(link, pr, source["project"])
                link["merge_ci"] = {"head": states[0], "merge": states[1]}
                self.r.store.save_link(link)
                if states == ("passed", "passed"):
                    self.transition(link, source, "done", "PR merged; CI passed on both the final PR head and merged commit.", pr)
                elif "failed" in states:
                    if source["status"] != "blocked" or link["status"] != "merged_ci_failed":
                        self.transition(link, source, "blocked", "PR merged but CI failed. Inspect the failing checks; merged-commit CI monitoring continues.", pr)
                else:
                    link["status"] = "merged_ci_pending"
                    self.r.store.save_link(link)
                    self.r.api.comment_once(link["card_id"], f"[cursor-pr:{link['number']}:merge-ci-pending:{pr.get('merge_commit_sha')}]",
                                            f"PR merged; waiting for passing CI before Done. Final PR head: {states[0]}; merged commit: {states[1]}.\n{link['pr_url']}")
                continue
            if pr["state"] == "closed":
                self.transition(link, source, "blocked", "PR closed without merging. Check why it was closed before continuing this task.", pr)
                continue
            if source["status"] == "blocked" and source.get("feedback_delivery_pending"):
                checks = self.commit_checks(link["repository"], pr["head"]["sha"])
                if self.check_state(checks, source["project"].get("required_checks", [])) == "passed":
                    self.finish_feedback(source)
                    if source["status"] == "review_pending":
                        self.r.finalize(source)
                    continue
            if not execute or source["status"] not in {"review", "blocked"} or card["idList"] not in {
                self.r.config["lists"]["review"], self.r.config["lists"]["blocked"]
            }:
                continue
            feedback = self.feedback(link)
            if not feedback:
                continue
            batch = hashlib.sha256(json.dumps([(f["key"], f["fingerprint"]) for f in sorted(feedback, key=lambda f: f["key"])]).encode()).hexdigest()
            if link.get("attempted_batch") == batch:
                continue
            updated = [dt.datetime.fromisoformat(f["updated_at"].replace("Z", "+00:00")).timestamp() for f in feedback if f.get("updated_at")]
            if updated and time.time() - max(updated) < self.settings.get("debounce_seconds", 60):
                continue
            if link["rounds"] >= self.settings.get("max_review_rounds", 10):
                self.r.api.comment_once(link["card_id"], f"[cursor-pr:{link['number']}:review-limit]",
                                        "Automatic review-round limit reached. Inspect feedback and use an explicit handoff or raise the local limit.")
                continue
            self.start_review(link, source, card, pr, feedback, batch)
            return True
        return False

    def start_review(self, link, source, card, pr, feedback, batch):
        run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        folder = self.r.state / "runs" / run_id
        folder.mkdir(parents=True, mode=0o700)
        run = {"run_id": run_id, "card_id": card["id"], "card": card, "status": "running", "kind": "review",
               "started": self.now(), "model": self.r.config["model"], "run_dir": str(folder), "branch": link["branch"],
               "worktree": source["worktree"], "project": source["project"], "pr_url": link["pr_url"],
               "expected_head": pr["head"]["sha"], "feedback": feedback, "message": ""}
        self.r.store.save(run)
        link.update(attempted_batch=batch, rounds=link["rounds"] + 1)
        self.r.store.save_link(link)
        self.r.deadline = time.monotonic() + self.r.config.get("run_timeout_seconds", 7200)
        try:
            self.r.api.request("PUT", f"/cards/{card['id']}", idList=self.r.config["lists"]["in_progress"])
            self.r.api.comment_once(card["id"], f"[cursor-run:{run_id}:claim]",
                                    f"Cursor is addressing {len(feedback)} new PR feedback item(s) on the existing branch.\n" + self.r.location(run))
            self.r.execute_review(run)
        except (self.fail, OSError, ValueError, KeyError) as exc:
            run.update(status="blocked_pending", finished=self.now(), message=f"PR feedback blocked: {exc}\n" + self.r.location(run))
            self.r.store.save(run)
        if run["status"] == "feedback_pending":
            self.finish_feedback(run)
        if run["status"] in {"blocked_pending", "review_pending"}:
            self.r.finalize(run)

    def finish_feedback(self, run):
        link = self.r.store.link(run["pr_url"])
        pr = self.metadata(link)
        if pr.get("merged") or pr["state"] != "open":
            # Merge/close reconciliation owns the next move; no stale thread resolutions.
            run["status"] = "review"
            self.r.store.save(run)
            return
        if pr["head"]["sha"] != run["head"]:
            run.update(status="blocked_pending", feedback_delivery_pending=False,
                       message="PR head changed before feedback delivery; stale threads were not resolved.\n" + self.r.location(run))
            self.r.store.save(run)
            return
        card = self.r.api.card(run["card_id"])
        permitted = {self.r.config["lists"][key] for key in ("in_progress", "review", "blocked")}
        if card.get("closed") or card["idBoard"] != self.r.config["board_id"] or card["idList"] not in permitted:
            run.update(status="cancelled", feedback_delivery_pending=False)
            self.r.store.save(run)
            return
        if any(card.get(key) != run["card"].get(key) for key in ("name", "desc")):
            run.update(status="blocked_pending", feedback_delivery_pending=False,
                       message="Task specification changed before feedback delivery; owner reconciliation required.")
            self.r.store.save(run)
            return
        checks = self.commit_checks(link["repository"], run["head"])
        state = self.check_state(checks, run["project"].get("required_checks", []))
        if state != "passed" and not (not checks and run["project"].get("allow_no_ci") and not run["project"].get("required_checks")):
            if state == "failed":
                run.update(status="blocked_pending", feedback_delivery_pending=True,
                           message="CI failed before PR feedback could be resolved.\n" + self.r.location(run))
                self.r.store.save(run)
            return
        root = f"repos/{link['repository']}"
        inline = self.listing(f"{root}/pulls/{link['number']}/comments")
        issues = self.listing(f"{root}/issues/{link['number']}/comments")
        for item in run["feedback"]:
            response = run["feedback_responses"][item["key"]]
            marker = f"{MARKER}{run['run_id']}:{item['key']}:{item['fingerprint']}]"
            body = f"{marker}\n{response['response']}\n\nEvidence: {response['evidence']}\nVerified commit: {run['head']}"
            if not any(marker in c.get("body", "") for c in inline + issues):
                endpoint = (f"{root}/pulls/{link['number']}/comments/{item['root_id']}/replies" if item["kind"] == "inline"
                            else f"{root}/issues/{link['number']}/comments")
                self.api(endpoint, "POST", {"body": body}, run=run)
        # Re-read after replies: new human feedback on a thread prevents premature resolution.
        outstanding = self.feedback(link)
        addressed = {(f["key"], f["fingerprint"]) for f in run["feedback"]}
        threads = self.threads(link)
        for item in run["feedback"]:
            if not item["thread_id"]:
                continue
            if any(f["thread_id"] == item["thread_id"] and (f["key"], f["fingerprint"]) not in addressed for f in outstanding):
                continue
            thread = threads.get(item["root_id"])
            if thread and not thread["resolved"]:
                # Pin the publication head immediately before each external resolution.
                if self.metadata(link)["head"]["sha"] != run["head"]:
                    run.update(status="blocked_pending", feedback_delivery_pending=False,
                               message="PR changed during feedback delivery; resolution stopped.\n" + self.r.location(run))
                    self.r.store.save(run)
                    return
                result = self.api("graphql", "POST", {"query": "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{id isResolved}}}", "id": item["thread_id"]}, run=run)
                if result.get("errors"):
                    raise self.fail("GitHub did not confirm review thread resolution")
                thread["resolved"] = True
        link["processed"].update({item["key"]: item["fingerprint"] for item in run["feedback"]})
        self.r.store.save_link(link)
        run["status"] = "review_pending"
        run["feedback_delivery_pending"] = False
        run["feedback_verified"] = True
        self.r.store.save(run)
