#!/usr/bin/env python3
"""PreToolUse guard for benchmark sessions (exit 2 = block, message goes to the model).

Blocks: the real flagsmith binary by path (bypassing the write-blocking shim),
direct HTTP clients, reading or printing .env / keys, `flagsmith auth token`.
Each decision is logged to $PREFLIGHT_BENCH_LOG.
"""

import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from redact import redact  # noqa: E402

BASH_RULES = [
    (r"\.local/bin/flagsmith|PREFLIGHT_BENCH_REAL_FLAGSMITH", "call `flagsmith` from PATH"),
    (r"\b(curl|wget|httpie|http|nc|ncat|telnet|openssl\s+s_client)\b", "direct HTTP is not allowed; use `flagsmith api`"),
    (r"\b(requests|urllib|http\.client|httpx|aiohttp|fetch\()", "direct HTTP is not allowed; use `flagsmith api`"),
    (r"\b(cat|head|tail|less|more|grep|rg|awk|sed|xxd|od|strings|base64|cp|mv|scp|jq|sort|tee|nl|vi|vim|nano|python3?|node|perl|ruby)\b[^|;&]*\.env\b", "do not read .env"),
    (r"(echo|printf|print)[^|;&]*\$\{?FLAGSMITH_\w*(KEY|TOKEN)", "do not print credentials"),
    (r"\bprintenv\b|(^|[;&|]\s*)env\s*($|[|;&])|(^|[;&|]\s*)set\s*($|[|;&])|\bexport\s+-p\b|\bdeclare\s+-x\b|\bcompgen\s+-e\b", "do not dump the environment"),
    (r"\bauth\s+token\b", "do not print the admin token"),
    (r"PREFLIGHT_BENCH_CREDS", "do not read the harness credentials"),
]


def main() -> int:
    data = json.load(sys.stdin)
    tool, inp = data.get("tool_name", ""), data.get("tool_input", {}) or {}
    reason = None
    if tool == "Bash":
        cmd = inp.get("command", "")
        for rx, why in BASH_RULES:
            if re.search(rx, cmd):
                reason = why
                break
        target = cmd
    else:
        target = " ".join(str(inp.get(k, "")) for k in ("file_path", "path", "pattern", "glob"))
        if re.search(r"(^|/)\.env(\b|$)|PREFLIGHT_BENCH_CREDS", target):
            reason = "do not read .env"
    log = os.environ.get("PREFLIGHT_BENCH_LOG")
    if log and reason:
        with open(log, "a") as f:
            f.write(json.dumps({"ts": time.time(), "scenario": os.environ.get("PREFLIGHT_BENCH_SCENARIO"),
                                "decision": "hook_blocked", "tool": tool, "why": reason,
                                "argv": redact(target)[:300]}) + "\n")
    if reason:
        print(f"Blocked by the benchmark guard: {reason}.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
