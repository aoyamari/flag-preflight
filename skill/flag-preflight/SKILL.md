---
name: flag-preflight
description: Safely change Flagsmith feature flags with a simulate → approve → apply → verify → rollback workflow on the Flagsmith CLI. Use when the user wants to turn a Flagsmith flag on or off, change a flag's value, add or change a segment override, roll a flag out to a percentage of users, or re-weight multivariate variants in any environment (Development, Staging, Production…), or asks who a flag change would affect. Do NOT use for general questions about feature flags, for only reading current flag state (use `flagsmith flag list/get`), for creating or deleting features, environments or projects, or for SDK integration code.
---

# Flag Preflight

Simulates a flag change offline against the environment's real document
(official Flagsmith engine, synthetic identities), reports who changes and
what is risky, and applies only an approved, still-fresh plan.

Run everything through the wrapper from the project root (it loads `.env`
itself and never prints keys):

```bash
<skill-dir>/scripts/preflight <command> ...
```

`<skill-dir>` is this skill's base directory. Commands that write wait for
Flagsmith's cached document to catch up (up to ~150 s): give `apply` and
`rollback` a Bash timeout of 300000 ms.

## Approval protocol (mandatory)

**a. Plan first, always.** Never change a flag any other way — no direct
`flagsmith flag enable/update/delete`, `segment create/delete`, or
`flagsmith api` writes.

```bash
<skill-dir>/scripts/preflight plan --env <Env> --flag <flag> --change <spec>
```

Show the plan output to the user **verbatim** in a code block, then one or
two sentences of interpretation. Spec grammar: `references/change-spec.md`.

**b. Exit 3 = BLOCKED → stop.** Explain the warnings in plain words (an
active experiment would be contaminated; a `NO_EFFECT` line means the change
would not reach anyone and why). Do **not** pass `--override-experiment`
unless the user has written the reason themselves in this conversation; then
pass their words verbatim.

**c. Apply only after explicit approval** of that specific plan in this
conversation ("yes, apply it" or equivalent, after they saw the plan).

- Exit 2 (warnings): name each warning when asking for approval.
- **Production** (or any env whose name suggests live traffic): the user must
  type the environment's exact name themselves. Do not accept "yes".
- Approval covers one plan_id. A refused or re-made plan needs new approval.
- Never approve on the user's behalf, never infer approval from the request.

```bash
<skill-dir>/scripts/preflight apply --plan <plan_id> --confirm <exact env name>
```

Rollouts (`rollout:<pct>`) are two-stage: the first apply creates the segment
without an override and prints the precise impact. Show it and get a
**second** explicit approval before:

```bash
<skill-dir>/scripts/preflight apply --plan <plan_id> --confirm <Env> --approve-override
```

**d. After every apply, verify; then offer rollback with the plan_id.**

```bash
<skill-dir>/scripts/preflight verify --plan <plan_id>
<skill-dir>/scripts/preflight rollback --plan <plan_id>     # only if the user wants it
```

Report `X/N` from verify. Any mismatch: say so and offer rollback.

**e. Never read, print or copy `.env` or any key**, and never run
`flagsmith auth token`. The wrapper handles credentials.

## Exit codes

| Command | 0 | 2 | 3 | 1 |
|---|---|---|---|---|
| plan | safe | warnings | **blocked** | refused/error |
| apply / verify / rollback | done | done with warnings or mismatches | – | refused/error |

Warning codes and what to tell the user: `references/warnings-and-exit-codes.md`.

## Keeping it cheap

- Default output is ≤25 lines and is what the user should see. Use `--json`
  only when you must extract a field programmatically.
- Do not open the scripts or references unless a command fails or a spec is
  unclear. One command per protocol step is the normal path.
- Do not re-plan to "double-check"; plans are deterministic.

## When something fails

`REFUSED: ... changed since this plan was made` → make a new plan and ask for
approval again. Waiting messages about the document lagging are normal.
Other failures, setup and environment variables:
`references/troubleshooting.md`, `references/setup.md`.
