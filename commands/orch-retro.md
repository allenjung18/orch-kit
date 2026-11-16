---
description: Look back on the orch run in this repo — 3 to 5 evidence-cited proposals for the agents and commands, read from the run's own ledger. Shows them; never applies them.
---

Show the user what this repo's orch run suggests changing. `ORCH` below is `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/orch.py"`, run from the repo. Best after `ORCH winddown`; it also works mid-run.

1. Run `ORCH retro`. It reads `.orch/run.json` and `.orch/ledger.log` and writes only `.orch/retro.md`.
   - `REFUSED: no run …` → say there is no run to look back on, and stop.
   - `retro: no findings …` → say so in one line, with its counts. No file was written; that is a complete answer, never pad it.
   - Otherwise it prints the report and `RETRO <path>`.
2. Show each finding as printed: `R-n`, its target file, the evidence (ledger line numbers and how many runs had the same signature), the why, and the proposed text. Then the `listed, not proposed` line: assumed choices and delegated decisions, for the user to look over. To show a cited line, `sed -n '<n>p' .orch/ledger.log`.
3. **Never apply a proposal.** Do not edit an agent, command, script or skill from this command, and do not queue one as work. The user decides each `R-n`; if they want one, they make the change or ask for it in a new request.

How a finding is chosen: a signal (a refused report block, a refused land, an attempt that did not land, a chunk past twice its `est_min`) becomes a finding when it shows up on two ledger lines in this run, or with the same signature in an earlier run of the same ledger. A line the orchestrator wrote with `ORCH log "retro note: <lesson, naming the file it is about>"` is always a finding and ranks first. At most 5.
