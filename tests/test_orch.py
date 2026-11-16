"""Contract tests for scripts/orch.py: what the parent session sees from each verb.

Each test builds a throwaway git repo, drives orch.py as a subprocess the way the
commands do, and asserts on its printed verdicts, exit codes and git state.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ORCH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "orch.py")

REPORT = """CHUNK: {id}
RESULT: {result}
BRANCH: {branch}
SHAS: abc123
GATE: 3 passed, 0 failed
REVIEW: 0 findings
DECISION: {decision}
ASSUMED: none
REMAINING: {remaining}
NEXT: none
"""


def sh(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, text=True, check=True).stdout


class Repo(unittest.TestCase):
    test_cmd = "test ! -e FAIL"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="orch-test-")
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        sh("git init -q -b main && git config user.email t@t && git config user.name t "
           "&& echo a > a.txt && git add a.txt && git commit -qm init", self.root)
        r = self.orch("setup", "--test-cmd", self.test_cmd, "--load", "1000",
                      "--worktrees", os.path.join(self.tmp, "wt"))
        self.assertEqual(r.returncode, 0, r.stdout)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def orch(self, *args, stdin=None, cwd=None):
        return subprocess.run([sys.executable, ORCH] + list(args), cwd=cwd or self.root,
                              capture_output=True, text=True, input=stdin)

    def ok(self, *args, **kw):
        r = self.orch(*args, **kw)
        self.assertEqual(r.returncode, 0, "%s -> %s%s" % (args, r.stdout, r.stderr))
        return r.stdout

    def run_json(self):
        with open(os.path.join(self.root, ".orch", "run.json")) as f:
            return json.load(f)

    def item(self, iid):
        return next(x for x in self.run_json()["items"] if x["id"] == iid)

    def verdict(self):
        out = self.orch("next").stdout
        return [l for l in out.splitlines() if l.startswith("NEXT:")][0][6:]

    def values(self, out):
        line = [l for l in out.splitlines() if l.startswith("VALUES:")][0][8:]
        return dict(p.split(": ", 1) for p in line.split(" | "))

    def build(self, cid, filename="b.txt", content="b"):
        """Act as the builder: commit one file on the chunk's branch."""
        v = self.values(self.ok("prepare", cid))
        with open(os.path.join(v["WORKTREE"], filename), "w") as f:
            f.write(content + "\n")
        sh("git add %s && git commit -qm '%s'" % (filename, cid), v["WORKTREE"])
        return v

    def report(self, cid, result="DONE", decision="none", remaining="none", text=None):
        x = self.item(cid)
        body = text if text is not None else REPORT.format(
            id=cid, result=result, branch=x["branch"], decision=decision, remaining=remaining)
        return self.orch("report", cid, "-", stdin=body)

    def base_sha(self):
        return sh("git rev-parse main", self.root).strip()


