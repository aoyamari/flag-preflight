"""Mask anything shaped like a Flagsmith credential before it is stored."""

import os
import re

_PATTERNS = [
    re.compile(r"\bser\.[A-Za-z0-9]{20,}"),
    re.compile(r"\b[A-Za-z0-9]{8}\.[A-Za-z0-9]{32}\b"),
    re.compile(r"\b(?=[A-Za-z0-9]{22}\b)(?=[A-Za-z0-9]*[A-Z])(?=[A-Za-z0-9]*[a-z])(?=[A-Za-z0-9]*[0-9])[A-Za-z0-9]{22}\b"),
    re.compile(r"(?i)\b(token|bearer|api-key)\s+[0-9a-f]{40}\b"),
]


def _env_values() -> list[str]:
    path = os.environ.get("PREFLIGHT_BENCH_CREDS")
    vals = []
    if path and os.path.exists(path):
        for line in open(path):
            if "=" in line and not line.lstrip().startswith("#"):
                v = line.split("=", 1)[1].strip().strip("'\"")
                if len(v) >= 12:
                    vals.append(v)
    return vals


_VALUES = _env_values()


def redact(text: str) -> str:
    for v in _VALUES:
        text = text.replace(v, "[REDACTED]")
    for rx in _PATTERNS:
        text = rx.sub("[REDACTED]", text)
    return text
