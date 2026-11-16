---
description: Set up orch-kit in this repo — find the test command and base branch, confirm them, write .orch/config.json
argument-hint: "[--test-cmd '<cmd>'] [--base <branch>]"
---

Set up orch-kit for the git repo in the current directory. `ORCH` below is `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/orch.py"`.

1. **Check the basics.** `git rev-parse --show-toplevel` (must be a git repo), `python3 --version` (3.8 or newer), and that the working tree has at least one commit.
2. **Find the base branch.** `git symbolic-ref --short refs/remotes/origin/HEAD`, else `main`, else `master`.
3. **Find the test command**, the one command that proves the repo is green. Look in this order and take the first that exists:
   - the repo's CLAUDE.md, AGENTS.md or CONTRIBUTING (a stated test command wins);
   - `package.json` scripts.test → `npm test` (or `pnpm test` / `yarn test` per the lockfile);
   - `pyproject.toml` / `pytest.ini` / `tests/` → `python3 -m pytest -q`;
   - `go.mod` → `go test ./...`; `Cargo.toml` → `cargo test`; `Makefile` with a `test:` target → `make test`.
   Also note a setup command a fresh worktree needs (`npm ci`, `pip install -e .`, copying a `.env`), if any.
4. **Confirm with the user** in one message: base branch, test command, setup command, and the defaults (3 agents at once, worktrees in `../<repo>-orch-wt/`, a fresh-context review of every chunk). Use what they say; arguments given to this command count as already confirmed: $ARGUMENTS
5. **Write it:** `ORCH setup --test-cmd '<cmd>' [--setup-cmd '<cmd>'] [--base <b>] [--agents N] [--review on|off]`. It refuses an unknown base branch; fix and re-run. It adds `.orch/` to `.git/info/exclude`, so run state never shows up in `git status`.
6. **Prove the gate once:** run the test command and show the pass/fail counts. A red or empty suite at setup means every land will be refused: say so plainly.
7. **Tell the user how to run it**, briefly:
   - `/orch + feature: <what you want>` queues work and starts the loop; `/orch` alone resumes it.
   - `/orch-auto <goal>` runs unattended to a finish line. Permission prompts stall an unattended run, so start it in a mode that does not stop for routine edits and git commands (for example auto mode, or `acceptEdits` plus allow rules for `git` and `python3`), and keep deny rules for anything you never want run unasked (`git push --force`, `rm -rf`).
