#!/usr/bin/env python3
"""orch.py — the queue behind /orch and /orch-auto.

One run per repo, kept in <repo>/.orch/ (git-ignored). The parent session
never edits that state by hand: every read and write goes through a verb here,
so the launch list, the decisions owed and the wind-down report are derived,
never typed. Python 3.8+, standard library only.
"""
import argparse
import datetime as dt
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys

MAX_ATTEMPTS = 3          # a chunk launched this many times without landing is FAILED
MAX_LAND_REFUSALS = 2     # a branch refused this many times at land is FAILED
REPORT_LABELS = ["CHUNK", "RESULT", "BRANCH", "SHAS", "GATE", "REVIEW",
                 "DECISION", "ASSUMED", "REMAINING", "NEXT"]
RESULTS = ("DONE", "PARTIAL", "BLOCKED", "FAILED")
AUTO_SLOTS = ("stop", "off-limits", "delegation", "push")
PUSH_EACH = re.compile(r"^\s*(push\s+)?(after\s+)?(each|every)\b", re.I)  # anything else: never push
DEFAULT_CHECKLIST = [
    "every chunk the plans named is done, or waits only on the user",
    "the test command passes on the final base-branch sha, with real counts",
    "the README says what it is, how to install it and how to use it",
    "no worktree or branch of this run is left behind, except ones named for the user",
]


class Refused(Exception):
    """A verb that cannot do what was asked says so; it never guesses."""


def now():
    return dt.datetime.now().replace(microsecond=0).isoformat()


def git(args, cwd, check=True):
    p = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise Refused("git %s: %s" % (" ".join(args), (p.stderr or p.stdout).strip()))
    return p


def repo_root(start):
    p = git(["rev-parse", "--path-format=absolute", "--git-common-dir"], start, check=False)
    if p.returncode != 0:
        raise Refused("not inside a git repo: %s" % start)
    common = p.stdout.strip()
    if os.path.basename(common) == ".git":
        return os.path.dirname(common)
    raise Refused("bare or unusual git layout (%s); run from a normal checkout" % common)


class Store:
    def __init__(self, root):
        self.root = root
        self.dir = os.path.join(root, ".orch")
        self.run_path = os.path.join(self.dir, "run.json")
        self.cfg_path = os.path.join(self.dir, "config.json")
        self.ledger_path = os.path.join(self.dir, "ledger.log")
        self._lock = None

    def __enter__(self):
        os.makedirs(self.dir, exist_ok=True)
        self._lock = open(os.path.join(self.dir, ".lock"), "w")
        fcntl.flock(self._lock, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._lock, fcntl.LOCK_UN)
        self._lock.close()

    def config(self):
        if not os.path.exists(self.cfg_path):
            raise Refused("no .orch/config.json in %s — run /orch-setup first" % self.root)
        with open(self.cfg_path) as f:
            return json.load(f)

    def load(self):
        if not os.path.exists(self.run_path):
            raise Refused("no run in %s — start one with: orch.py init" % self.root)
        with open(self.run_path) as f:
            return json.load(f)

    def save(self, run):
        tmp = self.run_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(run, f, indent=1)
        os.replace(tmp, self.run_path)

    def log(self, text):
        with open(self.ledger_path, "a") as f:
            f.write("%s  %s\n" % (now(), text))


# ---------------------------------------------------------------- helpers

def new_id(run, prefix):
    run["counters"][prefix] += 1
    return "%s-%d" % (prefix, run["counters"][prefix])


def find(run, item_id):
    for coll in ("items", "decisions"):
        for x in run[coll]:
            if x["id"] == item_id:
                return x
    raise Refused("no such id in this run: %s" % item_id)


def chunks(run):
    return [x for x in run["items"] if x["kind"] == "chunk"]


def slugify(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:40].rstrip("-") or "chunk"


def load_avg():
    try:
        return os.getloadavg()[0]
    except OSError:
        return None


def decision_open(run, did):
    d = next((x for x in run["decisions"] if x["id"] == did), None)
    return d is None or d["status"] != "answered"


def running_count(run):
    n = sum(1 for x in run["items"] if x["status"] in ("running", "planning"))
    return n + (1 if run.get("review_running") else 0)


def chunk_row(run, title, brief="", slug=None, files=None, model="", after=None,
              needs=None, read_only=False, test="", est=None, origin=""):
    cid = new_id(run, "C")
    row = {"id": cid, "kind": "chunk", "title": title, "brief": brief,
           "slug": "c%s-%s" % (cid[2:], slugify(slug or title)),
           "files": files or [], "model": model or "", "after": after or [],
           "needs": needs or [], "read_only": bool(read_only), "test": test,
           "est": est, "from": origin, "status": "queued", "attempt": 0,
           "misses": 0, "land_refusals": 0, "agent": "", "worktree": "",
           "branch": "", "note": "", "shas": [], "next": [], "assumed": "",
           "gate": "", "review": ""}
    run["items"].append(row)
    return row


def add_decision(run, question, options, recommended, klass, origin):
    did = new_id(run, "D")
    run["decisions"].append({"id": did, "question": question, "options": options,
                             "recommended": recommended, "class": klass or "",
                             "from": origin, "status": "open", "answer": "", "how": ""})
    return did


# ---------------------------------------------------------------- setup / init

def cmd_setup(st, a):
    cfg = {}
    if os.path.exists(st.cfg_path):
        cfg = st.config()
    base = a.base or cfg.get("base") or detect_base(st.root)
    test = a.test_cmd if a.test_cmd is not None else cfg.get("test_cmd", "")
    if not test:
        raise Refused("no test command: pass --test-cmd '<the command that proves the repo is green>'")
    cfg.update({
        "base": base,
        "test_cmd": test,
        "setup_cmd": a.setup_cmd if a.setup_cmd is not None else cfg.get("setup_cmd", ""),
        "worktree_dir": a.worktrees or cfg.get("worktree_dir") or
        os.path.join(os.path.dirname(st.root), os.path.basename(st.root) + "-orch-wt"),
        "agents": a.agents or cfg.get("agents", 3),
        "load": a.load or cfg.get("load", os.cpu_count() or 4),
        "gate_timeout_min": a.gate_timeout or cfg.get("gate_timeout_min", 30),
        "review": cfg.get("review", True) if a.review is None else a.review == "on",
    })
    git(["rev-parse", "--verify", "refs/heads/" + cfg["base"]], st.root)
    with open(st.cfg_path, "w") as f:
        json.dump(cfg, f, indent=1)
    exclude = os.path.join(st.root, ".git", "info", "exclude")
    os.makedirs(os.path.dirname(exclude), exist_ok=True)
    have = open(exclude).read() if os.path.exists(exclude) else ""
    if ".orch/" not in have.split():
        with open(exclude, "a") as f:
            f.write("\n.orch/\n")
    st.log("setup %s" % json.dumps(cfg))
    print("SETUP OK %s" % st.cfg_path)
    for k, v in cfg.items():
        print("  %-16s %s" % (k, v))


def detect_base(root):
    p = git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], root, check=False)
    if p.returncode == 0:
        return p.stdout.strip().split("/", 1)[-1]
    for b in ("main", "master"):
        if git(["rev-parse", "--verify", "refs/heads/" + b], root, check=False).returncode == 0:
            return b
    raise Refused("cannot tell the base branch: pass --base")


