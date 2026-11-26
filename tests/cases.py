"""Run simulate() over the Spike 2 cases a–d and print a compact summary."""

import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skill" / "flag-preflight" / "scripts"))

from simulate import simulate, synthetic_identities

CASES = {
    "a1 env default: new_checkout enabled=true":
        {"op": "env_default", "feature": "new_checkout", "enabled": True},
    "a2 env default: new_checkout value='v2' (stays off)":
        {"op": "env_default", "feature": "new_checkout", "value": "v2"},
    "b1 change override: beta_users new_checkout enabled=false":
        {"op": "segment_override", "feature": "new_checkout", "segment": "beta_users", "enabled": False},
    "b2 add override: beta_users checkout_layout off (default priority)":
        {"op": "segment_override", "feature": "checkout_layout", "segment": "beta_users", "enabled": False},
    "b3 add override: beta_users checkout_layout off @priority 0 (experiment -> 1)": [
        {"op": "segment_override", "feature": "checkout_layout", "segment": "experiment-534-rollout", "priority": 1},
        {"op": "segment_override", "feature": "checkout_layout", "segment": "beta_users", "enabled": False, "priority": 0},
    ],
    "c  20% rollout: new segment PERCENTAGE_SPLIT 20 + new_checkout on":
        {"op": "percentage_rollout", "feature": "new_checkout", "percentage": 20},
    "d1 mv weights env default: checkout_layout compact 0->50":
        {"op": "mv_weights", "feature": "checkout_layout", "weights": {"compact": 50}},
    "d2 mv weights experiment segment: compact 50->70":
        {"op": "mv_weights", "feature": "checkout_layout", "segment": "experiment-534-rollout", "weights": {"compact": 70}},
}


def main() -> None:
    doc = json.loads(Path(sys.argv[1]).read_text())
    ids = synthetic_identities(1000, 42)
    out = {}
    for name, mutation in CASES.items():
        r = simulate(doc, mutation, ids)
        out[name] = {
            "changed_identities": r["changed_identities"],
            "features": {f: {"transitions": d["transitions"],
                             "by_plan": {g: {k: v for k, v in c.items() if k != "users"} for g, c in d["by_group"].items()}}
                         for f, d in r["features"].items()},
            "warnings": r["warnings"],
        }
    print(json.dumps({"population": r["population"], "cases": out}, indent=2))


if __name__ == "__main__":
    main()
