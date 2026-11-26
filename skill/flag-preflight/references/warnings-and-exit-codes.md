# Warning codes

`X` = block (plan exits 3), `!` = warn (exit 2), info = shown only in `--json`.

| Code | Level | Meaning | Tell the user |
|---|---|---|---|
| `EXPERIMENT_ACTIVE` | block | The flag has a running experiment (env doc `metadata.experiment` or the experiments API) | Changing it contaminates the experiment. Stop; it needs the experiment owner's decision. Override only with the user's own written reason. |
| `NO_EFFECT` | warn | 0 identities change. Reason given: `loses on priority — <segment> (priority p) catches n/m` (a higher-priority override wins for everyone targeted), `already in the requested state`, or `no sampled identity is in <segment>` | Applying would change nothing for users; usually the wrong lever. Suggest the override that actually wins. |
| `RISK_PRODUCTION` | warn | Environment name contains "prod" | Requires the user to type the env name to apply. |
| `RISK_LARGE_BLAST` | warn | ≥ `--risk-pct` (50%) of identities change, or a rollout ≥ that percentage | State the share; suggest a rollout first. |
| `PROJECT_SCOPED_WRITE` | warn | `rollout:` creates a project-level segment: it shows up in every environment's document (no effect without an override) and invalidates their open plans | Mention it; it is expected. |
| `ROLLBACK_LIMITED` | warn | Current value is null; the CLI cannot set null back | Rollback of the value must be done in the dashboard. |
| `CHANGE_REQUESTS` | warn | Environment requires change-request approvals | Apply may only open a change request. |
| `APPROXIMATE_UNTIL_SEGMENT_EXISTS` | info | Rollout counts are faithful; *which* identities fall in the split is fixed only once the segment has its real id | Stage 1 of apply prints the precise number. |
| `EXPERIMENTS_API_UNAVAILABLE` | info | Experiments API not reachable; only doc metadata was checked | – |

# Exit codes

| Command | 0 | 1 | 2 | 3 |
|---|---|---|---|---|
| `plan` | safe | refused / error | warnings | blocked |
| `apply` | applied, post-apply doc evaluates as predicted | refused (confirm mismatch, stale plan, blocked, already applied) | stage 1 of a rollout done, or post-apply doc disagrees with prediction | – |
| `verify` | all sampled identities match remote evaluation | refused | mismatches | – |
| `rollback` | restored, doc identical to pre-plan, remote sample matches | refused (stale, nothing to roll back) | restored with differences | – |

# Files

- `.preflight/plans/<plan_id>.json` — spec, mutation, doc hash + `updated_at`,
  live feature version, pre-change flag state, simulation, 30-identity sample,
  warnings, history. `<plan_id>.before.json` etc. are document snapshots
  without `api_key`.
- `.preflight/audit.log` — append-only JSON lines (who, when, action, plan,
  result, writes); each line carries the sha256 of the previous one.
