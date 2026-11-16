---
description: Unattended /orch — asks a short launch checklist once, then plans, builds, lands and checks its own finish line without asking again, and ends with a report of what it built, what it decided for you and what only you can do.
argument-hint: "<goal, in your words>"
---

This is `/orch` run unattended. Follow `/orch` (the file `${CLAUDE_PLUGIN_ROOT}/commands/orch.md`) for everything, with the differences below. `ORCH` is `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/orch.py"`.

Goal: $ARGUMENTS

## 1. Launch checklist: the only time you ask
Ask these in ONE message (AskUserQuestion where options fit) and wait for the answers. After this, you do not ask the user anything until the wind-down. If the goal text already answers them (`stop: … | off-limits: … | delegation: … | push: … | done-when: … | rounds: …`, as a scheduled or headless run would pass them), use those answers and ask only for a slot still missing; with no way to ask (a headless run), a missing slot is `none`, except `push`, which is `never`.
1. **Stop:** when should the run stop? (e.g. "when the goal's done-when list is met", "after 6 chunks", "by 18:00")
2. **Off-limits:** files, folders or actions no agent may touch (e.g. "migrations/", "the payments module", "deleting anything").
3. **Delegation:** may the run answer a decision for you when it is cheap to reverse and has a recommended option? ("yes, reversible ones" or "none")
4. **Push:** "never" (default: you push at the end), or "after each land".
5. **Done when:** anything to add to the finish line beyond the defaults (the plan's chunks done, tests green on the final base sha, a README that covers it, no stray worktrees).
6. **Rounds:** how many finish-review rounds before giving up (default 3).

Then:
- Write the done-when list as `- ` lines (defaults first, then theirs) to `.orch/checklist.md`.
- `ORCH init --auto --rounds <n> --checklist .orch/checklist.md`
- Record each answer in their own words: `ORCH standing stop "<words>"`, likewise `off-limits`, `delegation`, `push`. `next` refuses to launch until all four are set.
- `ORCH add --kind feature --text "<the goal>"`.

Unattended runs stall on permission prompts. If you are in a mode that prompts for every edit or git command, say so now in one line and suggest restarting in a less interactive mode, then carry on.

## 2. The loop: differences from /orch
- Every prompt you send a planner or builder ends with one line: `STANDING: stop: <…> | off-limits: <…>`.
- `NEEDS-YOU` → first `ORCH delegate`. It answers only reversible decisions that have a recommended option, and only when the delegation answer allows it; everything else stays REFUSED and is the user's. Then `ORCH next` again: if other work can run, run it. If only user-gated chunks remain, wind down with `USER-ONLY`.
- `STRANDED` → wind down with `STRANDED`. Do not retry failed chunks.
- `FINISH-REVIEW round r of R` → the queue ran dry, which is not the same as done. `ORCH done-when start`, then launch an Agent in the **background**: subagent_type `orch-kit:finish-reviewer`, description `finish review r`, prompt = the start output verbatim plus the STANDING line. When it returns, if `ORCH status` still shows the round running, run `ORCH done-when close`. Then loop: any chunks it planned run like any others.
- `DRY` → every row is met or user-only: wind down with `FINISHED`.
- `DRY — round cap reached` → wind down with `ROUNDS`.
- The stop condition is met (time, chunk count, anything the user named) → let running agents finish (end your turn and let their completions wake you), land what is green, launch nothing new, then wind down with `STOP`.
- Never ask the user mid-run. A question for them becomes a decision (a builder's BLOCKED) and waits for the report.

## 3. Wind-down
`ORCH winddown --reason FINISHED|USER-ONLY|ROUNDS|STRANDED|STOP [--note "<one line>"]`. It refuses a reason that is not true; fix that, never force it. It prints the report and saves it under `.orch/`. Show the user the report as printed (Built, Decided for you, Only you can do, Suggested follow-ups) and the report's path. That report is the end of the run.
