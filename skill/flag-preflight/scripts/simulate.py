"""Flag Preflight — offline impact simulation of a flag change.

    simulate(before_doc, mutation) -> diff

`before_doc` is a Flagsmith environment document (`flagsmith environment
document`). `mutation` is a plain, JSON-serialisable dict (or a list of them,
applied in order). Supported ops:

  {"op": "env_default", "feature": F, "enabled": bool?, "value": any?}
  {"op": "segment_override", "feature": F, "segment": name|id,
       "enabled": bool?, "value": any?, "priority": int?}
  {"op": "percentage_rollout", "feature": F, "percentage": 0..100,
       "enabled": True, "value": any?, "segment_name": str?, "segment_id": int?,
       "priority": int?}
  {"op": "mv_weights", "feature": F, "weights": {variant_key|option_id: pct},
       "segment": name|id|None}          # None = environment default

Evaluation uses the official Flagsmith SDK in offline mode, so it is the same
engine local-evaluation SDKs run. Nothing here talks to the network.
"""

from __future__ import annotations

import copy
import json
import random
import uuid
from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping, Sequence

from flagsmith import Flagsmith

_UNSET = object()

Identity = tuple[str, Mapping[str, Any]]
Mutation = Mapping[str, Any] | Sequence[Mapping[str, Any]]


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #
class DocHandler:
    """Offline handler serving an in-memory environment document."""

    def __init__(self, doc: Mapping[str, Any]) -> None:
        self._doc = doc

    def get_environment(self) -> Mapping[str, Any]:
        return self._doc


def client_for(doc: Mapping[str, Any]) -> Flagsmith:
    if not doc.get("api_key"):
        # The identity hash key is f"{api_key}_{identifier}" when composite-key
        # hashing is on, so a stand-in key would move splits and variants.
        raise ValueError("environment document has no api_key; use the unmodified document")
    return Flagsmith(offline_mode=True, offline_handler=DocHandler(doc))


def synthetic_identities(
    n: int = 1000,
    seed: int = 42,
    plans: Sequence[tuple[str, float]] = (("free", 0.6), ("pro", 0.3), ("beta", 0.1)),
) -> list[Identity]:
    rng = random.Random(seed)
    names, weights = zip(*plans)
    return [(f"user_{i}", {"plan": rng.choices(names, weights)[0]}) for i in range(n)]


def evaluate(doc: Mapping[str, Any], identities: Iterable[Identity]) -> dict[str, dict[str, tuple[bool, Any]]]:
    """identifier -> {feature_name: (enabled, value)}"""
    c = client_for(doc)
    out = {}
    for identifier, traits in identities:
        flags = c.get_identity_flags(identifier, dict(traits))
        out[identifier] = {f.feature_name: (f.enabled, f.value) for f in flags.all_flags()}
    return out


# --------------------------------------------------------------------------- #
# Document helpers
# --------------------------------------------------------------------------- #
def _env_fs(doc: dict, feature: str) -> dict:
    hits = [fs for fs in doc["feature_states"] if fs["feature"]["name"] == feature]
    if len(hits) != 1:
        raise ValueError(f"feature {feature!r}: expected 1 environment feature_state, found {len(hits)}")
    return hits[0]


def _segment(doc: dict, ref: str | int) -> dict:
    segs = doc["project"]["segments"]
    hits = [s for s in segs if s["id"] == ref or s["name"] == ref or str(s["id"]) == str(ref)]
    if len(hits) != 1:
        raise ValueError(f"segment {ref!r} not found in environment document")
    return hits[0]


def _segment_fs(segment: dict, feature: str) -> dict | None:
    hits = [fs for fs in segment.get("feature_states", []) if fs["feature"]["name"] == feature]
    return hits[0] if hits else None


def _overrides(doc: dict, feature: str) -> list[tuple[dict, dict]]:
    """[(segment, feature_state)] for every segment override of `feature`."""
    return [
        (s, fs)
        for s in doc["project"]["segments"]
        for fs in s.get("feature_states", [])
        if fs["feature"]["name"] == feature
    ]