class QueueTest(Repo):
    def test_a_chunk_goes_from_queue_to_landed_on_base(self):
        self.ok("init")
        self.ok("add", "--kind", "chunk", "--text", "add b")
        self.assertEqual(self.verdict(), "LAUNCH 1")
        v = self.build("C-1")
        self.assertTrue(os.path.isdir(v["WORKTREE"]))
        self.assertEqual(self.verdict(), "IDLE — 1 running")
        r = self.report("C-1")
        self.assertIn("now: orch.py land C-1", r.stdout)
        out = self.ok("land", "C-1")
        self.assertIn("LANDED C-1", out)
        self.assertEqual(sh("git show main:b.txt", self.root), "b\n")
        self.assertFalse(os.path.exists(v["WORKTREE"]))
        self.assertEqual(sh("git branch --list 'orch/*'", self.root).strip(), "")
        self.assertEqual(self.item("C-1")["status"], "done")
        self.assertEqual(self.verdict(), "DRY")

    def test_a_worktree_command_resolves_the_main_repo_state(self):
        self.ok("init")
        self.ok("add", "--kind", "chunk", "--text", "x")
        v = self.build("C-1")
        out = self.ok("status", cwd=v["WORKTREE"])
        self.assertIn("RUNNING C-1", out)

    def test_init_refuses_while_a_run_is_open_and_archives_a_wound_down_one(self):
        self.ok("init")
        r = self.orch("init")
        self.assertEqual(r.returncode, 2)
        self.assertIn("a run is open", r.stdout)
        self.ok("winddown", "--reason", "DRY")
        self.ok("init")
        self.assertEqual(len(os.listdir(os.path.join(self.root, ".orch", "runs"))), 1)

    def test_launch_rules(self):
        """Each row: queue shape -> the line `next` prints for C-2 and the verdict."""
        cases = [
            ("after a queued chunk waits", ["--after", "C-1"], None, "WAIT      C-2 after C-1"),
            ("after a failed chunk strands", ["--after", "C-1"], "failed", "STRANDED  C-2 after C-1"),
            ("a shared file with a running chunk holds", ["--files", "a.txt"], "running", "HELD      C-2 shares a.txt"),
            ("a disjoint file with a running chunk launches", ["--files", "z.txt"], "running", "LAUNCH    C-2"),
        ]
        for name, flags, c1_state, expect in cases:
            with self.subTest(name):
                self.tearDown(); self.setUp()
                self.ok("init")
                self.ok("add", "--kind", "chunk", "--text", "one", "--files", "a.txt")
                self.ok("add", "--kind", "chunk", "--text", "two", *flags)
                if c1_state == "running":
                    self.ok("prepare", "C-1")
                elif c1_state == "failed":
                    self.ok("mark", "C-1", "failed", "gave up")
                out = self.orch("next").stdout
                self.assertIn(expect, out)

    def test_stranded_is_the_verdict_when_only_stranded_chunks_remain(self):
        self.ok("init")
        self.ok("add", "--kind", "chunk", "--text", "one")
        self.ok("add", "--kind", "chunk", "--text", "two", "--after", "C-1")
        self.ok("mark", "C-1", "failed", "x")
        self.assertTrue(self.verdict().startswith("STRANDED"))

    def test_retrying_a_failed_chunk_unstrands_its_chain(self):
        self.ok("init")
        self.ok("add", "--kind", "chunk", "--text", "one")
        self.ok("add", "--kind", "chunk", "--text", "two", "--after", "C-1")
        self.ok("mark", "C-1", "failed", "x")
        self.ok("mark", "C-1", "retry")
        self.assertEqual(self.verdict(), "LAUNCH 1")
        self.assertEqual(self.orch("mark", "C-1", "retry").returncode, 2)

    def test_agent_cap_holds_the_rest(self):
        self.ok("setup", "--agents", "1")
        self.ok("init")
        self.ok("add", "--kind", "chunk", "--text", "one")
        self.ok("add", "--kind", "chunk", "--text", "two")
        out = self.orch("next").stdout
        self.assertIn("LAUNCH    C-1", out)
        self.assertIn("OVER-CAP  C-2 (agent cap 1)", out)

    def test_a_builder_chunk_cannot_be_marked_done_by_hand(self):
        self.ok("init")
        self.ok("add", "--kind", "chunk", "--text", "one")
        r = self.orch("mark", "C-1", "done")
        self.assertEqual(r.returncode, 2)
        self.assertIn("marked done by `land`", r.stdout)

    def test_add_dedupes_by_title(self):
        self.ok("init")
        self.ok("add", "--kind", "chunk", "--text", "one")
        self.assertIn("duplicate of C-1", self.orch("add", "--kind", "idea", "--text", "one").stdout)


