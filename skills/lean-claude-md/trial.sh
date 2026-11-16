#!/usr/bin/env bash
# trial.sh — try a lean instruction file in headless Claude Code sessions
# without swapping it in. One sandbox per probe; a Haiku judge reads each
# probe's full transcript (text + tool calls) against its expectation.
#
#   trial.sh --lean <file> --probes <tsv> [--scope user|project]
#            [--repo <dir>] [--target <path in repo>] [--model <m>]
#            [--control] [--out <dir>] [--keep]
#
#   --scope user     the lean file replaces ~/.claude/CLAUDE.md: user settings
#                    are skipped (--setting-sources project,local), the lean file
#                    is appended to the system prompt, user hooks are put back
#                    with --settings <config dir>/settings.json
#   --scope project  the lean file replaces <target> (default CLAUDE.md) in a
#                    throwaway clone of --repo; everything else loads as normal
#   --control        also run every probe with an EMPTY instruction file: a
#                    probe that still passes tests nothing (it is the default
#                    behaviour), and is reported as PROVES-NOTHING. Negative
#                    probes (name starts neg-) skip the control run.
#
# probes.tsv, one probe per line, tab-separated, '#' lines skipped:
#   name <TAB> prompt <TAB> expect <TAB> check (optional) <TAB> setup (optional)
#   expect  plain English, judged against the transcript ("names the cost and
#           offers /vorch"; "no plan: just does the rename")
#   check   shell run in the sandbox afterwards; exit 0 = pass (both must pass)
#   setup   shell run in the sandbox before the prompt
#
# Exit 0 all probes pass, 1 any fail (or PROVES-NOTHING under --control), 2 usage.
# CLAUDE_BIN overrides the claude binary (the suite stubs it).
set -u
CLAUDE="${CLAUDE_BIN:-claude}"
LEAN="" PROBES="" SCOPE=user REPO="" TARGET=CLAUDE.md MODEL="" CONTROL=0 OUT="" KEEP=0
usage() { sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --lean) LEAN="${2:-}"; shift 2 ;;
    --probes) PROBES="${2:-}"; shift 2 ;;
    --scope) SCOPE="${2:-}"; shift 2 ;;
    --repo) REPO="${2:-}"; shift 2 ;;
    --target) TARGET="${2:-}"; shift 2 ;;
    --model) MODEL="${2:-}"; shift 2 ;;
    --control) CONTROL=1; shift ;;
    --out) OUT="${2:-}"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    -h|--help) usage ;;
    *) echo "trial.sh: unknown argument '$1'" >&2; exit 2 ;;
  esac
done
[ -f "$LEAN" ] || { echo "trial.sh: REFUSED — --lean file not found: '$LEAN'" >&2; exit 2; }
[ -f "$PROBES" ] || { echo "trial.sh: REFUSED — --probes file not found: '$PROBES'" >&2; exit 2; }
case "$SCOPE" in user|project) ;; *) echo "trial.sh: --scope is user or project" >&2; exit 2 ;; esac
if [ "$SCOPE" = project ]; then
  [ -n "$REPO" ] && git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 || { echo "trial.sh: REFUSED — --scope project needs --repo <git repo>" >&2; exit 2; }
fi
command -v jq >/dev/null || { echo "trial.sh: REFUSED — jq is required" >&2; exit 2; }
LEAN="$(cd "$(dirname "$LEAN")" && pwd)/$(basename "$LEAN")"
OUT="${OUT:-$(mktemp -d "${TMPDIR:-/tmp}/lean-trial.XXXXXX")}"
mkdir -p "$OUT"
CFG="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
EMPTY="$OUT/empty-instructions.md"; : > "$EMPTY"

# make_sandbox <dir> — a fresh repo for one probe
make_sandbox() {
  if [ "$SCOPE" = project ]; then
    git clone -q --depth 1 "file://$(cd "$REPO" && pwd)" "$1" 2>/dev/null || return 1
  else
    mkdir -p "$1" && git -C "$1" init -q && \
      git -C "$1" -c user.name=trial -c user.email=trial@example.invalid commit -q --allow-empty -m init
  fi
}