def cmd_init(st, a):
    st.config()
    if os.path.exists(st.run_path):
        old = st.load()
        if not old.get("wound_down"):
            raise Refused("a run is open (created %s): wind it down first, or carry on with it"
                          % old["created"])
        arch = os.path.join(st.dir, "runs")
        os.makedirs(arch, exist_ok=True)
        shutil.move(st.run_path, os.path.join(arch, "run-%s.json" % old["created"].replace(":", "")))
    checklist = DEFAULT_CHECKLIST
    if a.checklist:
        with open(a.checklist) as f:
            checklist = [l[2:].strip() for l in f if l.startswith("- ")]
    run = {"version": 1, "created": now(), "mode": "auto" if a.auto else "attended",
           "rounds": a.rounds, "round": 0, "review_running": None,
           "standing": {}, "items": [], "decisions": [],
           "done_when": ([{"n": i + 1, "text": t, "state": "open", "evidence": ""}
                          for i, t in enumerate(checklist)] if a.auto else []),
           "counters": {"C": 0, "I": 0, "D": 0}, "wound_down": None}
    st.save(run)
    st.log("init mode=%s" % run["mode"])
    print("RUN %s mode=%s%s" % (st.run_path, run["mode"],
                                 " rounds=%d" % a.rounds if a.auto else ""))


def cmd_standing(st, a):
    run = st.load()
    if a.slot:
        if a.slot not in AUTO_SLOTS:
            raise Refused("unknown slot %s (slots: %s)" % (a.slot, ", ".join(AUTO_SLOTS)))
        if a.words is None:
            raise Refused("give the user's words: standing %s \"<their words>\"" % a.slot)
        run["standing"][a.slot] = a.words
        st.save(run)
        st.log("standing %s: %s" % (a.slot, a.words))
    for s in AUTO_SLOTS:
        print("  %-11s %s" % (s, run["standing"].get(s, "<unset>")))


# ---------------------------------------------------------------- queue

def cmd_add(st, a):
    run = st.load()
    title = a.text.strip()
    for x in run["items"]:
        if x["title"] == title and x["status"] != "dropped":
            raise Refused("duplicate of %s (%s)" % (x["id"], x["status"]))
    if a.kind == "chunk":
        row = chunk_row(run, title, brief=a.brief or "", files=split_csv(a.files),
                        model=a.model, after=split_csv(a.after), needs=split_csv(a.needs),
                        read_only=a.read_only, origin="user")
    else:
        iid = new_id(run, "I")
        row = {"id": iid, "kind": a.kind, "title": title, "status": "unplanned",
               "agent": "", "note": ""}
        run["items"].append(row)
    st.save(run)
    st.log("add %s %s: %s" % (row["id"], a.kind, title))
    print("ADDED %s %s" % (row["id"], title))


