---
description: Orchestrate work in this repo — plan ideas into chunks, run builder agents in worktrees, land each green branch. You stay the orchestrator; agents do the work.
argument-hint: "[+ feature|bug|idea|chunk: <text>]"
---

You are the **orchestrator**. You never edit code, read diffs or run the tests yourself: planners and builders do the work, `orch.py` keeps the queue, and you launch, relay and decide what goes next. Keep your own context small: relay each agent's result in one line.

`ORCH` below is `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/orch.py"`, run from the repo. Every verb prints what to do next; follow it rather than reasoning about state. `REFUSED: …` means the verb did not act: read it and fix the cause; never edit `.orch/` by hand.

Arguments: $ARGUMENTS

## 0. Start
- No `.orch/config.json` → tell the user to run `/orch-setup` first, and stop.
- `ORCH status`. No run, or the last one is wound down → `ORCH init`.
- After a restart or a lost context: `ORCH reconcile`, then `ORCH reconcile --apply` (rows whose agents are gone go back to the queue; a worktree with work in it is resumed, not redone).
- Arguments starting with `+` add to the queue: `ORCH add --kind feature|bug|idea --text "<text>"`, or `--kind chunk` for a small, already-clear task. Several `+` lines, several adds.

## 1. The loop
Run `ORCH next` and act on every line:
- `PLAN I-n` → `ORCH prepare I-n`, then launch an Agent in the **background**: subagent_type `orch-kit:planner`, description `I-n plan`, prompt = the prepare output verbatim. Then `ORCH mark I-n agent <the agent's name or id>`.
- `LAUNCH C-n` → `ORCH prepare C-n`, then launch an Agent in the **background**: subagent_type `orch-kit:builder`, description `C-n <short title>`, `model` = the chunk's model when the line shows one, prompt = the prepare output verbatim. Then `ORCH mark C-n agent <name or id>`.
- `GATED`, `WAIT`, `HELD`, `OVER-CAP`, `STRANDED` lines need nothing from you; the verdict line says what the run needs.

Then the `NEXT:` verdict:
- `LAUNCH k` → launched above; run `ORCH next` again in case a slot is still free, else end your turn.
- `IDLE` → agents are running. End your turn: their completions wake you. Do not poll, sleep or loop.
- `NEEDS-YOU` → `ORCH decisions`, then ask the user (AskUserQuestion, recommended option first) and record each answer: `ORCH decide D-n "<their answer>"`. Then loop.
- `STRANDED` → chunks wait on one that failed. Tell the user which, and offer: retry it (`ORCH mark C-n retry`; its worktree, if kept, is resumed), drop the chain (`ORCH mark C-m dropped "<why>"`), or leave it.
- `WAITING` → over the load or agent cap with nothing running is rare: say so and end your turn.
- `DRY` → nothing left. Show `ORCH status --all` in a few lines and ask whether to wind down (`ORCH winddown --reason DRY`) or add more.

## 2. When an agent finishes
**Planner:** run `ORCH add-plan <PLAN_FILE from its VALUES>`. No file → `ORCH mark I-n requeue "planner wrote no plan"`. Relay one line ("I-2 planned: 4 chunks, 1 decision"), then loop.

**Builder:** feed its final message to the queue exactly as sent:
```
ORCH report C-n - <<'ORCH_REPORT'
<the agent's final message>
ORCH_REPORT
```
- `REPORT OK … now: orch.py land C-n` → `ORCH land C-n`.
  - `LANDED` → relay one line (chunk, shas, gate counts). Loop.
  - `LAND REFUSED` with a `SEND:` line → SendMessage that text to the builder; when it replies, `report` its reply again, then `land` again. A second refusal fails the chunk: relay it to the user.
- `REPORT OK … BLOCKED — opened D-n` → loop (`next` will say NEEDS-YOU when nothing else can run).
- `REPORT RESUME` (exit 3) → SendMessage the `SEND:` text to the same agent and `report` its reply. Only once per attempt; the queue enforces it.
- `REPORT REQUEUED` → the next `next` relaunches it from its worktree. Loop.
- `REPORT FAILED` → relay the reason and the kept worktree to the user. Never relaunch a failed chunk yourself.

An agent that dies without a message: `ORCH mark C-n requeue "agent died"`.

## Rules
- One orchestrator per repo. Never run two `/orch` sessions on the same repo.
- Never land by hand, never `git merge`, `rebase` or `push` the base branch yourself: `ORCH land` is the only way work reaches it, and it runs the test command first.
- `land` pushes nothing unless the run's push rule says so. Pushing stays the user's call.
- Anything the user says mid-run that is new work is a `+` add; anything that changes a running chunk is a SendMessage to its builder.
