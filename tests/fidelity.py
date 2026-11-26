"""Fidelity test: offline prediction vs real Flagsmith evaluation.

Development only. Applies `flag enable new_checkout`, evaluates identities
remotely (transient, no --persist), then ALWAYS reverts in `finally`.
"""

import json
import os
import subprocess
import time
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skill" / "flag-preflight" / "scripts"))

from simulate import apply_mutation, evaluate, synthetic_identities

# Development's client-side key comes from the environment, never from the repo.
DEV_KEY = os.environ.get("FLAGSMITH_DEV_CLIENT_KEY") or sys.exit("set FLAGSMITH_DEV_CLIENT_KEY (Development client-side key)")
DEV = ["-p", os.environ.get("FLAGSMITH_PROJECT", "preflight-demo"), "-e", DEV_KEY]
FEATURE = "new_checkout"
MUTATION = {"op": "env_default", "feature": FEATURE, "enabled": True}
PER_PLAN = 10


def cli(*args: str) -> str:
    env = {k: v for k, v in os.environ.items() if k not in ("FLAGSMITH_ENVIRONMENT_KEY", "FLAGSMITH_ENVIRONMENT")}
    return subprocess.run(["flagsmith", *args, *DEV, "--no-input"], check=True, capture_output=True, text=True, env=env).stdout


def remote(identifier: str, plan: str) -> dict:
    flags = json.loads(cli("eval", "--identity", identifier, "--trait", f"plan={plan}", "--json"))["flags"]
    return {k: (v["enabled"], v["value"], v.get("reason")) for k, v in flags.items()}


def env_default_enabled() -> bool:
    return next(f["enabled"] for f in json.loads(cli("flag", "list", "--json")) if f["feature"] == FEATURE)


def compare(pred: dict, ids, label: str) -> dict:
    rows, ok = [], 0
    for identifier, traits in ids:
        r = remote(identifier, traits["plan"])
        p = pred[identifier]
        match = all(p[f] == r[f][:2] for f in p) and set(p) == set(r)
        ok += match
        rows.append({"id": identifier, "plan": traits["plan"], "match": match,
                     **{f: {"pred": p[f], "remote": r[f][:2], "reason": r[f][2]} for f in sorted(p)}})
    print(f"[{label}] {ok}/{len(ids)} match", file=sys.stderr)
    return {"label": label, "matched": ok, "total": len(ids), "rows": rows}


def main() -> None:
    doc = json.loads(Path(sys.argv[1]).read_text())
    pop = synthetic_identities()
    ids = [x for plan in ("free", "pro", "beta") for x in [i for i in pop if i[1]["plan"] == plan][:PER_PLAN]]
    pred_before = evaluate(doc, ids)
    pred_after = evaluate(apply_mutation(doc, MUTATION), ids)

    if doc.get("name") != "Development":
        sys.exit(f"refusing: document is for {doc.get('name')!r}, this test writes to Development only")
    assert env_default_enabled() is False, "precondition: new_checkout must start disabled in Development"
    report = {"before": compare(pred_before, ids, "before (control)")}
    try:
        cli("flag", "enable", FEATURE, "--yes")
        t0 = time.time()
        while not remote(ids[0][0], ids[0][1]["plan"])[FEATURE][0]:  # wait for SDK API to see the change
            if time.time() - t0 > 60:
                raise RuntimeError("change not visible on SDK API after 60s")
            time.sleep(1)
        report["propagation_seconds"] = round(time.time() - t0, 1)
        report["after"] = compare(pred_after, ids, "after (enabled)")
    finally:
        cli("flag", "disable", FEATURE, "--yes")
        report["reverted_env_default_enabled"] = env_default_enabled()
        print(f"[revert] new_checkout env default enabled = {report['reverted_env_default_enabled']}", file=sys.stderr)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