def split_csv(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def cmd_add_plan(st, a):
    run = st.load()
    with open(a.file) as f:
        plan = json.load(f)
    origin = plan.get("intake") or ("review-%s" % plan["round"] if "round" in plan else "")
    if not origin:
        raise Refused("plan names neither intake nor round")
    dmap = {}
    for d in plan.get("decisions", []):
        if not d.get("question") or not d.get("options"):
            raise Refused("decision without question or options: %r" % d)
        dmap[d.get("key", d["question"])] = add_decision(
            run, d["question"], d["options"], d.get("recommended", ""), d.get("class", ""), origin)
    smap, added, skipped = {}, [], []
    titles = {x["title"] for x in chunks(run) if x["status"] != "dropped"}
    prev = None
    for c in plan.get("chunks", []):
        if not c.get("title"):
            raise Refused("chunk without a title: %r" % c)
        if c["title"] in titles:
            skipped.append(c["title"])
            continue
        after = [smap.get(s, s) for s in c.get("after", [])]
        if plan.get("serial", True) and prev and not c.get("after"):
            after = [prev]
        needs = [dmap.get(k, k) for k in c.get("needs", [])]
        row = chunk_row(run, c["title"], brief=c.get("brief", ""), slug=c.get("slug"),
                        files=c.get("files", []), model=c.get("model", ""), after=after,
                        needs=needs, read_only=c.get("read_only", False),
                        test=c.get("test", ""), est=c.get("est_min"), origin=origin)
        smap[c.get("slug", c["title"])] = row["id"]
        prev = row["id"]
        added.append(row)
        titles.add(c["title"])
    if plan.get("intake"):
        it = find(run, plan["intake"])
        it["status"], it["note"] = "planned", "%d chunks" % len(added)
    elif "round" in plan:
        run["review_running"] = None
    st.save(run)
    st.log("add-plan %s: %s; decisions %s" % (origin, ", ".join(r["id"] for r in added) or "none",
                                              ", ".join(dmap.values()) or "none"))
    for r in added:
        print("ADDED %s %s%s%s" % (r["id"], r["title"],
                                   "  after " + ",".join(r["after"]) if r["after"] else "",
                                   "  needs " + ",".join(r["needs"]) if r["needs"] else ""))
    for t in skipped:
        print("SKIPPED duplicate: %s" % t)
    for q, did in dmap.items():
        print("DECISION %s opened" % did)


def classify(run, cfg):
    """Return (lines, verdict). Read-only."""
    lines, launch = [], []
    if run.get("wound_down"):
        return ["REFUSED: this run is wound down; init a new one"], "REFUSED"
    if run["mode"] == "auto":
        unset = [s for s in AUTO_SLOTS if not run["standing"].get(s)]
        if unset:
            return ["REFUSED: standing slot(s) unset: %s" % ", ".join(unset)], "REFUSED"
    running = running_count(run)
    load = load_avg()
    over_load = load is not None and load > cfg["load"]
    slots = max(0, cfg["agents"] - running)
    busy_files = set()
    for x in chunks(run):
        if x["status"] == "running":
            busy_files.update(x["files"])
    gated = stranded = waiting = 0
    status = {x["id"]: x["status"] for x in run["items"]}
    for x in run["items"]:
        if x["kind"] != "chunk" and x["status"] == "unplanned":
            if over_load or slots == 0:
                lines.append("OVER-CAP  %s plan: %s" % (x["id"], x["title"]))
                continue
            slots -= 1
            launch.append(x["id"])
            lines.append("PLAN      %s %s: %s" % (x["id"], x["kind"], x["title"]))
    for x in chunks(run):
        if x["status"] != "queued":
            continue
        dead = [d for d in x["after"] if status.get(d) in ("failed", "dropped")]
        pending = [d for d in x["after"] if status.get(d) != "done"]
        needs = [d for d in x["needs"] if decision_open(run, d)]
        shared = sorted(set(x["files"]) & busy_files)
        if dead:
            stranded += 1
            lines.append("STRANDED  %s after %s (%s)" % (x["id"], ",".join(dead), status[dead[0]]))
        elif needs:
            gated += 1
            lines.append("GATED     %s needs %s" % (x["id"], ",".join(needs)))
        elif pending:
            waiting += 1
            lines.append("WAIT      %s after %s" % (x["id"], ",".join(pending)))
        elif shared:
            waiting += 1
            lines.append("HELD      %s shares %s with a running chunk" % (x["id"], ",".join(shared)))
        elif over_load or slots == 0:
            waiting += 1
            why = "load %.1f > cap %s" % (load, cfg["load"]) if over_load else "agent cap %d" % cfg["agents"]
            lines.append("OVER-CAP  %s (%s)" % (x["id"], why))
        else:
            slots -= 1
            busy_files.update(x["files"])
            launch.append(x["id"])
            lines.append("LAUNCH    %s %s%s  [%s]" % (x["id"], x["title"],
                                                  "  model=" + x["model"] if x["model"] else "",
                                                  "read-only" if x["read_only"] else "builder"))
    if launch:
        verdict = "LAUNCH %d" % len(launch)
    elif running:
        verdict = "IDLE — %d running" % running
    elif waiting:
        verdict = "WAITING — over a cap or holding a shared file"
    elif gated:
        verdict = "NEEDS-YOU — %d chunk(s) wait on a decision" % gated
    elif stranded:
        verdict = "STRANDED — chunks wait on a failed chunk"
    elif run["mode"] == "auto" and any(r["state"] in ("open", "missing") for r in run["done_when"]):
        if run["round"] < run["rounds"]:
            verdict = "FINISH-REVIEW round %d of %d" % (run["round"] + 1, run["rounds"])
        else:
            verdict = "DRY — round cap reached with rows still open (wind down: ROUNDS)"
    else:
        verdict = "DRY"
    return lines, verdict


def cmd_next(st, a):
    run, cfg = st.load(), st.config()
    lines, verdict = classify(run, cfg)
    for l in lines:
        print(l)
    load = load_avg()
    print("load %s / cap %s · agents %d running / cap %d" % (
        "%.1f" % load if load is not None else "unknown", cfg["load"], running_count(run), cfg["agents"]))
    print("NEXT: %s" % verdict)
    if verdict == "REFUSED":
        sys.exit(2)


# ---------------------------------------------------------------- launch

def cmd_prepare(st, a):
    run, cfg = st.load(), st.config()
    x = find(run, a.id)
    plan_dir = os.path.join(st.dir, "plans")
    os.makedirs(plan_dir, exist_ok=True)
    orch = os.path.abspath(__file__)
    if x["kind"] != "chunk":
        if x["status"] not in ("unplanned", "planning"):
            raise Refused("%s is %s, not unplanned" % (x["id"], x["status"]))
        x["status"] = "planning"
        st.save(run)
        st.log("prepare %s planner" % x["id"])
        print("VALUES: INTAKE: %s | KIND: %s | REPO: %s | BASE: %s | TEST_CMD: %s | PLAN_FILE: %s | ORCH: %s"
              % (x["id"], x["kind"], st.root, cfg["base"], cfg["test_cmd"],
                 os.path.join(plan_dir, "%s.json" % x["id"]), orch))
        print("TEXT: %s" % x["title"])
        return
    if x["status"] != "queued":
        raise Refused("%s is %s, not queued" % (x["id"], x["status"]))
    _, verdict = classify(run, cfg)
    resumed = False
    if x["read_only"]:
        wt, br = "none", "none"
    else:
        wt = os.path.join(cfg["worktree_dir"], x["slug"])
        br = "orch/" + x["slug"]
        has_branch = git(["rev-parse", "--verify", "refs/heads/" + br], st.root, check=False).returncode == 0
        if os.path.isdir(wt):
            resumed = True
        elif has_branch:
            git(["worktree", "add", wt, br], st.root)
            resumed = True
        else:
            os.makedirs(cfg["worktree_dir"], exist_ok=True)
            git(["worktree", "add", "-b", br, wt, cfg["base"]], st.root)
    x.update({"status": "running", "attempt": x["attempt"] + 1, "misses": 0,
              "worktree": wt, "branch": br, "started": now()})
    st.save(run)
    st.log("prepare %s attempt %d worktree %s%s" % (x["id"], x["attempt"], wt, " (resumed)" if resumed else ""))
    scratch = os.path.join(st.dir, "scratch", x["slug"])
    os.makedirs(scratch, exist_ok=True)
    print("VALUES: CHUNK: %s | ATTEMPT: %d | REPO: %s | BASE: %s | WORKTREE: %s | BRANCH: %s | "
          "TEST_CMD: %s | SETUP_CMD: %s | REVIEW: %s | READ_ONLY: %s | SCRATCH: %s | ORCH: %s"
          % (x["id"], x["attempt"], st.root, cfg["base"], wt, br, cfg["test_cmd"],
             cfg.get("setup_cmd") or "none", "on" if cfg.get("review", True) else "off",
             "yes" if x["read_only"] else "no", scratch, orch))
    print("ITEM: %s — %s%s" % (x["title"], x["brief"], ("  Proving test: " + x["test"]) if x["test"] else ""))
    if x["files"]:
        print("FILES: %s" % ", ".join(x["files"]))
    if resumed:
        print("RESUME: the worktree already holds an earlier attempt's work — continue from it, do not restart.")
    if x["note"]:
        print("LAST ATTEMPT: %s" % x["note"])
    if x["needs"]:
        for d in x["needs"]:
            dd = find(run, d)
            print("DECIDED: %s %s — %s" % (d, dd["question"], dd["answer"]))


def cmd_mark(st, a):
    run = st.load()
    x = find(run, a.id)
    if a.state == "agent":
        x["agent"] = a.note or ""
    elif a.state in ("dropped", "failed", "done"):
        if a.state == "done" and x["kind"] == "chunk" and not x["read_only"] and x["status"] != "done":
            raise Refused("a builder chunk is marked done by `land`, never by hand")
        x["status"] = a.state
        x["note"] = a.note or x.get("note", "")
    elif a.state == "retry":
        if x["kind"] != "chunk" or x["status"] != "failed":
            raise Refused("%s is %s; only a failed chunk is retried" % (x["id"], x["status"]))
        x.update({"status": "queued", "attempt": 0, "misses": 0, "land_refusals": 0,
                  "note": a.note or "retried by the user"})
    elif a.state == "requeue":
        if x["status"] not in ("running", "planning"):
            raise Refused("%s is %s; only a running item is requeued" % (x["id"], x["status"]))
        x["status"] = "queued" if x["kind"] == "chunk" else "unplanned"
        if x["kind"] == "chunk":
            x["attempt"] = max(0, x["attempt"] - 1)  # the machine's fault costs no attempt
        x["note"] = a.note or "requeued"
    else:
        raise Refused("mark states: agent | requeue | retry | dropped | failed | done (read-only chunks)")
    st.save(run)
    st.log("mark %s %s %s" % (x["id"], a.state, a.note or ""))
    print("MARKED %s %s" % (x["id"], x["status"] if a.state != "agent" else "agent " + x["agent"]))


# ---------------------------------------------------------------- report

def shas_ok(s):
    s = re.sub(r"\s*\([^()]*\)$", "", s)  # one trailing (note)
    if s == "none":
        return True
    toks = [t for t in re.split(r"[ ,]+", s) if t]
    return bool(toks) and all(re.fullmatch(r"[0-9a-f]{7,40}", t) for t in toks)


def gate_ok(g):
    if re.search(r"\d+\s+(passed|pass|ran|tests?)\b|\d+/\d+", g):
        return True
    return bool(re.search(r"(?<![A-Za-z0-9_])(OK|FAIL(ED)?|none|not run)(?![A-Za-z0-9_])", g))


def parse_report(text):
    lines = text.strip().splitlines()
    starts = [i for i, l in enumerate(lines) if l.startswith("CHUNK:")]
    if not starts:
        return None, "no CHUNK: line"
    block = lines[starts[-1]:]
    fields = {}
    order = []
    for l in block:
        m = re.match(r"^([A-Z]+):\s?(.*)$", l)
        if m and m.group(1) in REPORT_LABELS:
            if m.group(1) in fields:
                return None, "%s: appears twice" % m.group(1)
            fields[m.group(1)] = m.group(2).strip()
            order.append(m.group(1))
    missing = [k for k in REPORT_LABELS if k not in fields]
    if missing:
        return None, "missing %s" % ", ".join(missing)
    if order != REPORT_LABELS:
        return None, "labels out of order"
    if fields["RESULT"] not in RESULTS:
        return None, "RESULT is %r, not one of %s" % (fields["RESULT"], "|".join(RESULTS))
    if not shas_ok(fields["SHAS"]):
        return None, "SHAS %r is not none or hex shas (7-40)" % fields["SHAS"]
    if not gate_ok(fields["GATE"]):
        return None, "GATE %r names no count, OK, FAIL or none" % fields["GATE"]
    return fields, ""


def parse_decision(s):
    """`<question> | (a) x; (b) y | recommended: (a) | class: reversible`"""
    parts = [p.strip() for p in s.split("|")]
    if len(parts) < 2 or not parts[0]:
        return None
    opts = [o.strip() for o in parts[1].split(";") if o.strip()]
    rec = klass = ""
    for p in parts[2:]:
        if p.lower().startswith("recommended:"):
            rec = p.split(":", 1)[1].strip()
        elif p.lower().startswith("class:"):
            klass = p.split(":", 1)[1].strip().lower()
    return parts[0], opts, rec, klass


RESUME_PROMPT = ("Your last message was not the report block. Change nothing on disk and redo nothing: "
                 "reply with ONLY the 10 report lines (CHUNK: to NEXT:), filled in, nothing before or after.")


def cmd_report(st, a):
    run = st.load()
    x = find(run, a.id)
    if x["kind"] != "chunk":
        raise Refused("%s is not a chunk" % x["id"])
    if x["status"] != "running":
        raise Refused("%s is %s, not running" % (x["id"], x["status"]))
    text = sys.stdin.read() if a.file == "-" else open(a.file).read()
    fields, why = parse_report(text)
    if fields and fields["CHUNK"] != x["id"]:
        fields, why = None, "CHUNK is %s, expected %s" % (fields["CHUNK"], x["id"])
    if not fields:
        x["misses"] += 1
        if x["misses"] == 1:
            st.save(run)
            st.log("report-miss %s attempt %d: %s" % (x["id"], x["attempt"], why))
            print("REPORT RESUME %s — %s" % (x["id"], why))
            print("SEND: %s" % RESUME_PROMPT)
            sys.exit(3)
        return give_up_or_requeue(st, run, x, "no report: %s" % why)
    x.update({"gate": fields["GATE"], "review": fields["REVIEW"],
              "assumed": fields["ASSUMED"] if fields["ASSUMED"] != "none" else "",
              "next": [] if fields["NEXT"] == "none" else [n.strip() for n in fields["NEXT"].split(";") if n.strip()]})
    res = fields["RESULT"]
    if res == "DONE":
        if x["read_only"]:
            x["status"], x["note"] = "done", fields["REMAINING"] if fields["REMAINING"] != "none" else ""
            st.save(run)
            st.log("report %s DONE (read-only)" % x["id"])
            print("REPORT OK %s DONE — read-only, marked done" % x["id"])
        elif fields["SHAS"] == "none" and nothing_to_land(st, x):
            git(["worktree", "remove", "--force", x["worktree"]], st.root, check=False)
            git(["branch", "-D", x["branch"]], st.root, check=False)
            x.update({"status": "done", "worktree": "", "branch": "",
                      "note": "nothing to land: " + fields["REMAINING"]})
            st.save(run)
            st.log("report %s DONE, nothing to land" % x["id"])
            print("REPORT OK %s DONE — nothing to land, closed" % x["id"])
        else:
            st.save(run)
            st.log("report %s DONE gate=%s" % (x["id"], fields["GATE"]))
            print("REPORT OK %s DONE — now: orch.py land %s" % (x["id"], x["id"]))
        return
    if res == "PARTIAL":
        x["note"] = "partial: " + fields["REMAINING"]
        return give_up_or_requeue(st, run, x, x["note"], exit_code=0)
    if res == "BLOCKED":
        dec = parse_decision(fields["DECISION"]) if fields["DECISION"] != "none" else None
        if not dec:
            x["status"], x["note"] = "failed", "blocked without a decision: " + fields["REMAINING"]
            st.save(run)
            st.log("report %s BLOCKED, no decision -> failed" % x["id"])
            print("REPORT FAILED %s — BLOCKED but named no decision; relay to the user: %s"
                  % (x["id"], fields["REMAINING"]))
            sys.exit(1)
        did = add_decision(run, dec[0], dec[1], dec[2], dec[3], x["id"])
        x["needs"].append(did)
        x["status"], x["note"] = "queued", "blocked on %s: %s" % (did, fields["REMAINING"])
        x["attempt"] = max(0, x["attempt"] - 1)  # waiting on the user costs no attempt
        st.save(run)
        st.log("report %s BLOCKED -> %s" % (x["id"], did))
        print("REPORT OK %s BLOCKED — opened %s: %s" % (x["id"], did, dec[0]))
        return
    x["status"], x["note"] = "failed", "failed: " + fields["REMAINING"]
    st.save(run)
    st.log("report %s FAILED: %s" % (x["id"], fields["REMAINING"]))
    print("REPORT OK %s FAILED — relay to the user: %s (worktree kept: %s)"
          % (x["id"], fields["REMAINING"], x["worktree"]))


def nothing_to_land(st, x):
    """A builder chunk whose branch holds no commits and whose worktree holds no edits."""
    cfg = st.config()
    if not os.path.isdir(x["worktree"]):
        return False
    ahead = git(["rev-list", "--count", "%s..HEAD" % cfg["base"]], x["worktree"], check=False).stdout.strip()
    dirty = git(["status", "--porcelain", "--untracked-files=no"], x["worktree"], check=False).stdout.strip()
    return ahead == "0" and not dirty


def give_up_or_requeue(st, run, x, why, exit_code=4):
    if x["attempt"] >= MAX_ATTEMPTS:
        x["status"], x["note"] = "failed", "no progress after %d attempts: %s" % (x["attempt"], why)
        st.save(run)
        st.log("%s failed: %s" % (x["id"], x["note"]))
        print("REPORT FAILED %s — %s; relay to the user, never relaunch" % (x["id"], x["note"]))
        sys.exit(1)
    x["status"], x["note"] = "queued", why
    st.save(run)
    st.log("%s requeued after attempt %d: %s" % (x["id"], x["attempt"], why))
    print("REPORT REQUEUED %s — the next cycle relaunches it from its worktree (%s)" % (x["id"], why))
    if exit_code:
        sys.exit(exit_code)


# ---------------------------------------------------------------- land

def cmd_land(st, a):
    run, cfg = st.load(), st.config()
    x = find(run, a.id)
    if x["kind"] != "chunk" or x["read_only"] or x["status"] != "running":
        raise Refused("%s is not a running builder chunk" % x["id"])
    wt, br, base, root = x["worktree"], x["branch"], cfg["base"], st.root
    lock = os.path.join(st.dir, "land.lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        raise Refused("another land is in progress (%s); land one branch at a time" % lock)
    try:
        refused = land_steps(st, cfg, x, wt, br, base, root)
    finally:
        os.remove(lock)
    if refused:
        x["land_refusals"] += 1
        if x["land_refusals"] >= MAX_LAND_REFUSALS:
            x["status"], x["note"] = "failed", "land refused twice: " + refused
            st.save(run)
            st.log("land %s REFUSED (final): %s" % (x["id"], refused))
            print("LAND REFUSED %s — %s. Second refusal: FAILED. Relay to the user; worktree kept: %s"
                  % (x["id"], refused, wt))
            sys.exit(1)
        st.save(run)
        st.log("land %s REFUSED: %s" % (x["id"], refused))
        print("LAND REFUSED %s — %s. %s is untouched; worktree kept: %s" % (x["id"], refused, base, wt))
        print("SEND: Landing %s was refused: %s. Fix it in %s on %s, commit, re-run the test command, "
              "and reply with the 10 report lines again." % (x["id"], refused, wt, br))
        sys.exit(1)
    run = st.load()
    x = find(run, a.id)
    st.save(run)
    print("LANDED %s %s -> %s" % (x["id"], " ".join(x["shas"]) or "(no commits)", base))
    push = run["standing"].get("push", "")
    if a.push or PUSH_EACH.match(push):
        p = git(["push", "origin", base], root, check=False)
        line = "PUSHED %s" % base if p.returncode == 0 else "PUSH FAILED %s — %s (the push is now the user's)" % (
            base, (p.stderr or p.stdout).strip().splitlines()[-1:] or "")
        st.log(line)
        print(line)


def land_steps(st, cfg, x, wt, br, base, root):
    """Return a refusal reason, or '' after a fast-forward of base."""
    if not os.path.isdir(wt):
        return "worktree %s is gone" % wt
    if git(["status", "--porcelain"], wt).stdout.strip():
        return "worktree has uncommitted changes"
    head = git(["rev-parse", "HEAD"], wt).stdout.strip()
    if git(["rev-parse", "--abbrev-ref", "HEAD"], wt).stdout.strip() != br:
        return "worktree is not on branch %s" % br
    old_base = git(["rev-parse", base], root).stdout.strip()
    if head == old_base or git(["merge-base", "--is-ancestor", head, old_base], root, check=False).returncode == 0:
        return "branch %s has no commits beyond %s" % (br, base)
    if git(["rebase", base], wt, check=False).returncode != 0:
        git(["rebase", "--abort"], wt, check=False)
        return "rebase onto %s conflicts" % base
    gates = os.path.join(st.dir, "gates")
    os.makedirs(gates, exist_ok=True)
    out_path = os.path.join(gates, "%s-land.out" % x["id"])
    with open(out_path, "w") as out:
        try:
            p = subprocess.run(cfg["test_cmd"], shell=True, cwd=wt, stdout=out, stderr=subprocess.STDOUT,
                               timeout=cfg.get("gate_timeout_min", 30) * 60, start_new_session=True)
            rc = p.returncode
        except subprocess.TimeoutExpired:
            rc = "timeout"
    if rc != 0:
        tail = open(out_path).read().strip().splitlines()[-3:]
        return "test command %s after rebase (%s; last lines: %s)" % (
            "timed out" if rc == "timeout" else "exit %s" % rc, out_path, " / ".join(tail))
    new_head = git(["rev-parse", "HEAD"], wt).stdout.strip()
    if git(["rev-parse", base], root).stdout.strip() != old_base:
        return "%s moved during the gate; land again" % base
    on_base = git(["rev-parse", "--abbrev-ref", "HEAD"], root).stdout.strip() == base
    if on_base:
        if git(["status", "--porcelain", "--untracked-files=no"], root).stdout.strip():
            return "the main checkout %s has uncommitted changes on %s" % (root, base)
        if git(["merge", "--ff-only", br], root, check=False).returncode != 0:
            return "fast-forward of %s refused" % base
    else:
        git(["update-ref", "refs/heads/" + base, new_head, old_base], root)
    shas = git(["log", "--format=%h", "%s..%s" % (old_base, new_head)], root).stdout.split()
    # --force: tracked files are clean (checked above); what is left is the gate's own
    # untracked output (__pycache__, build dirs), which must not strand the worktree.
    rm = git(["worktree", "remove", "--force", wt], root, check=False)
    if rm.returncode == 0:
        git(["branch", "-D", br], root, check=False)
    else:
        st.log("land %s: worktree %s not removed: %s" % (x["id"], wt, rm.stderr.strip()))
        print("WORKTREE LEFT %s (remove it by hand): %s" % (wt, rm.stderr.strip()))
    run = st.load()
    y = find(run, x["id"])
    y.update({"status": "done", "shas": shas, "note": "landed", "worktree": "", "branch": ""})
    for k in ("gate", "review", "assumed", "next"):
        y[k] = x[k]
    st.save(run)
    st.log("land %s LANDED %s" % (x["id"], " ".join(shas)))
    return ""


# ---------------------------------------------------------------- decisions

def cmd_decisions(st, a):
    run = st.load()
    open_ = [d for d in run["decisions"] if d["status"] != "answered"]
    if not open_:
        print("DECISIONS: none open")
    for d in open_:
        waits = [x["id"] for x in chunks(run) if d["id"] in x["needs"] and x["status"] == "queued"]
        print("%s  %s" % (d["id"], d["question"]))
        for o in d["options"]:
            print("      %s" % o)
        print("      recommended: %s · class: %s · unblocks: %s" % (
            d["recommended"] or "none", d["class"] or "unset", ", ".join(waits) or "nothing queued"))


def cmd_decide(st, a):
    run = st.load()
    d = find(run, a.id)
    if not a.id.startswith("D-"):
        raise Refused("%s is not a decision" % a.id)
    if d["status"] == "answered":
        raise Refused("%s is already answered: %s" % (d["id"], d["answer"]))
    d.update({"status": "answered", "answer": a.answer, "how": "user"})
    st.save(run)
    st.log("decide %s: %s" % (d["id"], a.answer))
    print("ANSWERED %s: %s" % (d["id"], a.answer))


DELEGABLE = ("reversible",)


def cmd_delegate(st, a):
    run = st.load()
    words = run["standing"].get("delegation", "").strip()
    if run["mode"] != "auto":
        raise Refused("delegation is an /orch-auto verb; in an attended run the user answers")
    for d in run["decisions"]:
        if d["status"] == "answered":
            continue
        if not words or words.lower() in ("none", "no"):
            why = "the delegation slot is %r" % (words or "unset")
        elif d["class"] not in DELEGABLE:
            why = "class is %r (only reversible is delegated)" % (d["class"] or "unset")
        elif not d["recommended"]:
            why = "no recommended option"
        else:
            d.update({"status": "answered", "answer": "%s — delegated, overridable" % d["recommended"],
                      "how": "delegated"})
            st.log("delegate %s %s" % (d["id"], d["recommended"]))
            print("ANSWERED %s by delegation: %s — %s" % (d["id"], d["recommended"], d["question"]))
            continue
        print("REFUSED %s — %s; it stays the user's" % (d["id"], why))
    st.save(run)


# ---------------------------------------------------------------- finish rounds

def cmd_done_when(st, a):
    run = st.load()
    if run["mode"] != "auto":
        raise Refused("done-when rows exist only in an /orch-auto run")
    if a.action is None:
        for r in run["done_when"]:
            print("%2d  %-9s %s%s" % (r["n"], r["state"].upper(), r["text"],
                                      "  — " + r["evidence"] if r["evidence"] else ""))
        return
    if a.action == "start":
        if run.get("review_running"):
            raise Refused("round %d is still open" % run["review_running"])
        if run["round"] >= run["rounds"]:
            raise Refused("round cap %d reached" % run["rounds"])
        run["round"] += 1
        run["review_running"] = run["round"]
        for r in run["done_when"]:
            if r["state"] != "user-only":
                r["state"], r["evidence"] = "open", ""
        st.save(run)
        st.log("finish-review round %d started" % run["round"])
        cfg = st.config()
        plan_dir = os.path.join(st.dir, "plans")
        os.makedirs(plan_dir, exist_ok=True)
        print("VALUES: ROUND: %d of %d | REPO: %s | BASE: %s | TEST_CMD: %s | PLAN_FILE: %s | ORCH: %s"
              % (run["round"], run["rounds"], st.root, cfg["base"], cfg["test_cmd"],
                 os.path.join(plan_dir, "round-%d.json" % run["round"]), os.path.abspath(__file__)))
        return
    if a.action == "close":
        run["review_running"] = None
        st.save(run)
        st.log("finish-review round %d closed" % run["round"])
        print("CLOSED round %d" % run["round"])
        return
    try:
        n = int(a.action)
    except ValueError:
        raise Refused("done-when [start | close | <n> met|missing|user-only \"<evidence>\"]")
    row = next((r for r in run["done_when"] if r["n"] == n), None)
    if not row:
        raise Refused("no row %d" % n)
    if a.state not in ("met", "missing", "user-only") or not a.evidence:
        raise Refused("row %d needs a state (met|missing|user-only) and evidence" % n)
    row.update({"state": a.state, "evidence": a.evidence})
    st.save(run)
    st.log("done-when %d %s: %s" % (n, a.state, a.evidence))
    print("ROW %d %s" % (n, a.state.upper()))


# ---------------------------------------------------------------- status / wind-down

def cmd_status(st, a):
    run, cfg = st.load(), st.config()
    by = {}
    for x in run["items"]:
        by.setdefault(x["status"], []).append(x["id"])
    print("run %s · mode %s%s · %s" % (run["created"], run["mode"],
                                      " · round %d/%d" % (run["round"], run["rounds"]) if run["mode"] == "auto" else "",
                                      "WOUND DOWN (%s)" % run["wound_down"]["reason"] if run["wound_down"] else "open"))
    print("  " + " · ".join("%s %d" % (k, len(v)) for k, v in sorted(by.items())) if by else "  empty queue")
    for x in run["items"]:
        if x["status"] in ("running", "planning"):
            print("  RUNNING %s %s agent=%s%s" % (x["id"], x["title"][:60], x.get("agent") or "?",
                                                  " wt=" + x["worktree"] if x.get("worktree") not in ("", "none", None) else ""))
    if run.get("review_running"):
        print("  RUNNING finish review round %d" % run["review_running"])
    opened = [d["id"] for d in run["decisions"] if d["status"] != "answered"]
    print("  decisions open: %s" % (", ".join(opened) or "none"))
    if a.all:
        for x in run["items"]:
            print("  %-5s %-9s %s%s" % (x["id"], x["status"], x["title"][:70],
                                        "  (" + x["note"] + ")" if x.get("note") else ""))


def cmd_reconcile(st, a):
    """After a restart or a compaction: what is each RUNNING row really?"""
    run, cfg = st.load(), st.config()
    rows = [x for x in run["items"] if x["status"] in ("running", "planning")]
    for x in rows:
        if x["kind"] != "chunk" or x["read_only"]:
            verdict = "requeue"
        elif os.path.isdir(x["worktree"]):
            ahead = git(["rev-list", "--count", "%s..HEAD" % cfg["base"]], x["worktree"], check=False).stdout.strip()
            dirty = git(["status", "--porcelain"], x["worktree"], check=False).stdout.strip()
            verdict = "resume" if (ahead not in ("", "0") or dirty) else "requeue"
        else:
            verdict = "requeue"
        print("%-7s %s %s%s" % (verdict, x["id"], x["title"][:60],
                                " (worktree %s has work: relaunch keeps it)" % x["worktree"] if verdict == "resume" else ""))
        if a.apply:
            x["status"] = "queued" if x["kind"] == "chunk" else "unplanned"
            if x["kind"] == "chunk":
                x["attempt"] = max(0, x["attempt"] - 1)
            x["note"] = "reconciled: %s" % verdict
    if a.apply and rows:
        st.save(run)
        st.log("reconcile --apply: %d row(s) back to the queue" % len(rows))
    print("reconcile: %d running row(s)%s" % (len(rows), ", requeued" if a.apply and rows else ""))


REASONS = ("FINISHED", "USER-ONLY", "ROUNDS", "DRY", "STRANDED", "STOP")


def cmd_winddown(st, a):
    run, cfg = st.load(), st.config()
    if run.get("wound_down"):
        raise Refused("already wound down (%s)" % run["wound_down"]["reason"])
    if running_count(run):
        raise Refused("%d still running: wait for them to return first" % running_count(run))
    reason = a.reason.upper()
    if reason not in REASONS:
        raise Refused("reason must be one of %s" % ", ".join(REASONS))
    queued = [x for x in run["items"] if x["status"] in ("queued", "unplanned")]
    rows_open = [r for r in run["done_when"] if r["state"] not in ("met", "user-only")]
    gated = [x for x in queued if x["kind"] == "chunk" and any(decision_open(run, d) for d in x["needs"])]
    if reason == "FINISHED" and (run["mode"] != "auto" or rows_open or queued):
        raise Refused("not FINISHED: %d row(s) open, %d item(s) queued" % (len(rows_open), len(queued)))
    if reason == "USER-ONLY" and (not gated or len(gated) != len(queued)):
        raise Refused("not USER-ONLY: %d queued, %d of them gated on the user" % (len(queued), len(gated)))
    if reason == "ROUNDS" and run["round"] < run["rounds"]:
        raise Refused("not ROUNDS: round %d of %d" % (run["round"], run["rounds"]))
    if reason == "DRY" and queued:
        raise Refused("not DRY: %d item(s) still queued" % len(queued))
    out = []
    out.append("# orch run report — %s" % now()[:16].replace("T", " "))
    out.append("")
    out.append("Run %s · mode %s · wound down: **%s**%s" % (run["created"], run["mode"], reason,
                                                          " — " + a.note if a.note else ""))
    out.append("")
    out.append("## Built")
    built = [x for x in chunks(run) if x["status"] == "done"]
    for x in built or []:
        out.append("- %s %s — %s%s" % (x["id"], x["title"], " ".join(x["shas"]) or "read-only",
                                       ("; review: " + x["review"]) if x.get("review") else ""))
    if not built:
        out.append("- nothing")
    assumed = [x for x in chunks(run) if x.get("assumed")]
    delegated = [d for d in run["decisions"] if d["how"] == "delegated"]
    out.append("")
    out.append("## Decided for you (overridable)")
    for d in delegated:
        out.append("- %s %s → %s" % (d["id"], d["question"], d["answer"]))
    for x in assumed:
        out.append("- %s assumed: %s" % (x["id"], x["assumed"]))
    if not delegated and not assumed:
        out.append("- nothing")
    out.append("")
    out.append("## Only you can do")
    owed = []
    for d in run["decisions"]:
        if d["status"] != "answered":
            owed.append("decide %s: %s (%s; recommended %s)" % (d["id"], d["question"],
                                                                "; ".join(d["options"]), d["recommended"] or "none"))
    for x in chunks(run):
        if x["status"] == "failed":
            owed.append("%s failed — %s%s" % (x["id"], x["note"],
                                               "; worktree %s, branch %s" % (x["worktree"], x["branch"])
                                               if x.get("worktree") not in ("", "none") else ""))
    for r in run["done_when"]:
        if r["state"] == "user-only":
            owed.append("done-when %d: %s (%s)" % (r["n"], r["text"], r["evidence"]))
        elif r["state"] != "met":
            owed.append("done-when %d still %s: %s" % (r["n"], r["state"], r["text"]))
    push = run["standing"].get("push", "")
    if built and not PUSH_EACH.match(push):
        owed.append("push %s when you are happy with it (nothing was pushed)" % cfg["base"])
    out.extend("- " + o for o in owed) if owed else out.append("- nothing")
    follow = [n for x in chunks(run) for n in x.get("next", [])]
    follow.append("/orch-retro — what this run's ledger suggests changing in the agents and commands "
                  "(proposals only, nothing applied)")
    out.append("")
    out.append("## Suggested follow-ups (not queued)")
    out.extend("- " + n for n in follow)
    path = os.path.join(st.dir, "report-%s.md" % now().replace(":", "")[:15])
    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")
    run["wound_down"] = {"reason": reason, "at": now(), "report": path}
    st.save(run)
    st.log("winddown %s -> %s" % (reason, path))
    print("\n".join(out))
    print("\nREPORT %s" % path)


# ---------------------------------------------------------------- retro

LEDGER_LINE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)  (.*)$")
RETRO_MAX = 5
RETRO_TARGETS = {"report-miss": "agents/builder.md", "land-refused": "commands/orch.md",
                 "retry": "agents/planner.md", "overrun": "agents/planner.md"}
# "main checkout" before "uncommitted": the user's dirty checkout is not the builder's
REFUSAL_CLASSES = (("test command", "red-gate"), ("conflicts", "rebase-conflict"),
                   ("main checkout", "main-checkout-dirty"), ("uncommitted", "dirty-worktree"),
                   ("no commits", "no-commits"), ("moved during", "base-moved"),
                   ("is gone", "worktree-gone"), ("not on branch", "wrong-branch"),
                   ("fast-forward", "fast-forward-refused"))
REFUSAL_HINTS = {"red-gate": "run the test command on the rebased branch and make it pass before replying",
                 "rebase-conflict": "rebase onto the base branch and resolve the conflict before replying",
                 "dirty-worktree": "commit or discard every change in the worktree before replying",
                 "no-commits": "commit the work on its branch before replying"}
NAMED_PATH = re.compile(r"\b((?:agents|commands|scripts|skills)/[\w./-]*\w)")


def clip(text, n=80):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1] + "…"


