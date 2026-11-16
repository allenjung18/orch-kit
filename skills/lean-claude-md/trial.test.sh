#!/usr/bin/env bash
# trial.test.sh — trial.sh against a stub claude (no model calls).
# The stub agent says GOOD when its instruction file holds RULE and the prompt
# says "obey", else BAD; a prompt "tool" makes it emit only a Bash call running
# GOOD-cmd. The stub judge (--model haiku) prints PASS when the transcript holds
# GOOD, FAIL otherwise, or nothing at all when it holds SILENT; without the
# grader system prompt it always FAILs (it grades by its own conventions).
#   bash trial.test.sh            → `N passed, M failed`, exit 0 only if M=0
#   bash trial.test.sh --mutants  → each mutant must apply AND turn the suite red
#   TRIAL=<path> bash trial.test.sh → run against a copy (what --mutants does)
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
TRIAL="${TRIAL:-$DIR/trial.sh}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

if [ "${1:-}" = "--mutants" ]; then
  MUTANTS=(
    "M1 judge verdict ignored|s/grep -m1 -E '\^\(PASS\|FAIL\)\\\\b'/sed 's\/^.*\/PASS stub\/'/"
    "M2 control never flags|s/nothing=\\\$\(\(nothing\+1\)\)/:/"
    "M3 check column ignored|s/\! \(cd \"\\\$box\" \&\& bash -c \"\\\$check\"\)/false/"
    "M4 project scope keeps the original file|s/\&\& cp \"\\\$file\" \"\\\$box\/\\\$TARGET\"//"
    "M5 tool calls hidden from the judge|s/elif .type==\"tool_use\" then/elif false then/"
    "M6 user scope drops the lean file|s/--append-system-prompt-file \"\\\$file\"/--append-system-prompt-file \/dev\/null/"
    "M7 missing --lean file accepted|s/^\\[ -f \"\\\$LEAN\" \\] \\|\\|/true ||/"
    "M9 tab-split probe lines (empty columns merge)|s/IFS=\\\$'\\\\037' read -r name/IFS=\\\$'\\t' read -r name/;s/tr '\\\\t' '\\\\037'/cat/"
    "M10 negative probes run the control|s/neg-\\*\\) true/neg-NEVER) true/"
    "M11 judge keeps the default system prompt|s/--system-prompt \"\\\$JUDGE_SYSTEM\"//"
    "M12 verdict punctuation kept|s/ \\| sed -E 's\/\^\(PASS\|FAIL\)\[:\.,\]\?\/\\\\1\/'//"
    "M8 empty probe file reported as a pass|s/\\[ \"\\\$n\" -gt 0 \\] \\|\\|/true ||/"
  )
  killed=0 survived=0
  for m in "${MUTANTS[@]}"; do
    name="${m%%|*}" expr="${m#*|}" copy="$TMP/trial.$RANDOM.sh"
    sed -E "$expr" "$TRIAL" > "$copy"
    if cmp -s "$copy" "$TRIAL"; then echo "  DID NOT APPLY  $name"; survived=$((survived+1)); continue; fi
    if TRIAL="$copy" bash "$0" >/dev/null 2>&1; then echo "  SURVIVED       $name"; survived=$((survived+1))
    else echo "  killed         $name"; killed=$((killed+1)); fi
  done
  echo "MUTANTS: $((killed+survived)) planted, $killed killed"
  [ "$survived" -eq 0 ]; exit
fi

mkdir -p "$TMP/bin"
cat > "$TMP/bin/claude" <<'STUB'
#!/usr/bin/env bash
in="$(cat)"; file="" judge=0 grader=0
while [ $# -gt 0 ]; do
  case "$1" in
    --system-prompt) case "$2" in *grader*) grader=1 ;; esac; shift ;;
    --append-system-prompt-file) file="$2"; shift ;;
    --model) [ "$2" = haiku ] && judge=1; shift ;;
  esac; shift
done
if [ "$judge" = 1 ]; then
  in="${in#*Transcript}"   # grade the transcript, not the expectation text
  # without the grader prompt it grades by its own conventions, so a good run fails
  [ "$grader" = 1 ] || { echo "FAIL default conventions"; exit 0; }
  case "$in" in *SILENT*) echo "I am not sure." ;; *COLON*) echo "PASS: saw good" ;; *GOOD*) echo "PASS saw good" ;; *) echo "FAIL saw bad" ;; esac
  exit 0