def _next_priority(doc: dict, feature: str) -> int:
    prios = [fs["feature_segment"]["priority"] for _, fs in _overrides(doc, feature)]
    return max(prios) + 1 if prios else 0


def _new_override(doc: dict, feature: str, priority: int) -> dict:
    """A segment feature_state shaped like the ones Flagsmith emits."""
    base = _env_fs(doc, feature)
    return {
        "django_id": None,
        "enabled": base["enabled"],
        "feature": copy.deepcopy(base["feature"]),
        "feature_segment": {"priority": priority},
        "feature_state_value": base["feature_state_value"],
        "featurestate_uuid": str(uuid.uuid4()),
        "multivariate_feature_state_values": [
            {**copy.deepcopy(mv), "id": None, "mv_fs_value_uuid": str(uuid.uuid4())}
            for mv in base.get("multivariate_feature_state_values") or []
        ],
    }


def _set_state(fs: dict, m: Mapping[str, Any]) -> None:
    if m.get("enabled", _UNSET) is not _UNSET:
        fs["enabled"] = bool(m["enabled"])
    if m.get("value", _UNSET) is not _UNSET:
        fs["feature_state_value"] = m["value"]


# --------------------------------------------------------------------------- #
# Mutations
# --------------------------------------------------------------------------- #
def _op_env_default(doc: dict, m: Mapping[str, Any]) -> None:
    _set_state(_env_fs(doc, m["feature"]), m)


def _op_segment_override(doc: dict, m: Mapping[str, Any]) -> None:
    seg = _segment(doc, m["segment"])
    fs = _segment_fs(seg, m["feature"])
    if fs is None:
        prio = m.get("priority", _next_priority(doc, m["feature"]))
        fs = _new_override(doc, m["feature"], prio)
        seg.setdefault("feature_states", []).append(fs)
    elif "priority" in m:
        fs["feature_segment"]["priority"] = m["priority"]
    _set_state(fs, m)


def _op_percentage_rollout(doc: dict, m: Mapping[str, Any]) -> None:
    pct = float(m["percentage"])
    if not 0 <= pct <= 100:
        raise ValueError("percentage must be within 0..100")
    feature = m["feature"]
    # A real segment gets its id from Flagsmith, and the id seeds the split hash:
    # counts are faithful, *which* identities land inside is not (see report).
    seg_id = m.get("segment_id") or -(max([abs(s["id"]) for s in doc["project"]["segments"]] + [0]) + 1)
    fs = _new_override(doc, feature, m.get("priority", _next_priority(doc, feature)))
    _set_state(fs, {"enabled": m.get("enabled", True), **({"value": m["value"]} if "value" in m else {})})
    doc["project"]["segments"].append({
        "id": seg_id,
        "name": m.get("segment_name", f"preflight-{feature}-{pct:g}pct"),
        "rules": [{
            "type": "ALL",
            "rules": [],
            "conditions": [{"operator": "PERCENTAGE_SPLIT", "property_": "$.identity.key", "value": str(pct)}],
        }],
        "feature_states": [fs],
    })


def _op_mv_weights(doc: dict, m: Mapping[str, Any]) -> None:
    seg_ref = m.get("segment")
    if seg_ref is None:
        fs = _env_fs(doc, m["feature"])
    else:
        fs = _segment_fs(_segment(doc, seg_ref), m["feature"])
        if fs is None:
            raise ValueError(f"segment {seg_ref!r} has no override for {m['feature']!r}")
    mvs = fs.get("multivariate_feature_state_values") or []
    if not mvs:
        raise ValueError(f"{m['feature']!r} has no multivariate values on that feature_state")
    for ref, pct in m["weights"].items():
        hit = [
            mv for mv in mvs
            if str(ref) in (mv["multivariate_feature_option"].get("key"), str(mv["multivariate_feature_option"]["id"]))
        ]
        if len(hit) != 1:
            raise ValueError(f"variant {ref!r} not found on {m['feature']!r}")
        hit[0]["percentage_allocation"] = float(pct)
    total = sum(mv["percentage_allocation"] for mv in mvs)
    if total > 100:
        raise ValueError(f"variant weights sum to {total} > 100")