def kebab(text, words=3):
    text = re.sub(r"'[^']*'|\"[^\"]*\"", " ", text.lower())  # quoted values vary; the lesson does not
    return "-".join(re.findall(r"[a-z0-9]+", text)[:words]) or "unnamed"


def ledger_signal(text):
    """(signal, signature, chunk, why) for a ledger line the retro counts, else None."""
    m = re.match(r"retro note: (.+)", text)
    if m:
        return "note", kebab(m.group(1), 4), "", m.group(1)
    m = re.match(r"report-miss (C-\d+) attempt \d+: (.+)", text)
    if m:
        return "report-miss", kebab(m.group(2)), m.group(1), m.group(2)
    m = re.match(r"land (C-\d+) REFUSED(?: \(final\))?: (.+)", text)
    if m:
        sig = next((s for k, s in REFUSAL_CLASSES if k in m.group(2)), None) or kebab(m.group(2))
        return "land-refused", sig, m.group(1), m.group(2)
    m = re.match(r"(C-\d+) (?:requeued after attempt \d+|failed): (?:no progress after \d+ attempts: )?(.+)", text)
    if m:
        return "retry", kebab(m.group(2).split(":")[0]), m.group(1), m.group(2)
    m = re.match(r"report (C-\d+) FAILED: (.*)", text)
    if m:
        return "retry", "builder-failed", m.group(1), m.group(2) or "failed"
    return None