class ReportTest(Repo):
    def test_a_missing_block_gets_one_resume_then_a_strike(self):
        self.ok("init")
        self.ok("add", "--kind", "chunk", "--text", "one")
        self.ok("prepare", "C-1")
        r = self.report("C-1", text="I did the thing, all good!")
        self.assertEqual(r.returncode, 3)
        self.assertIn("REPORT RESUME C-1", r.stdout)
        self.assertIn("SEND:", r.stdout)
        r = self.report("C-1", text="still prose")
        self.assertEqual(r.returncode, 4)
        self.assertIn("REPORT REQUEUED C-1", r.stdout)
        self.assertEqual(self.item("C-1")["status"], "queued")

    def test_the_third_attempt_without_progress_fails_the_chunk(self):
        self.ok("init")
        self.ok("add", "--kind", "chunk", "--text", "one")
        for attempt in (1, 2):
            self.ok("prepare", "C-1")
            self.assertIn("REQUEUED", self.report("C-1", "PARTIAL", remaining="half").stdout)
        self.ok("prepare", "C-1")
        r = self.report("C-1", "PARTIAL", remaining="half")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no progress after 3 attempts", r.stdout)
        self.assertEqual(self.item("C-1")["status"], "failed")

    def test_a_relaunch_reuses_the_worktree_and_says_resume(self):
        self.ok("init")
        self.ok("add", "--kind", "chunk", "--text", "one")
        v = self.build("C-1")
        self.report("C-1", "PARTIAL", remaining="tests left")
        out = self.ok("prepare", "C-1")
        self.assertEqual(self.values(out)["WORKTREE"], v["WORKTREE"])
        self.assertIn("RESUME:", out)
        self.assertIn("LAST ATTEMPT: partial: tests left", out)

    def test_blocked_with_a_decision_gates_until_answered(self):
        self.ok("init")
        self.ok("add", "--kind", "chunk", "--text", "one")
        self.ok("prepare", "C-1")
        dec = "Which DB? | (a) sqlite; (b) postgres | recommended: (a) | class: reversible"
        r = self.report("C-1", "BLOCKED", decision=dec, remaining="needs a DB")
        self.assertIn("opened D-1", r.stdout)
        self.assertTrue(self.verdict().startswith("NEEDS-YOU"))
        self.assertIn("unblocks: C-1", self.ok("decisions"))
        self.ok("decide", "D-1", "(b) postgres")
        self.assertEqual(self.verdict(), "LAUNCH 1")
        self.assertIn("DECIDED: D-1 Which DB? — (b) postgres", self.ok("prepare", "C-1"))

    def test_waiting_on_the_user_costs_no_attempt(self):
        self.ok("init")
        self.ok("add", "--kind", "chunk", "--text", "one")
        self.ok("prepare", "C-1")
        self.report("C-1", "BLOCKED", decision="q | (a) x | recommended: (a)", remaining="r")
        self.ok("decide", "D-1", "(a)")
        for _ in (1, 2):
            self.ok("prepare", "C-1")
            self.assertIn("REQUEUED", self.report("C-1", "PARTIAL", remaining="half").stdout)

    def test_a_chunk_that_needed_no_change_closes_without_landing(self):
        self.ok("init")
        self.ok("add", "--kind", "chunk", "--text", "one")
        v = self.values(self.ok("prepare", "C-1"))
        body = REPORT.format(id="C-1", result="DONE", branch=v["BRANCH"], decision="none",
                             remaining="already done").replace("SHAS: abc123", "SHAS: none")
        self.assertIn("nothing to land", self.report("C-1", text=body).stdout)
        self.assertEqual(self.item("C-1")["status"], "done")
        self.assertFalse(os.path.exists(v["WORKTREE"]))

    def test_shas_none_with_commits_on_the_branch_still_lands(self):
        self.ok("init")
        self.ok("add", "--kind", "chunk", "--text", "one")
        v = self.build("C-1")
        body = REPORT.format(id="C-1", result="DONE", branch=v["BRANCH"], decision="none",
                             remaining="none").replace("SHAS: abc123", "SHAS: none")
        self.assertIn("now: orch.py land C-1", self.report("C-1", text=body).stdout)
        self.assertTrue(os.path.isdir(v["WORKTREE"]))

    def test_blocked_without_a_decision_fails(self):
        self.ok("init")
        self.ok("add", "--kind", "chunk", "--text", "one")
        self.ok("prepare", "C-1")
        r = self.report("C-1", "BLOCKED", remaining="stuck")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.item("C-1")["status"], "failed")

    def test_a_read_only_chunk_is_done_on_its_report(self):
        self.ok("init")
        self.ok("add", "--kind", "chunk", "--text", "repro", "--read-only")
        v = self.values(self.ok("prepare", "C-1"))
        self.assertEqual(v["WORKTREE"], "none")
        self.assertIn("read-only, marked done", self.report("C-1").stdout)
        self.assertEqual(self.item("C-1")["status"], "done")

    def test_report_block_shapes(self):
        good = REPORT.format(id="C-1", result="DONE", branch="b", decision="none", remaining="none")
        cases = [
            ("prose before the block is fine", "Summary: did it.\n" + good, 0),
            ("a missing label", good.replace("REVIEW: 0 findings\n", ""), 3),
            ("labels out of order", good.replace("GATE: 3 passed, 0 failed\nREVIEW: 0 findings\n",
                                                 "REVIEW: 0 findings\nGATE: 3 passed, 0 failed\n"), 3),
            ("an unknown RESULT", good.replace("RESULT: DONE", "RESULT: MOSTLY"), 3),
            ("the wrong chunk id", good.replace("CHUNK: C-1", "CHUNK: C-9"), 3),
        ]
        for name, text, code in cases:
            with self.subTest(name):
                self.tearDown(); self.setUp()
                self.ok("init")
                self.ok("add", "--kind", "chunk", "--text", "one")
                self.ok("prepare", "C-1")
                self.assertEqual(self.report("C-1", text=text).returncode, code)


