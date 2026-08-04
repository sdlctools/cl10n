#!/usr/bin/env bash
# conversation_metrics.sh — print a markdown table of what a Claude Code run
# actually cost: turns, tokens, cache hits, wall clock, tool calls, tool errors.
#
# Written for the demo workflows in .github/workflows/, which pipe its stdout
# straight into a `gh issue comment` / `gh pr comment` body:
#
#   { printf '### 2 · executor — conversation metrics\n\n'
#     bash .github/scripts/conversation_metrics.sh
#   } > "$BODY"
#
# CI-only and POSIX-only *by decision*, which is why it lives here and not
# under plugins/jira-sdlc/skills/_shared/scripts/ — that location carries
# AGENTS.md's win/*.ps1 parity rule, and GitHub runners are Linux. Don't move
# it there without also porting it.
#
# Usage: conversation_metrics.sh [CONVERSATION_ID | transcript.jsonl] ...
#
#   CONVERSATION_ID  a bare session id — located under ~/.claude/projects with
#                    the same `find -mindepth 2 -maxdepth 2` lookup
#                    scripts/make_transcript.sh uses, so the two take the same
#                    argument and you can hand either one the id you already
#                    have:
#                      ./make_transcript.sh        125ff9ca-0abe-…   # the GIF
#                      ./conversation_metrics.sh   125ff9ca-0abe-…   # the cost
#                    That depth pins the lookup to projects/<slug>/<id>.jsonl,
#                    which is also what keeps it from matching a subagent
#                    transcript (those are two levels deeper).
#   transcript.jsonl an explicit path — anything containing a `/` or ending
#                    `.jsonl` is taken as a path, not an id.
#   no argument      glob $HOME/.claude/projects/*/*.jsonl, the same discovery
#                    the GIF steps use, and how CI calls this. Each job runs on
#                    a fresh VM with no shared filesystem, so there is normally
#                    exactly one transcript and it is that job's own skill run.
#
# NEVER fails its caller — this is the one place it diverges from
# make_transcript.sh, which exits 1 on a bad id. A missing transcript, an
# unresolvable id, unusable JSON, or an absent jq degrades to an explanatory
# line and exit 0: metrics are a nicety, and a green run must not redden
# because the nicety was unavailable. Hence `set -uo pipefail` with no `-e`.
set -uo pipefail