OPS = {
    "env_default": _op_env_default,
    "segment_override": _op_segment_override,
    "percentage_rollout": _op_percentage_rollout,
    "mv_weights": _op_mv_weights,
}


def apply_mutation(before_doc: Mapping[str, Any], mutation: Mutation) -> dict:
    doc = copy.deepcopy(before_doc)
    for m in [mutation] if isinstance(mutation, Mapping) else mutation:
        if m.get("op") not in OPS:
            raise ValueError(f"unknown op {m.get('op')!r}; expected one of {sorted(OPS)}")
        OPS[m["op"]](doc, m)
    return doc


# --------------------------------------------------------------------------- #
# Risk signals
# --------------------------------------------------------------------------- #
def experiments_for(doc: Mapping[str, Any], feature: str) -> list[dict]:
    """Experiments the env doc reports on any feature_state of `feature`."""
    found = {}
    states = [("environment default", fs) for fs in doc["feature_states"] if fs["feature"]["name"] == feature]
    states += [(f"segment {s['name']} ({s['id']})", fs) for s, fs in _overrides(doc, feature)]
    for where, fs in states:
        exp = (fs.get("metadata") or {}).get("experiment")
        if exp:
            e = found.setdefault(exp["id"], {"id": exp["id"], "name": exp.get("name"), "in_experiment": []})
            if exp.get("in_experiment"):
                e["in_experiment"].append(where)
    return list(found.values())


def _mutated_features(mutation: Mutation) -> list[str]:
    ms = [mutation] if isinstance(mutation, Mapping) else mutation
    return sorted({m["feature"] for m in ms})


def _target_of(after_doc: dict, m: Mapping[str, Any]) -> tuple[str, int | None, float]:
    """(label, segment_id or None for env default, override priority) a mutation writes to."""
    if m["op"] in ("env_default",) or (m["op"] == "mv_weights" and m.get("segment") is None):
        return "environment default", None, float("inf")
    if m["op"] == "percentage_rollout":
        seg = after_doc["project"]["segments"][-1]  # appended by _op_percentage_rollout
    else:
        seg = _segment(after_doc, m["segment"])
    fs = _segment_fs(seg, m["feature"])
    return f"segment {seg['name']}", seg["id"], fs["feature_segment"]["priority"] if fs else float("inf")


def explain_no_effect(before_doc: Mapping[str, Any], mutation: Mutation, identities: Sequence[Identity]) -> list[dict]:
    """Why a mutation changes nobody: no-op, empty target, or shadowed by higher-priority overrides."""
    after_doc = apply_mutation(before_doc, mutation)
    c = client_for(after_doc)
    seg_cache = {i: {s.id for s in c.get_identity_segments(i, dict(t))} for i, t in identities}
    out = []
    for m in [mutation] if isinstance(mutation, Mapping) else mutation:
        feature = m["feature"]
        label, seg_id, prio = _target_of(after_doc, m)
        single = apply_mutation(before_doc, m)
        if single == before_doc:
            out.append({"feature": feature, "target": label, "reason": "noop", "detail": "already in the requested state"})
            continue
        members = [i for i, _ in identities if seg_id is None or seg_id in seg_cache[i]]
        if not members:
            out.append({"feature": feature, "target": label, "reason": "empty_target", "members": 0,
                        "detail": f"no sampled identity matches {label}"})
            continue
        order = {s["id"]: n for n, s in enumerate(after_doc["project"]["segments"])}
        winners = Counter()
        for i in members:
            beats = [
                (fs["feature_segment"]["priority"], order[s["id"]], s["name"])
                for s, fs in _overrides(after_doc, feature)
                if s["id"] != seg_id and s["id"] in seg_cache[i]
                and (fs["feature_segment"]["priority"], order[s["id"]]) < (prio, order.get(seg_id, -1) if seg_id else 0)
            ]
            if beats:
                p, _, name = min(beats)
                winners[f"{name} (priority {p})"] += 1
        out.append({"feature": feature, "target": label, "target_priority": None if prio == float("inf") else prio,
                    "reason": "shadowed" if winners else "unexplained", "members": len(members),
                    "shadowed_by": dict(winners)})
    return out