def ledger_runs(path):
    """The ledger cut at each `init` line into runs of (line number, time, text); the last is this run."""
    runs = [[]]
    with open(path) as f:
        for n, line in enumerate(f, 1):
            m = LEDGER_LINE.match(line.rstrip("\n"))
            if not m:
                continue
            if m.group(2).startswith("init ") and runs[-1]:
                runs.append([])
            runs[-1].append((n, m.group(1), m.group(2)))
    return runs


def retro_finding(key, c):
    """(target, why, proposal) for one candidate."""
    kind, sig = key
    why0, ids = clip(c["whys"][0], 60), ", ".join(c["chunks"])
    if kind == "note":
        m = NAMED_PATH.search(c["whys"][0])
        target = m.group(1) if m else "commands/orch.md"
        return (target, "the orchestrator noted it during the run",
                "Add the lesson where it applies in %s: \"%s\"" % (target, clip(c["whys"][0], 200)))
    target = RETRO_TARGETS[kind]
    n = len(c["lines"])
    if kind == "report-miss":
        return (target, "the queue refused %d report block(s) (%s: '%s'); each cost a resume round-trip" % (n, ids, why0),
                "Under \"## Finish\", add: \"Before you send, check the 10 lines against the template: every "
                "label once, in order, CHUNK first and NEXT last, nothing after NEXT.\"")
    if kind == "land-refused":
        hint = REFUSAL_HINTS.get(sig, "fix the cause ('%s') before replying" % why0)
        return (target, "%d land(s) refused as %s (%s); each sends the branch back, and a second refusal "
                        "fails the chunk" % (n, sig, ids),
                "In \"## 2. When an agent finishes\", under LAND REFUSED, add: \"On a %s refusal, the SEND text "
                "also tells the builder to %s.\"" % (sig, hint))
    if kind == "retry":
        return (target, "%d attempt(s) ended without landing (%s: '%s'); each relaunch repeats the builder's work"
                % (n, ids, why0),
                "Under \"## Shape the work\", add: \"Work that needed more than one attempt (%s) was too big or "
                "too vague: split it so each chunk fits one attempt and names its proving test.\"" % sig)
    return (target, "%s ran past twice the estimate, so the plan's sizing was off" % "; ".join(c["whys"]),
            "Under \"## Shape the work\", add: \"%s chunks ran past twice their est_min (%s): estimate that "
            "kind at the observed rate, or split it.\"" % (sig, "; ".join(c["whys"])))