# The measurement itself, lifted from the conversation-debugger skill's
# collect_run.sh (its `run metrics` block) where it was measured against real
# transcripts. Two traps it exists to avoid — both survive verbatim:
#  * one API response is split across several assistant lines (one per content
#    block), and every one of them carries the SAME usage object — summing per
#    line overcounts (2.6x on a real run). Dedup by message.id before summing.
#  * content blocks are NOT duplicated across those lines, so tool_use blocks
#    must be counted over every line, not over the deduped set.
#
# Two deliberate departures from collect_run.sh:
#
#  1. No attributionSkill filter, so the scope is the WHOLE conversation. The
#     runner's filesystem isn't shared between jobs, so the only conversation
#     present already IS the executed skill's run; filtering would only add a
#     silent-empty-table failure mode (headless `claude -p` isn't confirmed to
#     set that field).
#
#  2. Subagent spend is reported instead of silently dropped. collect_run.sh
#     splits on `isSidechain` and sums tokens over the main chain only — but
#     subagent turns are NOT written into the session transcript at all. They
#     land in a sibling file, <session>/subagents/agent-<id>.jsonl, one level
#     below the `*/*.jsonl` glob (verified: `isSidechain` is false on all
#     55,996 lines across 426 local transcripts, while 7 of them did spawn
#     subagents). So those files are collected too — see subagent_files() —
#     and their tokens get their own row. Main-chain rows are unchanged; the
#     `isSidechain != true` split still decides which side a line counts on,
#     so an older CLI that inlined sidechains is handled by the same code.
metrics_filter='
  def dedup: group_by(.message.id // "") | map(.[0]);
  def secs: sub("\\.[0-9]+Z$";"Z") | fromdateiso8601;
  def tally: group_by(.name) | map({name: .[0].name, n: length}) | sort_by(-.n)
             | map("\(.name):\(.n)") | join(" ");
  def dur: (./3600|floor) as $h | ((.%3600)/60|floor) as $m | (.%60|floor) as $s
           | if $h > 0 then "\($h)h \($m)m \($s)s"
             elif $m > 0 then "\($m)m \($s)s"
             else "\($s)s" end;
  [ .[] | select(.type=="assistant") ] as $all
  | ($all | map(select(.isSidechain != true))) as $main
  | ($all | map(select(.isSidechain == true))) as $sub
  | ($main | dedup) as $d
  | ($sub | dedup) as $sd
  | [ $main[] | .message.content[]? | select(.type=="tool_use") | {id, name} ] as $calls
  | ([ .[] | select(.type=="user") | .message.content
       | if type=="array" then .[] else empty end
       | select(.type=="tool_result" and .is_error == true) | .tool_use_id ] | unique) as $errids
  | ($calls | map(select(.id as $i | $errids | index($i) != null))) as $errcalls
  | {
      turns:      ($d | length),
      lines:      ($main | length),
      side:       ($sd | length),
      tin:        ($d | map(.message.usage.input_tokens // 0) | add // 0),
      tout:       ($d | map(.message.usage.output_tokens // 0) | add // 0),
      tcr:        ($d | map(.message.usage.cache_read_input_tokens // 0) | add // 0),
      tcw:        ($d | map(.message.usage.cache_creation_input_tokens // 0) | add // 0),
      stin:       ($sd | map(.message.usage.input_tokens // 0) | add // 0),
      stout:      ($sd | map(.message.usage.output_tokens // 0) | add // 0),
      stcr:       ($sd | map(.message.usage.cache_read_input_tokens // 0) | add // 0),
      stcw:       ($sd | map(.message.usage.cache_creation_input_tokens // 0) | add // 0),
      models:     ($d | map(.message.model // empty) | unique | join(" ")),
      first:      ($all | map(.timestamp) | min // ""),
      last:       ($all | map(.timestamp) | max // ""),
      span:       (if ($all|length) > 0
                   then (($all | map(.timestamp) | max | secs) - ($all | map(.timestamp) | min | secs))
                   else 0 end),
      toolcalls:  ($calls | length),
      tools:      ($calls | tally),
      errs:       ($errcalls | length),
      errtools:   ($errcalls | tally)
    }
  | "| metric | value |",
    "|---|---|",
    "| turns | \(.turns) |",
    "| assistant lines | \(.lines) |",
    "| subagent turns | \(.side) |",
    "| tokens in | \(.tin) |",
    "| tokens out | \(.tout) |",
    "| tokens cache read | \(.tcr) |",
    "| tokens cache write | \(.tcw) |",
    "| subagent tokens | \(if .side == 0 then "—"
                            else "in \(.stin) · out \(.stout) · cache read \(.stcr) · cache write \(.stcw)" end) |",
    "| models | \(if .models == "" then "—" else .models end) |",
    "| first timestamp | \(if .first == "" then "—" else .first end) |",
    "| last timestamp | \(if .last == "" then "—" else .last end) |",
    "| wall clock | \(.span | dur) |",
    "| tool calls | \(.toolcalls) |",
    "| tools used | \(if .tools == "" then "—" else .tools end) |",
    "| tool errors | \(.errs) |",
    "| tool errors by tool | \(if .errtools == "" then "—" else .errtools end) |"
'

# The footnote is part of the numbers, not decoration: `turns`, the `tokens *`
# rows and the tool counts are main-chain only, with delegated work reported
# separately as `subagent turns` / `subagent tokens`. Without this line the
# table quietly reads as a whole-session total.
FOOTNOTE='_Turns, tool calls and the `tokens *` rows cover the main chain; subagent spend is the `subagent tokens` row, read from the run’s `subagents/agent-*.jsonl` files. Token totals are deduped by `message.id`: one API response spans several transcript lines that all repeat the same usage object._'

# A run's subagent turns are not in its transcript — they are written to
# <session>/subagents/agent-<id>.jsonl, beside it. `find` rather than a glob
# because a subagent can itself spawn one (meta.json carries a spawnDepth), and
# a missing directory is the normal case, not an error.
subagent_files() {
  [ -d "${1%.jsonl}" ] || return 0
  find "${1%.jsonl}" -type f -name '*.jsonl' -path '*/subagents/*' 2>/dev/null
}

# An argument is a path if it looks like one, else a bare conversation id to
# look up — same lookup as scripts/make_transcript.sh, so both take the id you
# already have. Prints nothing when the id resolves to no session.
resolve_transcript() {
  case "$1" in
    */*|*.jsonl) printf '%s\n' "$1" ;;
    *) find "$HOME/.claude/projects" -mindepth 2 -maxdepth 2 -type f \
         -name "$1.jsonl" -print -quit 2>/dev/null ;;
  esac
}

if ! command -v jq >/dev/null 2>&1; then
  echo "_Conversation metrics unavailable — \`jq\` is not installed on this runner._"
  exit 0
fi

FILES=()
if [ "$#" -gt 0 ]; then
  for arg in "$@"; do
    resolved=$(resolve_transcript "$arg")
    if [ -z "$resolved" ]; then
      # Said in the id's own terms: "missing or empty" below would send someone
      # hunting for a file when what failed was the lookup.
      echo "_No conversation found for id \`$arg\` under \`~/.claude/projects\` — nothing to measure._"
      echo ""
      continue
    fi
    FILES+=("$resolved")
  done
  # Every argument failed to resolve — each already said so; don't follow it
  # with the no-transcripts-on-this-runner line, which would be a different
  # (and wrong) diagnosis.
  [ "${#FILES[@]}" -eq 0 ] && exit 0
else
  shopt -s nullglob
  FILES=("$HOME"/.claude/projects/*/*.jsonl)
  if [ "${#FILES[@]}" -eq 0 ]; then
    echo "_No Claude session transcript found on this runner — nothing to measure._"
    exit 0
  fi
fi

rendered=0
for f in "${FILES[@]}"; do
  if [ ! -s "$f" ]; then
    echo "_Transcript \`$(basename "$f")\` is missing or empty — nothing to measure._"
    echo ""
    continue
  fi
  # jq -s slurps the whole JSONL; a single malformed line aborts it, which is
  # a degrade case rather than a failure. Capture, then check. The run's
  # subagent transcripts go in as extra inputs — every line in them is
  # isSidechain:true, so the filter's existing split puts them on the subagent
  # side without needing to know which file they came from.
  SUBS=()
  while IFS= read -r s; do [ -n "$s" ] && SUBS+=("$s"); done <<EOF
$(subagent_files "$f")
EOF
  TABLE=$(jq -s -r "$metrics_filter" "$f" "${SUBS[@]+"${SUBS[@]}"}" 2>/dev/null)
  if [ -z "$TABLE" ]; then
    echo "_Transcript \`$(basename "$f")\` could not be parsed — no metrics for this run._"
    echo ""
    continue
  fi
  # Normally exactly one transcript per job; name them only when that
  # assumption turns out not to hold, so the usual comment stays clean.
  if [ "${#FILES[@]}" -gt 1 ]; then
    echo "#### \`$(basename "$f")\`"
    echo ""
  fi
  echo "$TABLE"
  echo ""
  rendered=$((rendered + 1))
done

[ "$rendered" -gt 0 ] && echo "$FOOTNOTE"
exit 0
