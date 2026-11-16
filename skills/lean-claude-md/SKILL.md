---
name: lean-claude-md
description: >-
  Lean pass on an always-loaded instruction file (global or project CLAUDE.md,
  AGENTS.md): measure it, audit every rule as keep / compress / move / cut,
  write a -lean copy beside it, trial that copy in headless sessions without
  swapping it in, then swap only on the user's go-ahead. Use when the user says
  "/lean-claude-md", "make my CLAUDE.md leaner", "trim the instructions",
  "audit my CLAUDE.md", "is this rule still needed", or "try the lean version
  before swapping".
argument-hint: "[path to CLAUDE.md or AGENTS.md; default ~/.claude/CLAUDE.md]"
---

# /lean-claude-md — a smaller instruction file, proven before it is swapped

Every byte of an always-loaded file is paid on every turn of every session.
A rule earns its place only if the model needs it and nothing else already
does its job. The original is never edited until a trial shows that the lean
copy still behaves; the user owns the swap.

## 1. Measure

```bash
wc -lc <file>                      # bytes are what the context pays
grep -n '^#' <file>                # the sections to audit
```

Also note what loads with it: `@imports`, a project's `.claude/rules/*.md`
without a `paths:` field (those load every session too), and an enclosing
CLAUDE.md in a parent directory.

## 2. Find what already does each rule's job

A rule is cut only when you can name what replaces it. Look before you judge:

```bash
CFG="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
jq '.hooks' "$CFG/settings.json" .claude/settings.json 2>/dev/null   # hooks that enforce or print
grep -rl "$(basename <file>)" "$CFG/plugins/cache" 2>/dev/null       # plugins that WRITE into the file
```

A plugin hook that injects a block (between `<!-- x-start -->` markers, say)
puts it back at the next session start: removing it from the file does not
stick. Name the plugin and the choice (disable it, or accept the drift) to the
user; never patch a plugin's cache.

## 3. Audit: one verdict per rule

| Verdict | When | Evidence it needs |
|---|---|---|
| **keep** | the model needs it and nothing else holds it | — |
| **compress** | the rule is needed, the words are not | the meaning survives in fewer words |
| **move** | it matters only for some files or some tasks | path-scoped `.claude/rules/<x>.md` (`paths:` frontmatter), a skill, or a command |
| **cut** | a hook, script, tool description or another file already does it, or the user says they never use it | the name of that replacement, or the user's words |

Report the audit as a table, and list separately every detail a compression
dropped. The user reads the dropped details, not the kept ones; flag the
weakest cuts (a detail no script enforces) and offer each back.

A prose rule becomes a hook only on evidence that it broke while it was in
place: commits or transcripts dated after the rule landed (e.g. `git log
--since=<rule date> --author=<me> --grep=<the forbidden thing>`). Zero breaks
means the prose works; a hook would only add upkeep.

## 4. Write the lean copy beside the original

`<name>-lean.md` next to the file, or in the repo's config folder if the file
is synced from one. Do not edit the original. Report bytes before and after.
User feedback on single rules ("I never use this") applies to BOTH files, so
the original stays in step until the swap.

## 5. Trial it without swapping

`trial.sh` (in this skill's directory) runs one headless session per probe in
a throwaway sandbox, with the lean file standing in for the real one, and has
Haiku judge the full transcript (what it said plus every tool call):

```bash
SKILL_DIR=<this skill's directory>
bash "$SKILL_DIR/trial.sh" --lean ~/.claude/CLAUDE-lean.md --probes probes.tsv --control
bash "$SKILL_DIR/trial.sh" --lean CLAUDE-lean.md --probes probes.tsv \
     --scope project --repo . --target CLAUDE.md --control
```

Write one probe per rule you kept or compressed (`example-probes.tsv` is a
worked set): `name <TAB> prompt <TAB> expected behaviour <TAB> check <TAB> setup`.
Include a **negative** probe for every trigger rule (a trivial request must
NOT set it off), named `neg-<x>`: it passes with no instructions by design,
so `--control` skips it. Give anything with a visible result (a commit
message, a file) a `check` command. `--control` reruns each probe with an empty file;
a probe that passes either way is reported `PROVES-NOTHING`: it measures the
model's default, not the rule, so rewrite or drop it.

Before it runs, check the load and say the cost: each probe is one full
session plus a Haiku call, twice with `--control`. Serial by design.

Limits to tell the user:
- `--scope user` appends the file to the system prompt, which the model may
  weigh a little more than a CLAUDE.md. Behaviour that passes here is likely,
  not certain, to pass live.
- Interactive-only behaviour (waiting for a reply, a status line) cannot be
  probed headless; a probe sees one turn.
- To try it live instead: `claude --setting-sources project,local --settings
  "$CFG/settings.json" --append-system-prompt-file <lean>` (user scope only).

## 6. A second opinion, fact-checked

If the user brings another model's critique, take each point on its facts:
run the command it is about, check the decision it would reverse, and give a
verdict per point (accept / reject / right symptom, wrong cause). Never adopt a
rewrite wholesale: it tends to drop exact lists ("etc."), soften or harden
verbs, and move thresholds without a reason.

## 7. Swap on the user's word

Only after the trial passes and the user says go: replace the original with
the lean copy, delete the `-lean` file, and commit it where the repo wants
commits. If the file is deployed from a synced copy, the deploy step is the
user's. Report: bytes before → after, probes passed, dropped details, and any
plugin that will re-insert a block.
