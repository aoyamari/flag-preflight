# `--change` grammar

One change per plan. `<flag>` is given with `--flag`; segments by name or id.

| Spec | Meaning | Applied with |
|---|---|---|
| `enabled=true` / `enabled=false` | environment default on/off | `flag update <f> --enable/--disable` |
| `value=<v>` | environment default value | `flag update <f> --value <v> --type …` |
| `segment:<seg>:enabled=<bool>` | add or change `<seg>`'s override | `flag update <f> --segment <id> …` |
| `segment:<seg>:value=<v>` | value on `<seg>`'s override | same |
| `segment:<seg>:weight:<k>=<pct>[,<k>=<pct>]` | variant weights on `<seg>`'s override | `flag update <f> --segment <id> --weight …` |
| `rollout:<pct>` | new segment `preflight-<f>-<pct>pct` (PERCENTAGE_SPLIT) + override enabled | `segment create` then (2nd approval) `flag update --segment` |
| `weight:<k>=<pct>[,<k>=<pct>]` | variant weights on the environment default | `flag update <f> --weight …` |

Values: `true`/`false` → boolean, integers → integer, anything else → string.
Variant keys are the multivariate option keys (`flagsmith feature get <f>`).
A new segment override goes to the lowest priority (evaluated last).

## Mapping user requests

| User says | Spec |
|---|---|
| "turn on X for everyone" | `--flag X --change enabled=true` |
| "turn off X for beta users" | `--flag X --change segment:beta_users:enabled=false` |
| "roll X out to 20%" | `--flag X --change rollout:20` |
| "send 70% to variant compact" | `--flag X --change weight:compact=70` (or the segment form if a segment override carries the weights) |
| "set X to v2" | `--flag X --change value=v2` |

If the request names a segment that `plan` cannot find, it refuses with
`segment '<name>' not found`; list segments with `flagsmith segment list` and
ask the user which one they mean. Experiment segments are hidden from that
list but appear in plan warnings.

## Population

Default: 1000 synthetic identities, seed 42, trait `plan` ∈ free 60% / pro 30% /
beta 10%. `--identities N` resizes it; `--from-api` uses identities stored in
the environment (default 100) with their real traits. `--risk-pct` (default 50)
sets the blast-radius warning threshold.
