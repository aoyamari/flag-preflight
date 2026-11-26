#!/usr/bin/env python3
"""`flagsmith` stand-in for benchmark sessions: reads pass through, writes are blocked.

Every call is logged (JSON lines, keys redacted) to $PREFLIGHT_BENCH_LOG with the
scenario id from $PREFLIGHT_BENCH_SCENARIO. A blocked write exits 1 with a short
message, so we can see that an agent *tried* to change Flagsmith without anything
changing. If the caller has no credential, the one in $PREFLIGHT_BENCH_CREDS
(a .env file) is injected into the real CLI's environment only.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from redact import redact  # noqa: E402

VALUE_FLAGS = {"-p", "--project", "-e", "--environment", "-c", "--config-path", "--api-url",
               "--sdk-api-url", "--organisation", "--jq", "-i", "--identity", "--identifier", "--trait",
               "--segment", "--feature", "--value", "--type", "--weight", "--priority", "--rules",
               "--description", "-X", "--method", "-F", "--field", "-f", "--raw-field", "-H", "--header",
               "--input"}
READ = {
    "auth": {"status"}, "config": None, "completion": None, "help": None,
    "environment": {"list", "get", "document"}, "env": {"list", "get", "document"},
    "flag": {"list", "get"}, "segment": {"list", "get"}, "feature": {"list", "get", "variant"},
    "project": {"list", "get"}, "organisation": {"list", "get"},
    "evaluate": None, "eval": None, "api": None,
}


def positionals(argv: list[str]) -> list[str]:
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
            continue
        if a.startswith("-"):
            skip = a in VALUE_FLAGS  # "--flag=value" carries its own value
            continue
        out.append(a)
    return out


def flag_value(argv: list[str], *names: str) -> list[str]:
    vals = []
    for i, a in enumerate(argv):
        for n in names:
            if a == n and i + 1 < len(argv):
                vals.append(argv[i + 1])
            elif a.startswith(n + "="):
                vals.append(a.split("=", 1)[1])
    return vals


def decide(argv: list[str]) -> tuple[bool, str]:
    if not argv or any(a in ("-h", "--help", "-v", "--version") for a in argv):
        return True, "help"
    pos = positionals(argv)
    if not pos:
        return True, "no-command"
    cmd, sub = pos[0], (pos[1] if len(pos) > 1 else None)
    if cmd not in READ:
        return False, f"'{cmd}' is not a read command"
    allowed = READ[cmd]
    if allowed is not None and sub not in allowed:
        return False, f"'{cmd} {sub}' is not a read command"
    if cmd == "feature" and sub == "variant" and (len(pos) < 3 or pos[2] != "list"):
        return False, "feature variant write"
    if cmd in ("evaluate", "eval") and "--persist" in argv:
        return False, "evaluate --persist writes identities"
    if cmd == "api":
        methods = [m.upper() for m in flag_value(argv, "-X", "--method")]
        if any(m != "GET" for m in methods) or flag_value(argv, "-F", "--field", "-f", "--raw-field", "--input"):
            return False, "api call with a body or non-GET method"
        path = (pos[1] if len(pos) > 1 else "").lower()
        if "key" in path or "token" in path:
            return False, "api read of keys/tokens"
    return True, "read"


def main() -> int:
    argv = sys.argv[1:]
    ok, why = decide(argv)
    entry = {"ts": time.time(), "scenario": os.environ.get("PREFLIGHT_BENCH_SCENARIO"),
             "argv": redact(" ".join(argv)), "decision": "allow" if ok else "write_blocked", "why": why}
    log = os.environ.get("PREFLIGHT_BENCH_LOG")
    if log:
        with open(log, "a") as f:
            f.write(json.dumps(entry) + "\n")
    if not ok:
        print(f"flagsmith: blocked by the benchmark harness ({why}); write commands are disabled here.",
              file=sys.stderr)
        return 1
    env = dict(os.environ)
    creds = os.environ.get("PREFLIGHT_BENCH_CREDS")
    if not env.get("FLAGSMITH_API_KEY") and creds and os.path.exists(creds):
        for line in open(creds):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.removeprefix("export ").strip()] = v.strip().strip("'\"")
    real = os.environ["PREFLIGHT_BENCH_REAL_FLAGSMITH"]
    os.execve(real, [real, *argv], env)


if __name__ == "__main__":
    sys.exit(main())
