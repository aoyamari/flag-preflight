# Setup

## Requirements

- Flagsmith CLI v2 on `PATH` (the wrapper adds `~/.local/bin`):
  `curl -fsSL https://get.flagsmith.com | sh`
- Python 3.10+ with the `flagsmith` SDK. The wrapper picks, in order:
  `$PREFLIGHT_PYTHON`, `./.venv/bin/python`, `python3`, or `uv run --with flagsmith`.

## Credentials (never print them)

The wrapper sources `./.env` (if `FLAGSMITH_API_KEY` is not already set) with
`set -a; . ./.env; set +a`. Expected variables:

| Variable | Purpose |
|---|---|
| `FLAGSMITH_API_KEY` | Admin credential (organisation API key). Reads any environment's document by name; no per-environment server key needed. |
| `FLAGSMITH_ENVIRONMENT_KEY` | Optional. Ignored by preflight: every CLI call selects the environment explicitly, so a server key cannot pin commands to the wrong environment. |
| `FLAGSMITH_PROJECT` | Optional. Project name or id; otherwise `--project`, otherwise the only project the credential can see. |
| `FLAGSMITH_API_URL` | Optional, self-hosted instances. |
| `PREFLIGHT_DIR` | Optional. State directory (default `./.preflight`). |
| `PREFLIGHT_DOC_WAIT` | Optional. Seconds to wait for the cached document (default 150). |

`flagsmith login` (browser) also works; the wrapper then finds no key in the
environment and the CLI uses its stored session.

## Repository hygiene

`.gitignore` must contain `.env`, `snapshots/`, `.preflight/`. Enable the
secret-blocking pre-commit hook once per clone:

```bash
git config core.hooksPath .githooks
```

## Installing the skill in a project

```bash
mkdir -p .claude/skills
ln -s ../../skill/flag-preflight .claude/skills/flag-preflight
```