class LandTest(Repo):
    def setUp(self):
        super().setUp()
        self.ok("init")
        self.ok("add", "--kind", "chunk", "--text", "one")

    def test_a_red_gate_refuses_and_leaves_base_alone_then_fails_on_the_second(self):
        before = self.base_sha()
        self.build("C-1", filename="FAIL")
        self.report("C-1")
        r = self.orch("land", "C-1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("test command exit", r.stdout)
        self.assertIn("SEND:", r.stdout)
        self.assertEqual(self.base_sha(), before)
        self.assertEqual(self.item("C-1")["status"], "running")
        r = self.orch("land", "C-1")
        self.assertIn("Second refusal: FAILED", r.stdout)
        self.assertEqual(self.item("C-1")["status"], "failed")
        self.assertEqual(self.base_sha(), before)

    def test_land_refusals(self):
        cases = [
            ("uncommitted changes", lambda wt: sh("echo x > dirty.txt", wt),
             "uncommitted changes"),
            ("no commits", None, "no commits beyond main"),
            ("a rebase conflict", "conflict", "rebase onto main conflicts"),
        ]
        for name, act, expect in cases:
            with self.subTest(name):
                self.tearDown(); self.setUp()
                if act is None:
                    self.ok("prepare", "C-1")
                elif act == "conflict":
                    self.build("C-1", filename="a.txt", content="branch")
                    sh("echo base > a.txt && git commit -qam base", self.root)
                else:
                    v = self.build("C-1")
                    act(v["WORKTREE"])
                before = self.base_sha()
                r = self.orch("land", "C-1")
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn(expect, r.stdout)
                self.assertEqual(self.base_sha(), before)

    def test_land_rebases_onto_a_moved_base(self):
        self.build("C-1")
        sh("echo c > c.txt && git add c.txt && git commit -qm other", self.root)
        self.ok("land", "C-1")
        self.assertEqual(sh("git log --format=%s main", self.root).split(), ["C-1", "other", "init"])

    def test_land_moves_base_by_ref_when_the_main_checkout_is_elsewhere(self):
        sh("git checkout -qb side", self.root)
        self.build("C-1")
        self.ok("land", "C-1")
        self.assertEqual(sh("git show main:b.txt", self.root), "b\n")
        self.assertEqual(sh("git rev-parse --abbrev-ref HEAD", self.root).strip(), "side")

    def test_land_refuses_a_dirty_main_checkout_on_base(self):
        self.build("C-1")
        sh("echo dirty >> a.txt", self.root)
        r = self.orch("land", "C-1")
        self.assertIn("uncommitted changes on main", r.stdout)

    def test_push_happens_only_on_an_each_land_policy(self):
        remote = os.path.join(self.tmp, "remote.git")
        sh("git init -q --bare %s && git remote add origin %s && git push -q origin main" % (remote, remote), self.root)
        cases = [("never push", False), ("after each land", True)]
        for words, pushed in cases:
            with self.subTest(words):
                run = self.run_json()
                run["standing"]["push"] = words
                with open(os.path.join(self.root, ".orch", "run.json"), "w") as f:
                    json.dump(run, f)
                cid = "C-%d" % (len(run["items"]))
                self.build(cid, filename="%s.txt" % cid)
                self.ok("land", cid)
                remote_sha = sh("git --git-dir=%s rev-parse main" % remote, self.root).strip()
                self.assertEqual(remote_sha == self.base_sha(), pushed)
                self.ok("add", "--kind", "chunk", "--text", "next %s" % words)


class LandCleanupTest(Repo):
    test_cmd = "touch artifact.out && test ! -e FAIL"

    def test_land_removes_the_worktree_even_when_the_gate_left_untracked_files(self):
        self.ok("init")
        self.ok("add", "--kind", "chunk", "--text", "one")
        v = self.build("C-1")
        self.ok("land", "C-1")
        self.assertFalse(os.path.exists(v["WORKTREE"]))
        self.assertEqual(sh("git branch --list 'orch/*'", self.root).strip(), "")


class PlanTest(Repo):
    def test_a_plan_registers_decisions_chains_chunks_and_closes_the_intake(self):
        self.ok("init")
        self.ok("add", "--kind", "feature", "--text", "dark mode")
        self.assertIn("PLAN      I-1", self.orch("next").stdout)
        v = self.values(self.ok("prepare", "I-1"))
        plan = {"intake": "I-1",
                "decisions": [{"key": "theme", "question": "Default theme?", "options": ["(a) light", "(b) system"],
                               "recommended": "(b)", "class": "reversible"}],
                "chunks": [{"slug": "tokens", "title": "colour tokens"},
                           {"slug": "toggle", "title": "toggle", "needs": ["theme"]},
                           {"slug": "docs", "title": "docs", "after": ["tokens"]}]}
        with open(v["PLAN_FILE"], "w") as f:
            json.dump(plan, f)
        out = self.ok("add-plan", v["PLAN_FILE"])
        self.assertIn("DECISION D-1 opened", out)
        self.assertEqual(self.item("C-2")["after"], ["C-1"])
        self.assertEqual(self.item("C-2")["needs"], ["D-1"])
        self.assertEqual(self.item("C-3")["after"], ["C-1"])
        self.assertEqual(self.item("I-1")["status"], "planned")
        self.assertIn("SKIPPED duplicate: colour tokens", self.ok("add-plan", v["PLAN_FILE"]))


class AutoTest(Repo):
    def standing(self, **slots):
        for k in ("stop", "off-limits", "delegation", "push"):
            self.ok("standing", k, slots.get(k, "none"))

    def test_an_auto_run_will_not_launch_with_a_slot_unset(self):
        self.ok("init", "--auto")
        self.ok("add", "--kind", "chunk", "--text", "one")
        r = self.orch("next")
        self.assertEqual(r.returncode, 2)
        self.assertIn("stop, off-limits, delegation, push", r.stdout)
        self.standing(delegation="reversible ones")
        self.assertEqual(self.verdict(), "LAUNCH 1")

    def test_delegate_answers_only_reversible_decisions_with_a_recommendation(self):
        self.ok("init", "--auto")
        self.standing(delegation="anything reversible")
        plan = {"round": 1, "decisions": [
            {"key": "a", "question": "q-rev", "options": ["(a) x", "(b) y"], "recommended": "(a)", "class": "reversible"},
            {"key": "b", "question": "q-oneway", "options": ["(a) x"], "recommended": "(a)", "class": "one-way"},
            {"key": "c", "question": "q-norec", "options": ["(a) x"], "class": "reversible"}], "chunks": []}
        p = os.path.join(self.tmp, "p.json")
        with open(p, "w") as f:
            json.dump(plan, f)
        self.ok("add-plan", p)
        out = self.ok("delegate")
        self.assertIn("ANSWERED D-1 by delegation", out)
        self.assertIn("REFUSED D-2 — class is 'one-way'", out)
        self.assertIn("REFUSED D-3 — no recommended option", out)

    def test_delegate_refuses_everything_when_the_user_said_none(self):
        self.ok("init", "--auto")
        self.standing(delegation="none")
        self.ok("add", "--kind", "chunk", "--text", "one")
        self.ok("prepare", "C-1")
        self.report("C-1", "BLOCKED", decision="q | (a) x | recommended: (a) | class: reversible", remaining="r")
        self.assertIn("REFUSED D-1 — the delegation slot is 'none'", self.ok("delegate"))

    def test_dry_is_not_done_until_the_rows_are_met_or_rounds_run_out(self):
        self.ok("init", "--auto", "--rounds", "1")
        self.standing()
        self.assertEqual(self.verdict(), "FINISH-REVIEW round 1 of 1")
        self.assertIn("not FINISHED", self.orch("winddown", "--reason", "FINISHED").stdout)
        self.ok("done-when", "start")
        self.assertTrue(self.verdict().startswith("IDLE"))
        self.ok("done-when", "1", "met", "nothing planned")
        self.ok("done-when", "close")
        self.assertTrue(self.verdict().startswith("DRY — round cap reached"))
        self.ok("winddown", "--reason", "ROUNDS")

    def test_finished_needs_every_row_met_or_user_only(self):
        self.ok("init", "--auto")
        self.standing()
        self.ok("done-when", "start")
        n = len(self.run_json()["done_when"])
        for i in range(1, n + 1):
            self.ok("done-when", str(i), "user-only" if i == n else "met", "evidence %d" % i)
        self.ok("done-when", "close")
        self.assertEqual(self.verdict(), "DRY")
        out = self.ok("winddown", "--reason", "FINISHED")
        self.assertIn("## Only you can do", out)
        self.assertIn("done-when %d:" % n, out)

    def test_winddown_reasons_must_be_true(self):
        cases = [
            ("USER-ONLY with nothing gated", "USER-ONLY", "not USER-ONLY"),
            ("DRY with work queued", "DRY", "not DRY"),
            ("ROUNDS before the cap", "ROUNDS", "not ROUNDS"),
        ]
        for name, reason, expect in cases:
            with self.subTest(name):
                self.tearDown(); self.setUp()
                self.ok("init", "--auto")
                self.ok("add", "--kind", "chunk", "--text", "one")
                self.assertIn(expect, self.orch("winddown", "--reason", reason).stdout)

    def test_the_report_lists_delegated_and_owed_work(self):
        self.ok("init", "--auto")
        self.standing(delegation="reversible")
        self.ok("add", "--kind", "chunk", "--text", "one")
        self.ok("add", "--kind", "chunk", "--text", "two")
        self.ok("prepare", "C-1")
        self.report("C-1", "BLOCKED", decision="pick | (a) x; (b) y | recommended: (a) | class: reversible", remaining="r")
        self.ok("prepare", "C-2")
        self.report("C-2", "BLOCKED", decision="ship? | (a) yes; (b) no | recommended: (a) | class: one-way", remaining="r")
        self.ok("delegate")
        self.ok("mark", "C-1", "dropped", "x")
        out = self.ok("winddown", "--reason", "USER-ONLY")
        decided = out.split("## Decided for you")[1].split("## Only you")[0]
        owed = out.split("## Only you can do")[1]
        self.assertIn("D-1 pick → (a) — delegated", decided)
        self.assertIn("decide D-2: ship?", owed)


class ReconcileTest(Repo):
    def test_reconcile_resumes_work_in_progress_and_requeues_empty_launches(self):
        self.ok("init")
        self.ok("add", "--kind", "chunk", "--text", "one")
        self.ok("add", "--kind", "chunk", "--text", "two")
        self.build("C-1")
        self.ok("prepare", "C-2")
        out = self.ok("reconcile", "--apply")
        self.assertIn("resume  C-1", out)
        self.assertIn("requeue C-2", out)
        self.assertEqual([self.item(c)["status"] for c in ("C-1", "C-2")], ["queued", "queued"])
        self.assertIn("RESUME:", self.ok("prepare", "C-1"))


if __name__ == "__main__":
    unittest.main()
