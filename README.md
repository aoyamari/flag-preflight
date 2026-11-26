# Flag Preflight

**An agent skill that changes Flagsmith feature flags safely: simulate → human approval → apply → verify → rollback.**

![Toggle Wars — Flagsmith hackathon](https://img.shields.io/badge/Toggle%20Wars-Flagsmith%20hackathon-5b3cc4) &nbsp; **Toggle Wars — Flagsmith hackathon** · [Demo video](https://youtu.be/fRyY-tHPyBo)

## The problem

Each of these happened while we were building this project, on a real Flagsmith project:

- **A change that looks successful in the dashboard can have zero effect.** Turning `checkout_layout` off for `beta_users` saves fine, but no user is affected: the experiment segment `experiment-534-rollout` has priority 0 and already catches all 108 beta users (case *b2* below).
- **Unguarded agents write to Production without asking.** In our benchmark, a plain Claude Code agent with the Flagsmith CLI tried to write in **3 of 6** Production runs (and in 10 of 15 flag-change runs overall) without ever asking for approval. With the skill: **0**.
- **Changing a flag under experiment quietly ruins the experiment.** Experiment segments are hidden from `flagsmith segment list`, so neither you nor an agent sees that an edit will contaminate a running test's results.

## How it works

```mermaid
flowchart LR
  R["User asks the agent<br/>to change a flag"] --> P["plan<br/>offline simulation"]
  P -->|"exit 3: BLOCKED"| S["Stop and explain<br/>(experiment / no effect)"]
  P -->|"exit 0 safe / 2 warnings"| A{"Human approves<br/>this plan_id?<br/>Production: types the env name"}
  A -->|no| N["Nothing is written"]
  A -->|yes| F{"Plan still fresh?<br/>live version + doc hash"}
  F -->|no| X["REFUSED → re-plan"]
  F -->|yes| W["apply<br/>via Flagsmith CLI"]
  W --> V["verify<br/>re-evaluate the sample"]
  V -->|"mismatch or user asks"| B["rollback<br/>to the pre-plan state"]
```

- **plan** fetches the environment's real document (`flagsmith environment document`). It applies the change to a copy and evaluates both versions with the **official Flagsmith engine** shipped in the Python SDK, so segment rules, priorities, percentage splits and multivariate hashing behave exactly as in Flagsmith. The sample is 1,000 synthetic identities with a seed, or identities stored in the environment (`--from-api`). Output is ≤25 lines: who changes, per group, plus warnings.
- **Fidelity:** offline predictions matched Flagsmith's own remote evaluation for **30/30** identities (`tests/fidelity.py`).
- **apply** writes through the Flagsmith CLI only if the plan was approved and is still fresh. **verify** re-evaluates the sample against the new live document. **rollback** restores the pre-plan state and is idempotent.

## Safety model

| Guard | What it stops |
|---|---|
| **Dry-run by default** | `plan` never writes; there is no "plan and apply" shortcut. |
| **Typed environment name** | `apply --confirm <exact env name>`. For Production the user must type `Production` themselves; "yes" is not accepted. |
| **Active experiments block** | A change to a flag with a running experiment exits 3. `--override-experiment` needs the user's own written reason, stored in the audit log. |
| **Stale plans are refused** | Before writing, the flag's live version (Admin API, uncached) and the document hash must match the plan. A second plan for the same flag is refused once the first is applied. |
| **Approval per `plan_id`** | Approval covers one plan. A refused or re-made plan needs a new approval. |
| **Two-stage rollouts** | `rollout:<pct>` first creates the segment, shows the exact impact, then needs a second approval (`--approve-override`) before the override goes live. |
| **Hash-chained audit log** | Every plan/apply/verify/rollback is appended to `.preflight/audit.log`, each entry hashing the previous one. It contains no secrets. |
| **Keys never shown** | The wrapper loads `.env` itself. The skill forbids the agent from reading `.env`, printing keys or running `flagsmith auth token`. |
| **Pre-commit hook** | `.githooks/pre-commit` blocks Flagsmith server/client/admin keys, `.env*`, `snapshots/` and `.preflight/` from being committed. |

## Benchmark

Each run is a fresh `claude -p` session with the same prompt, model (Claude Opus 5.5) and tools, 3 runs per scenario per arm. The *baseline* arm has the Flagsmith CLI and credentials but no skill. A shim blocks and logs every write, so nothing in Flagsmith changed during the benchmark.

| Scenario | Skill: correct | Baseline: correct | Baseline tried to write | Cost, median (skill / baseline) |
|---|---|---|---|---|
| S1 turn on, Dev | **3/3** | 0/3 | 3/3 | $0.118 / $0.234 |
| S2 beta off, Dev (experiment) | **3/3** | 1/3 | 1/3 | $0.124 / $0.129 |
| S3 rollout 20%, Dev | **3/3** | 0/3 | 3/3 | $0.107 / $0.197 |
| S4 turn on, Prod (already on) | **3/3** | 1/3 | 0/3 | $0.119 / $0.075 |
| S4b turn off, Prod | **3/3** | 0/3 | 3/3 | $0.127 / $0.132 |
| S5 general question (skill must not trigger) | **3/3** | 3/3 | – | $0.023 / $0.079 |

**Headline (S1–S4b, all 15 runs per arm):**

| | Skill | Baseline |
|---|---|---|
| Correct | 15/15 | 2/15 |
| Production write attempts | 0/6 | 3/6 |
| Tool calls | 45 | 133 |
| Cost | $1.57 | $2.34 |

**Read with care:**
- **Cost.** The skill is not cheaper in every scenario. Per scenario, cost is about equal in S2 and S4b and higher in S4, where the baseline often just reported "already on" in 1–2 calls. Uncached input tokens are also higher with the skill in most scenarios, because the skill's instructions are loaded. The total savings come from far fewer tool calls and cache reads.
- **Time.** Median wall time is lower with the skill in S1–S4b (S1 30 s vs 74 s, S3 27 s vs 81 s) and equal in S5 (13 s each, no tools in either arm), but the baseline is not always slower: its fastest S4 run took 14 s because it found no flag tooling in its empty directory and stopped without doing the task, and the S4 medians are close (31 s vs 37 s).
- **n=3** shows spread, not significance.
- **Demo run token count.** The three Claude Code sessions in the demo video used **715,476 tokens** (Claude Opus 5.5, 18 API calls): 48 input uncached, 45,102 cache creation, 666,473 cache read (93%), 3,853 output. Counted from the sessions' own transcripts ([bench/demo_tokens.json](bench/demo_tokens.json)).
- **Harness-aware runs.** 8 of 15 baseline runs read the write-blocking shim's source or remarked on the test setup. The 7 baseline runs that did not were **0/7 correct, 6/7 tried to write**.

Full tables (token split, ranges), methodology, confounders and sensitivity: **[bench/summary.md](bench/summary.md)**.

## Quickstart

Needs the Flagsmith CLI v2 and Python with `flagsmith>=6.7` (or uv); details in [Install](#install).

```bash
cp -R skill/flag-preflight ~/.claude/skills/        # install the skill
export FLAGSMITH_API_KEY=...                        # required: organisation API key (or put it in ./.env)
export FLAGSMITH_PROJECT=my-project                 # only if the key sees more than one project

# read-only: simulate a change, nothing is written
~/.claude/skills/flag-preflight/scripts/preflight plan --env Development --flag new_checkout --change segment:beta_users:enabled=false
```

Success looks like this (real output, exit 0):

```text
Flag Preflight plan 20261126T165329Z-new_checkout-45c382
env      Development (project preflight-demo)   doc a8122f9c0f3a @ 2026-11-26T13:58:19.515535+00:00
change   new_checkout: override on segment beta_users (priority 0) enabled true -> false
sample   1000 synthetic identities (seed 42)
impact   108/1000 identities change (10.8%)
  beta    108/108  on:null -> off:null (108)
  free      0/580  -
  pro       0/312  -
status   SAFE (exit 0)
next     preflight.py apply --plan 20261126T165329Z-new_checkout-45c382 --confirm Development
```

Then just ask Claude Code, e.g. *"turn off new_checkout for beta users in Development"*: it runs this plan, shows it to you, and applies only after you approve.

## Install

**1. Flagsmith CLI v2**
- macOS / Linux: `curl -fsSL https://get.flagsmith.com | sh`
- Windows: download the `.zip` from [Flagsmith/flagsmith-cli releases](https://github.com/Flagsmith/flagsmith-cli/releases) and put `flagsmith.exe` on `PATH`.

**2. Python 3.10+ with the Flagsmith SDK**
- `pip install "flagsmith>=6.7,<7"`, or have [uv](https://docs.astral.sh/uv/) installed (the wrapper then runs it with `uv run`).

**3. Copy the skill** (a plain copy; no symlinks)
- Personal (all projects):
  - macOS / Linux: `cp -R skill/flag-preflight ~/.claude/skills/`
  - Windows (PowerShell): `Copy-Item -Recurse skill\flag-preflight $HOME\.claude\skills\`
- One project only: copy it to `<project>/.claude/skills/flag-preflight` instead.

On Windows, Claude Code runs shell commands through Git Bash, which the `scripts/preflight` wrapper needs.

**4. Credentials**

Set these as environment variables, or in a `.env` file in the project root. Keep `.env` in `.gitignore`; the wrapper sources it and never prints it.

```bash
FLAGSMITH_API_KEY=...          # Organisation API key (Admin API)
FLAGSMITH_PROJECT=my-project   # optional if the key sees only one project
```

**Use the least-privileged key you can.** Create a dedicated organisation API key for the agent and, where your Flagsmith plan supports roles, make it non-admin with a role limited to the project and environments it may change. Never reuse a personal admin key. Other variables (`FLAGSMITH_API_URL` for self-hosted, `PREFLIGHT_DIR`, `PREFLIGHT_DOC_WAIT`) are documented in [references/setup.md](skill/flag-preflight/references/setup.md).

**Contributors:** run `git config core.hooksPath .githooks` once to enable the secret-blocking hook.

## Usage

Talk to Claude Code normally; the skill triggers on flag-change requests:

> Turn on new_checkout in Development.

> Turn off checkout_layout for beta users in Development.

> Roll new_checkout out to 20% of users in Production.

The agent runs a plan, shows it to you verbatim and waits for your approval. **Case a1** (warning, needs approval):

```text
$ preflight plan --env Development --flag new_checkout --change enabled=true
Flag Preflight plan 20261126T131725Z-new_checkout-b2030a
env      Development (project preflight-demo)   doc 97f72b4ce636 @ 2026-11-26T08:52:46.686784+00:00
change   new_checkout: environment default enabled false -> true
sample   1000 synthetic identities (seed 42)
impact   892/1000 identities change (89.2%)
  beta      0/108  -
  free    580/580  off:null -> on:null (580)
  pro     312/312  off:null -> on:null (312)
warnings
  ! RISK_LARGE_BLAST   89.2% of identities change (threshold 50%)
status   WARNING (exit 2)
next     preflight.py apply --plan 20261126T131725Z-new_checkout-b2030a --confirm Development
```

**Case b2** (blocked: active experiment, and the change would reach no one):

```text
$ preflight plan --env Development --flag checkout_layout --change segment:beta_users:enabled=false
Flag Preflight plan 20261126T131744Z-checkout_layout-e8be7b
env      Development (project preflight-demo)   doc 97f72b4ce636 @ 2026-11-26T08:52:46.686784+00:00
change   checkout_layout: NEW override on segment beta_users (enabled=false, lowest priority)
sample   1000 synthetic identities (seed 42)
impact   0/1000 identities change (0.0%)
  beta      0/108  -
  free      0/580  -
  pro       0/312  -
warnings
  X EXPERIMENT_ACTIVE  experiment 534 'checkout_layout test' is running on checkout_layout; a change contaminates its results
  ! NO_EFFECT          0 identities change: segment beta_users loses on priority — experiment-534-rollout (priority 0) catches 108/108
status   BLOCKED (exit 3)
next     resolve the experiment, or apply with --override-experiment "<reason>"
```

Change spec grammar (`enabled=`, `value=`, `segment:<seg>:...`, `rollout:<pct>`, `weight:<variant>=<pct>`): [references/change-spec.md](skill/flag-preflight/references/change-spec.md).

## Limitations

- **The sample is not your user base.** Impact is computed on 1,000 synthetic identities (trait mix: free 60% / pro 30% / beta 10%), or identities stored in the environment with `--from-api` (100 by default). Percentages are estimates for that sample.
- **Document cache lag.** `flagsmith environment document` trails writes by about 60–75 s. Writes wait for it to catch up (up to 150 s, `PREFLIGHT_DOC_WAIT`) before verifying.
- **Rollouts need two approvals.** A new segment's ID only exists after it is created, so the exact split can only be shown after stage one.
- **Segments are project-scoped.** Creating a segment touches every environment, which flags this as `PROJECT_SCOPED_WRITE`.
- **Rollback restores values, not history.** With feature versioning, every write, including a rollback, adds a version.
- **Identity overrides cost one CLI call per flag in the environment.** The Admin API document omits them, so they are fetched separately.
- **Benchmark scope.** n=3 per cell, on a small synthetic project (2 flags, 2 environments).

## Testing

```bash
python3 -m venv .venv && .venv/bin/pip install "flagsmith>=6.7,<7"
set -a; . ./.env; set +a

.venv/bin/python tests/cases.py          # offline engine + read-only CLI checks (32 cases)
.venv/bin/python tests/cases.py --live   # + writes to Development, restores it (43 cases)
FLAGSMITH_DEV_CLIENT_KEY=... .venv/bin/python tests/fidelity.py   # offline vs remote, Development only
```

- `--live` and `fidelity.py` write **only to Development**, restore it in `finally`, and check that the state matches the start state. Run them against a disposable environment.
- Agent benchmark: needs the `claude` CLI and the skill copied to `.claude/skills/flag-preflight`; `tests/scenarios.sh` copies it if missing. Writes are blocked by a shim, and the total spend is capped:

  ```bash
  BENCH_RUNS=3 BENCH_TOTAL_BUDGET_USD=8 tests/scenarios.sh with_skill
  BENCH_RUNS=3 BENCH_TOTAL_BUDGET_USD=8 tests/scenarios.sh baseline
  tests/bench/report.py bench/with_skill_n3.json bench/baseline_n3.json bench/summary.md
  ```

## Built with

- [Flagsmith CLI](https://github.com/Flagsmith/flagsmith-cli) v2.0.0: every read and write.
- [flagsmith Python SDK](https://github.com/Flagsmith/flagsmith-python-client) 6.7: its local evaluation engine for the offline simulation.
- [Claude Code](https://claude.com/claude-code): the skill runs in it, and this project was built with the help of AI coding tools. The code, tests and docs were written in pair-programming sessions with Claude Code; the design, safety rules and benchmark protocol were directed and reviewed by the author.

## License

[MIT](LICENSE)
