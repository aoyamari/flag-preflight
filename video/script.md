# Flag Preflight — demo video script

Target length 2:51 (1920×1080, H.264 + AAC). Voice: edge-tts `en-US-AndrewNeural`.
Every terminal frame comes from a real Claude Code session recorded with VHS
(`video/scene3.tape`, `scene4.tape`, `scene5.tape`). Nothing in the terminal is typed
or edited after the fact. Waiting time is either cut out or sped up, and every sped-up
stretch is labelled on screen. One real frame is held (labelled "paused") so it can be read. Card numbers are copied from `bench/summary.md`.

`video/build.py` reads the narration lines (`> `) and durations below. `[t]` starts a line
at second t of its scene (to line up with the footage); the cut list is `video/edl.json`.
Build: `video/build.py`; output `video/demo.mp4` (not committed).

## 1 · Title — 15s

On screen: **Flag Preflight** · "A flag change can look successful and reach 0 users. An agent without guardrails will flip Production." · Toggle Wars — Flagsmith hackathon

> A flag change can look successful, and reach zero users.
> And an AI agent without guardrails will flip Production.
> Flag Preflight is an agent skill that makes Flagsmith changes safe.

## 2 · The problem — 18s

On screen: the three problems from the README.

> We hit all three on a real Flagsmith project.
> An override that saved fine, but lost on priority to an experiment segment.
> An agent without the skill, writing to Production without asking.
> And an edit to a flag under experiment, which quietly ruins the test.

## 3 · Blocked: running experiment, no effect — 26s

Terminal (real, Development, read-only): `turn off checkout_layout for beta users in Development`

> [0.6] In Claude Code, I ask to turn off checkout layout for beta users.
> [5.0] The skill simulates the change offline, on Flagsmith's real environment document.
> [13.3] Blocked. Experiment 534 is running on this flag, and the override would reach no one: the experiment's segment catches all 108 beta users first.
> [21.4] The agent stops and explains.

## 4 · Plan, approve, apply, verify, rollback — 44s

Terminal (real, writes to Development): `turn on new_checkout for everyone in Development` → `yes, apply it` → `roll it back`

> [0.6] Next: turn on new checkout for everyone.
> [8.6] The plan: 892 of 1,000 identities change, with a large blast warning.
> [13.2] Nothing is written until I approve this exact plan.
> [19.6] Apply waits for Flagsmith's cache to catch up. That part is sped up.
> [24.8] Verify re-checks the sample against live Flagsmith: thirty of thirty match.
> [30.4] Then I ask for a rollback.
> [36.6] The environment is back to identical, and the sample matches again.

## 5 · Production needs the typed name — 25s

Terminal (real, Production, read-only): `turn off new_checkout in Production` → `no, cancel`.
The agent's first spec (`off`) is REFUSED, and it corrects it to `enabled=false`; that real frame is held 2.5s ("paused").

> [0.6] Now, Production.
> [4.6] A malformed change spec is refused, and the agent corrects it.
> [11.9] In Production, yes is not enough: the agent asks me to type the environment's name.
> [18.9] I cancel, and nothing is written.

## 6 · Benchmark — 20s

On screen (from `bench/summary.md`, S1–S4b, 3 runs per scenario per arm): correct 15/15 vs 2/15 · Production write attempts 0/6 vs 3/6 · tool calls 45 vs 133 · cost $1.57 vs $2.34 · median time ~164 s vs ~313 s (≈2× faster; sum of S1–S4b medians) · this demo run: 715,476 tokens (`bench/demo_tokens.json`) · n=3, small synthetic project, per-scenario cost sometimes equal or higher.

> We benchmarked the same agent with and without the skill, three runs per scenario.
> Correct: fifteen of fifteen, versus two.
> Production write attempts: zero, versus three of six.
> Fewer tool calls and lower total cost, though not in every scenario. Details are in the repo.

## 7 · Why Flagsmith is the engine — 23s

On screen: environment document + official SDK engine · Flagsmith CLI v2 for all I/O · experiment metadata · v2 feature versioning · github.com/aoyamari/flag-preflight

> Flagsmith is what makes this work.
> The environment document, run through the SDK's own engine, makes the simulation exact.
> The CLI does every read and write.
> Experiment metadata powers the block, and feature versioning catches stale plans and makes rollback safe.
> Flag Preflight. On GitHub, at aoyamari slash flag-preflight.