def cmd_retro(st, a):
    """Write 3-5 evidence-cited proposals from this run's ledger to .orch/retro.md; apply nothing.

    A signal becomes a finding with 2+ ledger lines this run, the same signature in an earlier
    run of the ledger, or a `retro note:` line (orch.py log "retro note: ..."). None: no file.
    """
    run = st.load()
    runs = ledger_runs(st.ledger_path)
    this, earlier = runs[-1], runs[:-1]
    seen_before = {}
    for r in earlier:
        for key in {s[:2] for s in (ledger_signal(t) for _, _, t in r) if s}:
            seen_before[key] = seen_before.get(key, 0) + 1
    cands = {}

    def hit(key, n, chunk, why):
        c = cands.setdefault(key, {"lines": [], "chunks": [], "whys": []})
        c["lines"].append(n)
        if chunk and chunk not in c["chunks"]:
            c["chunks"].append(chunk)
        c["whys"].append(why)

    starts, ends = {}, {}
    for n, ts, text in this:
        s = ledger_signal(text)
        if s:
            hit(("note", n) if s[0] == "note" else s[:2], n, s[2], s[3])
        m = re.match(r"prepare (C-\d+) attempt", text)
        if m and m.group(1) not in starts:
            starts[m.group(1)] = (n, ts)
        m = re.match(r"land (C-\d+) LANDED|report (C-\d+) DONE", text)
        if m:
            ends[m.group(1) or m.group(2)] = (n, ts)
    for x in chunks(run):
        est, cid = x.get("est"), x["id"]
        if not isinstance(est, (int, float)) or cid not in starts or cid not in ends:
            continue
        took = (dt.datetime.fromisoformat(ends[cid][1]) - dt.datetime.fromisoformat(starts[cid][1])).total_seconds() / 60
        if took >= 20 and took > 2 * est:
            hit(("overrun", "read-only" if x.get("read_only") else "builder"), ends[cid][0], cid,
                "%s est %g min, took %d (from line %d)" % (cid, est, round(took), starts[cid][0]))
    found = []
    for key, c in cands.items():
        c["runs"] = 1 + seen_before.get(key, 0)
        if key[0] == "note" or len(c["lines"]) >= 2 or c["runs"] >= 2:
            found.append((key, c))
    found.sort(key=lambda kc: (kc[0][0] != "note", -kc[1]["runs"] * len(kc[1]["lines"]), kc[1]["lines"][0]))
    found = found[:RETRO_MAX]
    path = os.path.join(st.dir, "retro.md")
    if not found:
        if os.path.exists(path):
            os.remove(path)  # a stale report from an earlier run must not read as this run's
        print("retro: no findings — %d signal line(s) this run, none with 2+ lines, an earlier run "
              "or a retro note; no file written" % sum(len(c["lines"]) for c in cands.values()))
        return
    total = max(1, sum(1 for r in runs if r and r[0][2].startswith("init ")))
    counts = {}
    for (kind, _), c in cands.items():
        counts[kind] = counts.get(kind, 0) + len(c["lines"])
    listed = ["%s assumed: %s" % (x["id"], clip(x["assumed"])) for x in chunks(run) if x.get("assumed")]
    listed += ["%s delegated: %s" % (d["id"], clip(d["answer"])) for d in run["decisions"] if d.get("how") == "delegated"]
    out = ["# orch retro — run %s" % run["created"],
           "scanned %s · %d chunks · signals: %s · ledger: %d run(s)" % (
               now(), len(chunks(run)), ", ".join("%s %d" % kv for kv in sorted(counts.items())), total),
           "listed, not proposed: %s" % ("; ".join(listed) or "none"), ""]
    for i, (key, c) in enumerate(found, 1):
        target, why, proposal = retro_finding(key, c)
        label = "retro note" if key[0] == "note" else "%s %s" % key
        out += ["## R-%d  add  %s" % (i, target),
                "evidence: ledger line%s %s (%d this run) · %s: %d of %d runs" % (
                    "s" if len(c["lines"]) > 1 else "", ", ".join(map(str, c["lines"])), len(c["lines"]),
                    label, c["runs"], total),
                "chunks: %s" % (", ".join(c["chunks"]) or "none"),
                "why: %s" % why,
                "--- proposed ---",
                proposal, ""]
    out.append("RESULT: %d finding(s) · proposals only: nothing was applied" % len(found))
    with open(path + ".tmp", "w") as f:
        f.write("\n".join(out) + "\n")
    os.replace(path + ".tmp", path)
    print("\n".join(out))
    print("\nRETRO %s" % path)


