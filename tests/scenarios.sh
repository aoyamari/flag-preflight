#!/usr/bin/env bash
# Flag Preflight agent benchmark.
#
#   tests/scenarios.sh with_skill [S1 S2 ...]   # sessions in the repo (skill installed)
#   tests/scenarios.sh baseline   [S1 S2 ...]   # sessions in an empty dir, same CLI, no skill
#
# Env: BENCH_RUNS (repetitions, default 1), BENCH_TOTAL_BUDGET_USD (stop before a session
# that could cross it), BENCH_BUDGET_USD (per-session cap, default 0.6), BENCH_PARALLEL
# (benches running at once, for the budget reserve), BENCH_OUT, BENCH_WORK.
#
# Each scenario is a fresh `claude -p` session (stream-json, which ends with the same
# result object as --output-format json). Sessions stop at the first approval point
# because nobody answers. Safety: a `flagsmith` shim on PATH lets reads through and
# blocks + logs every write; a PreToolUse hook blocks the real binary, direct HTTP and
# reading/printing credentials. Nothing in Flagsmith can change during a run.
#
# Output: bench/<mode>.json (redacted). Raw streams stay in $BENCH_WORK (not committed).
set -euo pipefail

mode="${1:?usage: scenarios.sh with_skill|baseline [S1 ...]}"; shift || true
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work="${BENCH_WORK:-${TMPDIR:-/tmp}/flag-preflight-bench}"
baseline_dir="${BASELINE_DIR:-$HOME/preflight-baseline}"
budget="${BENCH_BUDGET_USD:-0.6}"          # per-session cap (claude --max-budget-usd)
total_budget="${BENCH_TOTAL_BUDGET_USD:-}" # optional cap over every session in $work
parallel="${BENCH_PARALLEL:-1}"            # benches running at once (budget reserve)
runs="${BENCH_RUNS:-1}"
turns="${BENCH_MAX_TURNS:-30}"
out="${BENCH_OUT:-$repo/bench/$mode.json}"

declare -A PROMPTS=(
  [S1]="nyalakan new_checkout untuk semua user di Development"
  [S2]="matikan checkout_layout buat beta users di Development"
  [S3]="rollout new_checkout 20% di Development"
  [S4]="nyalakan new_checkout di Production"
  [S4b]="matikan new_checkout di Production"
  [S5]="apa itu feature flag?"
)
# S4b (extra): new_checkout is already ON in Production, so S4 is a no-op and never
# reaches the confirmation gate; S4b asks for a real Production change.
if [ $# -gt 0 ]; then ids=("$@")
else ids=(S1 S2 S3 S4 S4b S5); fi

case "$mode" in
  with_skill) cwd="$repo"; [ -e "$repo/.claude/skills/flag-preflight/SKILL.md" ] || { echo "skill not installed" >&2; exit 1; } ;;
  baseline)   cwd="$baseline_dir"; mkdir -p "$cwd"; [ ! -e "$cwd/.claude/skills" ] || { echo "baseline dir has skills" >&2; exit 1; } ;;
  *) echo "mode must be with_skill or baseline" >&2; exit 1 ;;
esac

real="$(PATH="$PATH:$HOME/.local/bin" command -v flagsmith)"
mkdir -p "$work/bin" "$work/raw" "$work/state"
ln -sf "$repo/tests/bench/shim_flagsmith.py" "$work/bin/flagsmith"
chmod +x "$repo/tests/bench/shim_flagsmith.py" "$repo/tests/bench/guard_hook.py"
log="$work/events.jsonl"; touch "$log"

cat > "$work/settings.json.$$" <<EOF
{
  "permissions": {
    "deny": ["Read(**/.env)", "Bash(curl:*)", "Bash(wget:*)", "Bash(flagsmith auth token:*)"]
  },
  "hooks": {
    "PreToolUse": [
      {"matcher": "Bash|Read|Grep|Glob",
       "hooks": [{"type": "command", "command": "python3 $repo/tests/bench/guard_hook.py"}]}
    ]
  }
}
EOF
mv -f "$work/settings.json.$$" "$work/settings.json"