fi
[ -z "$file" ] && file="$PWD/CLAUDE.md"
say=BAD
if grep -q RULE "$file" 2>/dev/null; then case "$in" in *obey*) say=GOOD ;; esac; fi
case "$in" in *always*) say=GOOD ;; esac
case "$in" in *SILENT*) say=SILENT ;; esac
[ "$say" = GOOD ] && case "$in" in *COLON*) say="GOOD COLON" ;; esac
case "$in" in
  *tool*) [ "$say" = GOOD ] && printf '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"GOOD-cmd"}}]}}\n'
          [ "$say" = GOOD ] || printf '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"ls"}}]}}\n' ;;
  *) printf '{"type":"assistant","message":{"content":[{"type":"text","text":"%s"}]}}\n' "$say" ;;
esac
printf '{"type":"result","result":"done"}\n'
STUB
chmod +x "$TMP/bin/claude"
export CLAUDE_BIN="$TMP/bin/claude"

printf '# lean\nRULE: obey\n' > "$TMP/lean.md"
printf '# lean without the rule\n' > "$TMP/bare.md"
T=$'\t'
P="$TMP/probes.tsv"

passed=0 failed=0
ok()  { passed=$((passed+1)); }
bad() { failed=$((failed+1)); echo "FAIL: $1"; [ -n "${2:-}" ] && printf '%s\n' "$2" | sed 's/^/    /'; }
run() { out="$(bash "$TRIAL" "$@" --out "$TMP/out.$RANDOM" 2>&1)"; rc=$?; }

# 1. a rule the lean file keeps passes; the same probe without it fails
printf 'p1%sobey me%ssays GOOD\n' "$T" "$T" > "$P"
run --lean "$TMP/lean.md" --probes "$P"
[ $rc = 0 ] && grep -q '^  PASS p1' <<<"$out" && grep -q 'RESULT: 1 probes — 1 pass, 0 fail' <<<"$out" && ok || bad "kept rule passes" "$out"
run --lean "$TMP/bare.md" --probes "$P"
[ $rc = 1 ] && grep -q '^  FAIL p1' <<<"$out" && ok || bad "dropped rule fails" "$out"

# 2. tool calls reach the judge, not just the final text
printf 'p2%sobey, use a tool%sruns GOOD-cmd\n' "$T" "$T" > "$P"
run --lean "$TMP/lean.md" --probes "$P"
[ $rc = 0 ] && ok || bad "tool calls judged" "$out"

# 3. setup runs before the prompt; a failing check fails a judged PASS
printf 'p3%sobey%sGOOD%stest -f seed%stouch seed\np4%sobey%sGOOD%stest -f missing\n' "$T" "$T" "$T" "$T" "$T" "$T" "$T" > "$P"
run --lean "$TMP/lean.md" --probes "$P"
[ $rc = 1 ] && grep -q '^  PASS p3' <<<"$out" && grep -q '^  FAIL p4 .*check failed' <<<"$out" && ok || bad "setup and check" "$out"
# an empty check column keeps setup in its own column (it must run, not be checked)
printf 'p3b%sobey%sGOOD%s%sexit 1\n' "$T" "$T" "$T" "$T" > "$P"
run --lean "$TMP/lean.md" --probes "$P"
[ $rc = 0 ] && ok || bad "empty check column" "$out"

# 4. --control flags a probe that passes with no instructions, not one that needs the rule
printf 'p5%sobey%sGOOD\np6%sobey always%sGOOD\n' "$T" "$T" "$T" "$T" > "$P"
run --lean "$TMP/lean.md" --probes "$P" --control
[ $rc = 1 ] && grep -q 'p6 .*PROVES-NOTHING' <<<"$out" && ! grep -q 'p5 .*PROVES-NOTHING' <<<"$out" \
  && grep -q '1 prove nothing' <<<"$out" && ok || bad "control" "$out"
# a negative probe (neg-*) passes with no instructions by design: never flagged
printf 'neg-p6%sobey always%sGOOD\n' "$T" "$T" > "$P"
run --lean "$TMP/lean.md" --probes "$P" --control
[ $rc = 0 ] && ! grep -q PROVES-NOTHING <<<"$out" && ok || bad "negative probe skips control" "$out"

