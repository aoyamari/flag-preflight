#!/usr/bin/env python3
"""Token usage of the Claude Code sessions recorded for the demo (scenes 3, 4, 5).

Reads the real session transcripts named in video/raw/scene<N>.session (not committed:
they can contain environment data) and writes bench/demo_tokens.json. Each API response
can span several transcript lines with the same message id; it is counted once.

usage: video/tokens.py
"""

import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEYS = ["input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"]


def scene_usage(n: int) -> dict:
    path = Path((ROOT / f"video/raw/scene{n}.session").read_text().strip())
    calls = {}
    for line in path.open():
        e = json.loads(line)
        m = e.get("message")
        if e.get("type") == "assistant" and isinstance(m, dict) and "usage" in m:
            calls[m.get("id") or e["uuid"]] = (m.get("model"), m["usage"])
    tot = Counter()
    for _, u in calls.values():
        for k in KEYS:
            tot[k] += u.get(k) or 0
    return {"scene": n, "session": path.stem, "api_calls": len(calls),
            "models": sorted({model for model, _ in calls.values()}),
            **{k: tot[k] for k in KEYS}, "total": sum(tot[k] for k in KEYS)}


def main() -> None:
    scenes = [scene_usage(n) for n in (3, 4, 5)]
    total = {k: sum(s[k] for s in scenes) for k in KEYS + ["total", "api_calls"]}
    total["models"] = sorted({m for s in scenes for m in s["models"]})
    out = {"note": "Claude Code sessions recorded with VHS for video/demo.mp4 (scenes 3-5); "
                   "summed per API response from the session transcripts' usage fields. "
                   "Excludes any calls Claude Code makes outside the transcript.",
           "scenes": scenes, "total": total}
    (ROOT / "bench/demo_tokens.json").write_text(json.dumps(out, indent=1) + "\n")
    for s in scenes + [dict(total, scene="all")]:
        print(f"scene {s['scene']}: " + ", ".join(f"{k}={s[k]:,}" for k in KEYS + ["total"]) + f", models={s['models']}")


if __name__ == "__main__":
    main()
