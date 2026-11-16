# orch-kit

Run Claude Code as a small team in any git repo. You describe the work; an
orchestrator session plans it into chunks, hands each chunk to a builder agent
in its own git worktree, has a fresh-context reviewer read every diff, and lands
a branch on your base branch only after **your** test command passes on it.

- `/orch` runs it **with you**: it asks when a decision is yours.
- `/orch-auto` runs it **unattended**: you answer a short checklist once, it works
  to a finish line, and ends with a report of what it built, what it decided for
  you and what only you can do.

No memory system, no project-specific setup, no services. One Python script
(standard library, 3.8+) holds the queue, and everything it knows lives in
`.orch/` inside your repo (git-ignored).

## Install

```
/plugin marketplace add allenjung18/orch-kit
/plugin install orch-kit@orch-kit
```

Or try it without installing: `claude --plugin-dir /path/to/orch-kit`.

Then, in the repo you want to work on:

```
/orch-setup
```

It finds your base branch and test command, confirms them with you, runs the tests
once to prove the gate works, and writes `.orch/config.json`.

## Use

```
/orch + feature: export reports as CSV
/orch + bug: the date picker shows UTC instead of local time
/orch + chunk: rename utils.py to helpers.py and fix the imports
/orch                       # resume after a break or a restart
```

`feature`, `bug` and `idea` go to a planner first, which splits them into chunks
and lists any decisions that need you. A `chunk` is already small and clear and
goes straight to a builder. A bug's first chunk is always a read-only repro.

```
/orch-auto add CSV export to the reports page, with tests and a README section
```

It asks six things once (when to stop, what is off-limits, whether it may
answer reversible decisions for you, whether to push, what "done" means, how
many finish-review rounds), then runs without asking. For a scheduled or
headless run, put the answers in the goal:
`/orch-auto <goal> | stop: when done | off-limits: migrations/ | delegation: reversible | push: never`.

## How it works

```
you ──/orch──▶ orchestrator (your session: launches, relays, decides; never codes)
                 │  orch.py next / prepare / report / land
                 ├──▶ planner          (read-only)  → plan JSON: chunks + decisions
                 ├──▶ builder × N      (one worktree each) ──▶ reviewer (fresh context)
                 └──▶ finish-reviewer  (/orch-auto only) → done-when rows met / missing
```

- **The queue is a script, not prose.** `orch.py next` derives what can launch now:
  chunks wait on the chunks they come `after`, on open decisions (`needs`), on the
  agent and load caps, and are held while another running chunk touches the same
  file. The orchestrator follows its verdict line.
- **One hard gate.** `orch.py land` rebases the branch onto base, runs your test
  command, and fast-forwards base only if it passes. Nothing else reaches your
  base branch. A refusal goes back to the builder once; twice and the chunk fails.
- **Reports, not chatter.** Every builder ends with the same 10 lines (result,
  shas, gate counts, review findings, any decision, assumptions, what remains).
  A builder that forgets gets one reminder; a second miss counts as a failed attempt.
  Three attempts without landing, and the chunk fails and waits for you.
- **Decisions.** Cheap-to-reverse choices are made and listed as *assumed*. Costly
  or one-way choices stop the chunk and become a decision `D-n`. In `/orch-auto`,
  a reversible decision with a recommended option may be answered for you (if you
  allowed it); every other one waits for you in the final report.
- **Done is checked, not assumed.** In `/orch-auto` an empty queue triggers a
  finish review against the done-when list on the landed code. Missing rows become
  new chunks, up to the round cap.

## Commands and verbs

| You type | It does |
|---|---|
| `/orch-setup` | detect and confirm test command, base branch; write `.orch/config.json` |
| `/orch [+ kind: text]` | add work and run the loop, or resume it |
| `/orch-auto <goal>` | the unattended run, ending in a report |

The orchestrator drives `scripts/orch.py`; you can run it yourself to look:
`python3 scripts/orch.py status --all`, `decisions`, `next`, or `--help` for every verb.

Settings in `.orch/config.json` (change with `orch.py setup --<flag>`): `test_cmd`,
`setup_cmd` (run once in each new worktree, e.g. `npm ci`), `base`, `agents`
(how many at once, default 3), `load` (pause launching above this load average),
`gate_timeout_min`, `review` (on/off), `worktree_dir` (default `../<repo>-orch-wt/`).

## Things to know

- **Permissions.** An unattended run stops at every permission prompt. Start
  `/orch-auto` in a mode that allows routine edits and `git`/`python3` commands,
  and keep deny rules for what must never run unasked (`git push --force`, `rm -rf`).
- **Cost.** Each chunk is a builder plus an Opus reviewer. Turn review off with
  `orch.py setup --review off` for cheap, mechanical runs.
- **One orchestrator per repo.** Two sessions driving the same `.orch/` will fight.
- **Your checkout.** Builders never touch it. `land` moves your base branch: if it
  is checked out, it must be clean, and it is fast-forwarded in place.
- **Pushing** is off unless you say "after each land"; otherwise the report reminds you.
- **Failed chunks keep their worktree** so you can look; `orch.py mark C-n retry`
  puts one back in the queue.
- **Headless.** `echo "/orch-auto <goal> | stop: … | push: never" | claude -p --permission-mode acceptEdits --allowedTools "Bash,Read,Write,Edit,Glob,Grep,Agent,SendMessage" --max-budget-usd 10`
  works: `-p` waits for the background agents. A toy-repo run planned, built,
  reviewed, landed and wound down in about six minutes. Only use broad `Bash` in a
  repo you trust; set a budget.

## Tests

```
python3 -m unittest discover -s tests
```

## License

MIT
