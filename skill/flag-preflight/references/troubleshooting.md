# Troubleshooting

## "environment document lags the live version; waiting…"

Normal. `flagsmith environment document` is served from a cache that trails
writes by about 60–75 s. Every read after a write waits until the document's
feature states match the flag's live version (Admin API
`/environments/<id>/features/<id>/versions/`, not cached) and its `updated_at`
has moved. Limit: `PREFLIGHT_DOC_WAIT` seconds (default 150). If it times out
the write already happened: run `verify` a minute later rather than applying
again.

## "REFUSED: <env> changed since this plan was made"

The flag has a new live version, or the document hash/`updated_at` moved
(someone else changed something, or a project-level segment was created or
deleted — that touches every environment). Make a new plan and ask for
approval again. Never edit plan files.

## "REFUSED: … has a new live version …" right after your own apply

You are applying a second plan made before the first was applied. That is the
guard working; re-plan.

## Rollback says "already in the pre-plan state; nothing to write"

Someone (or another plan's rollback) already restored it; the plan is marked
rolled back without writing.

## `segment create` → 500 Internal Server Error

The Admin API rejects a PERCENTAGE_SPLIT condition with an empty `property`.
preflight sends `$.identity.key`, which is accepted. If it still fails, check
`flagsmith api "api/v1/projects/<id>/segments/?include_feature_specific=true"`
for a half-created segment before retrying.

## Flag changes add versions

With v2 feature versioning every write (including a rollback) publishes a new
version. Rollback restores values; it cannot remove history entries.

## Predictions and `api_key`

With composite-key hashing the identity hash key is `<api_key>_<identifier>`,
so splits and variant allocation depend on the document's `api_key`. Never
simulate on a document with the key removed or replaced; plan snapshots on
disk have it stripped and are used only for diffs.

## Experiment segments are invisible to `segment list`

Flagsmith hides experiment segments (e.g. `experiment-534-rollout`). They are in
the environment document and plans report them.

## Identity overrides and the document

`flagsmith environment document` (Admin API) returns `identity_overrides: []` even
when overrides exist; only the SDK document (server key) carries them. preflight
rebuilds them from `flagsmith flag list --feature <f> --identities` (one call per
flag, not cached), so overridden identities simulate as Flagsmith evaluates them
and a new or removed identity override invalidates open plans.