# --------------------------------------------------------------------------- #
# Diff
# --------------------------------------------------------------------------- #
def _fmt(state: tuple[bool, Any] | None) -> str:
    if state is None:
        return "absent"
    enabled, value = state
    return f"{'on' if enabled else 'off'}:{json.dumps(value)}"


def simulate(
    before_doc: Mapping[str, Any],
    mutation: Mutation,
    identities: Sequence[Identity] | None = None,
    group_by: str = "plan",
    n_samples: int = 3,
) -> dict:
    identities = list(identities) if identities is not None else synthetic_identities()
    after_doc = apply_mutation(before_doc, mutation)
    before, after = evaluate(before_doc, identities), evaluate(after_doc, identities)

    population = Counter(str(t.get(group_by, "(unset)")) for _, t in identities)
    per_feature: dict[str, dict] = defaultdict(lambda: {"changed": 0, "transitions": Counter(), "by_group": defaultdict(Counter)})
    changed_ids, samples = set(), []
    for identifier, traits in identities:
        group = str(traits.get(group_by, "(unset)"))
        for feature in sorted(set(before[identifier]) | set(after[identifier])):
            b, a = before[identifier].get(feature), after[identifier].get(feature)
            if b == a:
                continue
            t = f"{_fmt(b)} -> {_fmt(a)}"
            pf = per_feature[feature]
            pf["changed"] += 1
            pf["transitions"][t] += 1
            pf["by_group"][group][t] += 1
            changed_ids.add(identifier)
            if len(samples) < n_samples:
                samples.append({"identifier": identifier, "traits": dict(traits), "feature": feature, "change": t})

    warnings = []
    for feature in _mutated_features(mutation):
        for exp in experiments_for(before_doc, feature):
            warnings.append(
                f"{feature!r} is part of experiment {exp['id']} {exp['name']!r}"
                + (f" (active on: {', '.join(exp['in_experiment'])})" if exp["in_experiment"] else "")
                + " — changing it can contaminate the experiment's results"
            )
    no_effect = []
    if not per_feature or any(f not in per_feature for f in _mutated_features(mutation)):
        no_effect = [e for e in explain_no_effect(before_doc, mutation, identities) if e["feature"] not in per_feature]
        for e in no_effect:
            if e["reason"] == "shadowed":
                by = ", ".join(f"{k} catches {v}/{e['members']}" for k, v in e["shadowed_by"].items())
                warnings.append(f"no identity changes for {e['feature']!r}: {e['target']} loses on priority — {by}")
            else:
                warnings.append(f"no identity changes for {e['feature']!r}: {e.get('detail', e['reason'])}")

    return {
        "mutation": mutation,
        "identities": len(identities),
        "population": dict(population),
        "changed_identities": len(changed_ids),
        "features": {
            f: {
                "changed": d["changed"],
                "transitions": dict(d["transitions"]),
                "by_group": {g: {"users": population[g], **dict(c)} for g, c in sorted(d["by_group"].items())},
            }
            for f, d in sorted(per_feature.items())
        },
        "samples": samples,
        "experiments": {f: experiments_for(before_doc, f) for f in _mutated_features(mutation)},
        "no_effect": no_effect,
        "warnings": warnings,
        "_before": before,
        "_after": after,
    }


if __name__ == "__main__":
    import argparse
    from pathlib import Path

    ap = argparse.ArgumentParser(description="Simulate a flag change offline against an environment document.")
    ap.add_argument("env_doc", type=Path)
    ap.add_argument("mutation", help="mutation as JSON, or @file.json")
    ap.add_argument("-n", type=int, default=1000, help="synthetic identities")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    raw = Path(args.mutation[1:]).read_text() if args.mutation.startswith("@") else args.mutation
    result = simulate(json.loads(args.env_doc.read_text()), json.loads(raw), synthetic_identities(args.n, args.seed))
    print(json.dumps({k: v for k, v in result.items() if not k.startswith("_")}, indent=2, default=str))