# run_probe <instructions file> <sandbox> <prompt> <transcript>
run_probe() {
  local file=$1 box=$2 prompt=$3 tx=$4 args=()
  args=(-p --output-format stream-json --verbose --max-turns 20
        --permission-mode acceptEdits --allowedTools=Bash,Read,Edit,Write,Glob,Grep)
  [ -n "$MODEL" ] && args+=(--model "$MODEL")
  if [ "$SCOPE" = user ]; then
    args+=(--setting-sources project,local --append-system-prompt-file "$file")
    [ -f "$CFG/settings.json" ] && args+=(--settings "$CFG/settings.json")
  else
    mkdir -p "$box/$(dirname "$TARGET")" && cp "$file" "$box/$TARGET"
  fi
  # The prompt goes on stdin: --allowedTools is variadic and would eat it.
  (cd "$box" && printf '%s' "$prompt" | "$CLAUDE" "${args[@]}") 2>/dev/null |
    jq -r 'select(.type=="assistant") | .message.content[]? |
      if .type=="text" then "SAY: " + .text
      elif .type=="tool_use" then "TOOL " + .name + ": " +
        ((.input.command // .input.file_path // .input.pattern // (.input|tostring))|tostring)
      else empty end' > "$tx" 2>/dev/null
  [ -s "$tx" ]
}

# The judge's own system prompt: Claude Code's default one carries its own
# conventions (a Co-Authored-By commit line), which it then grades against.
JUDGE_SYSTEM="You are a strict test grader. Judge only against the expected behaviour you are given; you have no conventions of your own."

# judge <expect> <transcript> -> prints "PASS|FAIL <reason>"
judge() {
  local verdict
  verdict="$( { printf 'You grade one test of an AI coding assistant. Expected behaviour:\n%s\n\n' "$1"
      printf 'Transcript (SAY = what it told the user, TOOL = a tool call it made):\n'
      head -c 20000 "$2"
      printf '\n\nReply with ONE line: PASS or FAIL, a space, then the reason in under 20 words.\n'
    } | (cd "$OUT" && "$CLAUDE" -p --model haiku --setting-sources local \
           --system-prompt "$JUDGE_SYSTEM" 2>/dev/null) |
    grep -m1 -E '^(PASS|FAIL)\b' | sed -E 's/^(PASS|FAIL)[:.,]?/\1/')"
  echo "${verdict:-FAIL judge gave no PASS/FAIL line}"
}

pass=0 fail=0 nothing=0 n=0
printf 'lean trial — %s, scope %s, probes %s\n' "$(basename "$LEAN")" "$SCOPE" "$(basename "$PROBES")"
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in ''|'#'*) continue ;; esac
  # A tab is IFS whitespace, so `read` would merge an empty column into the
  # next; split on a non-whitespace separator instead.
  IFS=$'\037' read -r name prompt expect check setup <<<"$(printf '%s' "$line" | tr '\t' '\037')"
  n=$((n+1))
  for variant in lean control; do
    [ "$variant" = control ] && [ "$CONTROL" = 0 ] && continue
    # A negative probe (neg-*) passes with no instructions by design.
    [ "$variant" = control ] && case "$name" in neg-*) true ;; *) false ;; esac && continue
    file=$LEAN; [ "$variant" = control ] && file=$EMPTY
    box="$OUT/$name.$variant"; tx="$OUT/$name.$variant.transcript"
    rm -rf "$box"
    if ! make_sandbox "$box"; then res="FAIL could not build the sandbox"
    else
      [ -n "${setup:-}" ] && (cd "$box" && bash -c "$setup") </dev/null >/dev/null 2>&1
      if ! run_probe "$file" "$box" "$prompt" "$tx"; then res="FAIL no transcript (claude did not run)"
      else
        res="$(judge "$expect" "$tx")"
        if [ "${res%% *}" = PASS ] && [ -n "${check:-}" ] && ! (cd "$box" && bash -c "$check") </dev/null >/dev/null 2>&1; then
          res="FAIL check failed: $check"
        fi
      fi
    fi
    if [ "$variant" = lean ]; then
      lean_res=$res
      case "$res" in PASS*) pass=$((pass+1)) ;; *) fail=$((fail+1)) ;; esac
      printf '  %-4s %-18s %s\n' "${res%% *}" "$name" "${res#* }"
    else
      case "$res" in PASS*)
        case "$lean_res" in PASS*) nothing=$((nothing+1))
          printf '       %-18s PROVES-NOTHING — passes with no instructions too\n' "$name" ;; esac ;;
      esac
    fi
  done
done < "$PROBES"

[ "$n" -gt 0 ] || { echo "trial.sh: REFUSED — no probes in $PROBES" >&2; exit 2; }
printf 'RESULT: %d probes — %d pass, %d fail' "$n" "$pass" "$fail"
[ "$CONTROL" = 1 ] && printf ', %d prove nothing' "$nothing"
printf '\ntranscripts: %s\n' "$OUT"
if [ "$KEEP" = 0 ]; then find "$OUT" -mindepth 1 -maxdepth 1 -type d -exec rm -rf {} +; fi
[ "$fail" -eq 0 ] && [ "$nothing" -eq 0 ]