def cmd_log(st, a):
    st.load()
    st.log(a.text)
    print("LOGGED")


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(prog="orch.py", description=__doc__.split("\n")[0])
    ap.add_argument("--repo", default=os.getcwd(), help="any path inside the repo (default: cwd)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("setup"); p.set_defaults(fn=cmd_setup)
    p.add_argument("--test-cmd"); p.add_argument("--setup-cmd"); p.add_argument("--base")
    p.add_argument("--worktrees"); p.add_argument("--agents", type=int); p.add_argument("--load", type=float)
    p.add_argument("--gate-timeout", type=int); p.add_argument("--review", choices=("on", "off"))

    p = sub.add_parser("init"); p.set_defaults(fn=cmd_init)
    p.add_argument("--auto", action="store_true"); p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--checklist")

    p = sub.add_parser("standing"); p.set_defaults(fn=cmd_standing)
    p.add_argument("slot", nargs="?"); p.add_argument("words", nargs="?")

    p = sub.add_parser("add"); p.set_defaults(fn=cmd_add)
    p.add_argument("--kind", required=True, choices=("chunk", "idea", "bug", "feature"))
    p.add_argument("--text", required=True); p.add_argument("--brief"); p.add_argument("--files")
    p.add_argument("--model"); p.add_argument("--after"); p.add_argument("--needs")
    p.add_argument("--read-only", action="store_true")

    p = sub.add_parser("add-plan"); p.set_defaults(fn=cmd_add_plan); p.add_argument("file")
    p = sub.add_parser("next"); p.set_defaults(fn=cmd_next)
    p = sub.add_parser("prepare"); p.set_defaults(fn=cmd_prepare); p.add_argument("id")
    p = sub.add_parser("mark"); p.set_defaults(fn=cmd_mark)
    p.add_argument("id"); p.add_argument("state"); p.add_argument("note", nargs="?")
    p = sub.add_parser("report"); p.set_defaults(fn=cmd_report); p.add_argument("id"); p.add_argument("file")
    p = sub.add_parser("land"); p.set_defaults(fn=cmd_land); p.add_argument("id")
    p.add_argument("--push", action="store_true")
    p = sub.add_parser("decisions"); p.set_defaults(fn=cmd_decisions)
    p = sub.add_parser("decide"); p.set_defaults(fn=cmd_decide); p.add_argument("id"); p.add_argument("answer")
    p = sub.add_parser("delegate"); p.set_defaults(fn=cmd_delegate)
    p = sub.add_parser("done-when"); p.set_defaults(fn=cmd_done_when)
    p.add_argument("action", nargs="?"); p.add_argument("state", nargs="?"); p.add_argument("evidence", nargs="?")
    p = sub.add_parser("status"); p.set_defaults(fn=cmd_status); p.add_argument("--all", action="store_true")
    p = sub.add_parser("reconcile"); p.set_defaults(fn=cmd_reconcile); p.add_argument("--apply", action="store_true")
    p = sub.add_parser("winddown"); p.set_defaults(fn=cmd_winddown)
    p.add_argument("--reason", required=True); p.add_argument("--note")
    p = sub.add_parser("retro"); p.set_defaults(fn=cmd_retro)
    p = sub.add_parser("log"); p.set_defaults(fn=cmd_log); p.add_argument("text")

    a = ap.parse_args(argv)
    try:
        with Store(repo_root(a.repo)) as st:
            a.fn(st, a)
    except Refused as e:
        print("REFUSED: %s" % e)
        sys.exit(2)


if __name__ == "__main__":
    main()