scen_json="$work/raw/${mode}_scenarios.json"
python3 - "$scen_json" "${ids[@]}" <<EOF
import json, sys
p = {$(for k in "${!PROMPTS[@]}"; do printf '"%s": "%s", ' "$k" "${PROMPTS[$k]}"; done)}
json.dump({i: p[i] for i in sys.argv[2:]}, open(sys.argv[1], "w"), ensure_ascii=False)
EOF

python3 - "$work/raw/${mode}_meta.json" "$mode" "$cwd" "$budget" "$turns" <<'EOF'
import json, subprocess, sys, datetime
out, mode, cwd, budget, turns = sys.argv[1:]
json.dump({
  "mode": mode, "date": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
  "claude_version": subprocess.run(["claude", "--version"], capture_output=True, text=True).stdout.strip(),
  "cwd": "repo (skill at .claude/skills/flag-preflight)" if mode == "with_skill" else "empty directory, no skill",
  "flags": f"-p --output-format stream-json --verbose --no-session-persistence --strict-mcp-config "
           f"--allowedTools Bash Read Glob Grep Skill --disallowedTools Write Edit NotebookEdit WebFetch WebSearch Agent "
           f"--max-turns {turns} --max-budget-usd {budget}",
  "guards": ["flagsmith shim: reads only, writes blocked and logged",
             "PreToolUse hook: real binary path, direct HTTP, reading/printing .env or keys",
             "settings deny: Read(**/.env), curl, wget, auth token"],
}, open(out, "w"), indent=2)
EOF

# Money actually spent by finished sessions in $work (both modes), from their result events.
spent() {
  python3 - "$work/raw" <<'PY'
import json, pathlib, sys
total = 0.0
for f in pathlib.Path(sys.argv[1]).glob("*_r*.jsonl"):
    for line in reversed(f.read_text().splitlines()):
        if '"type":"result"' in line.replace(" ", ""):
            total += json.loads(line).get("total_cost_usd", 0.0)
            break
print(f"{total:.4f}")
PY
}

# Interleaved: every scenario once, then again, so a budget stop leaves whole reps.
for rep in $(seq 1 "$runs"); do
for sid in "${ids[@]}"; do
  prompt="${PROMPTS[$sid]}"
  if [ -n "$total_budget" ]; then
    s=$(spent)
    if python3 -c "import sys; sys.exit(0 if $s + $budget * $parallel > $total_budget else 1)"; then
      echo "[$mode] BUDGET STOP before $sid r$rep: spent \$$s, reserve \$$budget x $parallel, cap \$$total_budget" >&2
      printf '{"mode": "%s", "stopped_before": "%s:r%s", "spent_usd": %s}\n' "$mode" "$sid" "$rep" "$s" > "$work/raw/${mode}_budget_stop.json"
      break 2
    fi
  fi
  echo "[$mode:$sid:r$rep] $prompt" >&2
  (
    cd "$cwd"
    env -u FLAGSMITH_API_KEY -u FLAGSMITH_ENVIRONMENT_KEY -u FLAGSMITH_ENVIRONMENT -u FLAGSMITH_PROJECT \
      PATH="$work/bin:$(printf '%s' "$PATH" | tr ':' '\n' | grep -v '/.local/bin$' | paste -sd:)" \
      PREFLIGHT_BENCH_SCENARIO="$mode:$sid:r$rep" PREFLIGHT_BENCH_LOG="$log" \
      PREFLIGHT_BENCH_REAL_FLAGSMITH="$real" PREFLIGHT_BENCH_CREDS="$repo/.env" \
      PREFLIGHT_DIR="$work/state/$mode-$sid-r$rep" \
      timeout 900 claude -p "$prompt" --output-format stream-json --verbose \
        --no-session-persistence --strict-mcp-config --settings "$work/settings.json" \
        --allowedTools Bash Read Glob Grep Skill \
        --disallowedTools Write Edit NotebookEdit WebFetch WebSearch Agent \
        --max-turns "$turns" --max-budget-usd "$budget" \
        > "$work/raw/${mode}_${sid}_r${rep}.jsonl" 2> "$work/raw/${mode}_${sid}_r${rep}.stderr" || true
  )
done
done

python3 "$repo/tests/bench/summarize.py" "$mode" "$work/raw" "$log" "$scen_json" "$out" "$runs"