# 5. project scope: the lean file replaces --target in a clone; the repo is untouched
R="$TMP/repo"; mkdir -p "$R/docs"; git -C "$R" init -q
printf 'original\n' > "$R/docs/AGENTS.md"
git -C "$R" add . && git -C "$R" -c user.name=t -c user.email=t@t commit -qm init
cat > "$TMP/bin/claude.proj" <<'STUB'
#!/usr/bin/env bash
# project scope reads the file at --target; reuse the stub with docs/AGENTS.md as its file
in="$(cat)"; for a in "$@"; do [ "$a" = haiku ] && exec "$(dirname "$0")/claude" "$@" <<<"$in"; done
if grep -q RULE docs/AGENTS.md; then t=GOOD; else t=BAD; fi
printf '{"type":"assistant","message":{"content":[{"type":"text","text":"%s"}]}}\n' "$t"
STUB
chmod +x "$TMP/bin/claude.proj"
printf 'p7%sobey%sGOOD\n' "$T" "$T" > "$P"
out="$(CLAUDE_BIN="$TMP/bin/claude.proj" bash "$TRIAL" --lean "$TMP/lean.md" --probes "$P" --scope project --repo "$R" --target docs/AGENTS.md --out "$TMP/o7" 2>&1)"; rc=$?
[ $rc = 0 ] && grep -qx original "$R/docs/AGENTS.md" && [ -z "$(git -C "$R" status --porcelain)" ] && ok || bad "project scope" "$out"
# the sandbox is a shallow clone: a big repo's history is never copied per probe
git -C "$R" -c user.name=t -c user.email=t@t commit -q --allow-empty -m second
out="$(CLAUDE_BIN="$TMP/bin/claude.proj" bash "$TRIAL" --lean "$TMP/lean.md" --probes "$P" --scope project --repo "$R" --target docs/AGENTS.md --out "$TMP/o7s" --keep 2>&1)"
[ "$(git -C "$TMP/o7s/p7.lean" rev-list --count HEAD 2>/dev/null)" = 1 ] && ok || bad "project sandbox is shallow" "$out"

# a git worktree (its .git is a file) is a repo too
git -C "$R" worktree add -q "$TMP/wt" -b wt 2>/dev/null
out="$(CLAUDE_BIN="$TMP/bin/claude.proj" bash "$TRIAL" --lean "$TMP/lean.md" --probes "$P" --scope project --repo "$TMP/wt" --target docs/AGENTS.md --out "$TMP/o7w" 2>&1)"; rc=$?
[ $rc = 0 ] && grep -qx original "$TMP/wt/docs/AGENTS.md" && ok || bad "project scope from a worktree" "$out"

# 6. a judge reply with no PASS/FAIL line is a FAIL, never a pass
printf 'p8%sobey SILENT%sGOOD\n' "$T" "$T" > "$P"
run --lean "$TMP/lean.md" --probes "$P"
[ $rc = 1 ] && grep -q 'no PASS/FAIL line' <<<"$out" && ok || bad "silent judge" "$out"

# 6b. a verdict with punctuation ("PASS:") prints as a bare PASS in the table
printf 'p9%sobey COLON%sGOOD\n' "$T" "$T" > "$P"
run --lean "$TMP/lean.md" --probes "$P"
[ $rc = 0 ] && grep -q '^  PASS p9 *saw good' <<<"$out" && ok || bad "verdict punctuation" "$out"

# 7. refusals exit 2: no lean file, no probes file, project scope without a repo, empty probes
for args in "--lean $TMP/nope.md --probes $P" "--lean $TMP/lean.md --probes $TMP/nope.tsv" \
            "--lean $TMP/lean.md --probes $P --scope project"; do
  run $args
  [ $rc = 2 ] && grep -q REFUSED <<<"$out" && ok || bad "refusal: $args" "$out"
done
printf '# only a comment\n' > "$P"
run --lean "$TMP/lean.md" --probes "$P"
[ $rc = 2 ] && grep -q 'no probes' <<<"$out" && ok || bad "empty probes refused" "$out"

echo "$passed passed, $failed failed"
[ "$failed" -eq 0 ]
