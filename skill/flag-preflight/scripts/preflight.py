#!/usr/bin/env python3
"""Flag Preflight — plan, apply, verify and roll back a Flagsmith flag change.

    preflight.py plan     --env <name> --flag <f> --change <spec> [--identities N | --from-api] [--json]
    preflight.py apply    --plan <id> --confirm <exact env name> [--override-experiment "<reason>"] [--approve-override]
    preflight.py verify   --plan <id> [--json]
    preflight.py rollback --plan <id> [--force] [--json]

Change specs:
    enabled=true|false                 environment default on/off
    value=<v>                          environment default value
    segment:<seg>:enabled=<bool>       add/change a segment override
    segment:<seg>:value=<v>
    segment:<seg>:weight:<k>=<pct>,..  variant weights on a segment override
    rollout:<pct>                      new PERCENTAGE_SPLIT segment + override on
    weight:<k>=<pct>[,<k>=<pct>]       variant weights on the environment default

`plan` is read-only. Writes happen only in `apply`/`rollback`, always through
the Flagsmith CLI, and only after the freshness and confirmation checks.

Credentials: the Flagsmith CLI's own (FLAGSMITH_API_KEY or `flagsmith login`).
Project: --project, FLAGSMITH_PROJECT, or the only project the credential sees.
Nothing secret is written to disk.

Exit codes (plan): 0 safe, 2 warnings, 3 blocked (active experiment on the flag).
Other commands: 0 ok, 1 refused/failed, 2 completed with warnings or mismatches.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from simulate import (  # noqa: E402
    _env_fs, _overrides, _segment, _segment_fs, apply_mutation, evaluate, simulate, synthetic_identities,
)

STATE_DIR = Path(os.environ.get("PREFLIGHT_DIR", ".preflight"))
PLANS = STATE_DIR / "plans"
AUDIT = STATE_DIR / "audit.log"
EXIT_SAFE, EXIT_WARN, EXIT_BLOCKED, EXIT_FAIL = 0, 2, 3, 1
SAMPLE_SIZE = 30
VOLATILE = {"featurestate_uuid", "mv_fs_value_uuid", "updated_at", "api_key"}


class Refused(Exception):
    """A safety check said no. Message is shown to the user; exit 1."""


# --------------------------------------------------------------------------- #
# Flagsmith CLI wrapper
# --------------------------------------------------------------------------- #
def _cli_env() -> dict:
    env = dict(os.environ)
    # Environment selection is always explicit (-e <client key>); a stray server
    # key in the environment would silently pin commands to another environment.
    env.pop("FLAGSMITH_ENVIRONMENT_KEY", None)
    env.pop("FLAGSMITH_ENVIRONMENT", None)
    env["FLAGSMITH_NO_INPUT"] = "1"
    return env


def cli(*args: str, write: bool = False) -> str:
    cmd = ["flagsmith", *args] + (["--yes"] if write else [])
    p = subprocess.run(cmd, capture_output=True, text=True, env=_cli_env())
    if p.returncode != 0:
        err = (p.stderr or p.stdout).strip().splitlines()
        raise Refused(f"flagsmith {' '.join(args[:3])} failed: {err[0] if err else p.returncode}")
    return p.stdout


def cli_json(*args: str) -> Any:
    return json.loads(cli(*args, "--json"))


class Env:
    """A resolved environment: name, numeric id, client-side key, project."""

    def __init__(self, project: str, name: str) -> None:
        envs = cli_json("environment", "list", "-p", project)
        hits = [e for e in envs if e["name"] == name]
        if len(hits) != 1:
            raise Refused(f"environment {name!r} not found in project {project!r} (have: {', '.join(e['name'] for e in envs)})")
        e = hits[0]
        self.project, self.name, self.id, self.key = project, e["name"], e["id"], e["api_key"]
        self.v2_versioning = bool(e.get("use_v2_feature_versioning"))
        self.change_requests = int(e.get("minimum_change_request_approvals") or 0)

    @classmethod
    def from_plan(cls, plan: dict) -> "Env":
        return cls(plan["project"], plan["env"]["name"])

    def args(self) -> list[str]:
        return ["-p", self.project, "-e", self.key]

    def document(self) -> dict:
        doc = json.loads(cli("environment", "document", self.name, "-p", self.project))
        if doc.get("name") != self.name:
            raise Refused(f"asked for {self.name!r} but the document is for {doc.get('name')!r}")
        return doc


# --------------------------------------------------------------------------- #
# Live state. The environment document is served from a cache that can lag a
# write by a minute or more; the Admin API's feature versions are not cached.
# --------------------------------------------------------------------------- #
DOC_WAIT_S = float(os.environ.get("PREFLIGHT_DOC_WAIT", 150))


def live_version(env: Env, feature_id: int) -> str | None:
    if not env.v2_versioning:
        return None
    res = cli_json("api", f"api/v1/environments/{env.id}/features/{feature_id}/versions/?page_size=1")
    return (res.get("results") or [{}])[0].get("uuid")


def _version_fs_uuids(env: Env, feature_id: int, version: str) -> set[str]:
    res = cli_json("api", f"api/v1/environments/{env.id}/features/{feature_id}/versions/{version}/featurestates/")
    return {fs["uuid"] for fs in res if fs.get("identity") is None}


def _doc_fs_uuids(doc: dict, flag: str) -> set[str]:
    return {_env_fs(doc, flag)["featurestate_uuid"]} | {fs["featurestate_uuid"] for _, fs in _overrides(doc, flag)}


def current_document(env: Env, flag: str, newer_than: str | None = None,
                     wait_s: float = DOC_WAIT_S) -> tuple[dict, str | None]:
    """The env document once it reflects `flag`'s live version (and, after a write,
    once its updated_at moved past `newer_than`), and that version."""
    deadline, waited = time.time() + wait_s, False
    while True:
        doc = env.document()
        fid = _env_fs(doc, flag)["feature"]["id"]
        version = live_version(env, fid)
        caught_up = newer_than is None or (doc.get("updated_at") or "") > newer_than
        if caught_up and (version is None or _doc_fs_uuids(doc, flag) == _version_fs_uuids(env, fid, version)):
            if waited:
                print(f"  (environment document caught up with live version {version[:8]})", file=sys.stderr)
            return doc, version
        if time.time() > deadline:
            raise Refused(f"environment document still lags the live state of {flag} after {wait_s:g}s; retry shortly")
        if not waited:
            print("  (environment document lags the live version; waiting for it to catch up…)", file=sys.stderr)
        waited = True
        time.sleep(5)


# --------------------------------------------------------------------------- #
# Documents, hashing, state
# --------------------------------------------------------------------------- #
def _strip(o: Any, drop: set[str]) -> Any:
    if isinstance(o, dict):
        return {k: _strip(v, drop) for k, v in o.items() if k not in drop}
    if isinstance(o, list):
        return sorted((_strip(v, drop) for v in o), key=lambda v: json.dumps(v, sort_keys=True))
    return o


def doc_hash(doc: dict) -> str:
    """sha256 of the document minus its key: any change at all, incl. new versions."""
    return hashlib.sha256(json.dumps(_strip(doc, {"api_key"}), sort_keys=True).encode()).hexdigest()


def semantic(doc: dict) -> dict:
    """The document with per-version bookkeeping removed: what evaluation depends on."""
    return _strip(doc, VOLATILE)


def _diff_paths(a: Any, b: Any, path: str = "") -> list[str]:
    if type(a) is not type(b):
        return [path or "/"]
    if isinstance(a, dict):
        return [p for k in sorted(set(a) | set(b)) for p in _diff_paths(a.get(k), b.get(k), f"{path}/{k}")]
    if isinstance(a, list):
        if len(a) != len(b):
            return [f"{path} (len {len(a)} -> {len(b)})"]
        return [p for i, (x, y) in enumerate(zip(a, b)) for p in _diff_paths(x, y, f"{path}[{i}]")]
    return [] if a == b else [f"{path}: {json.dumps(a)} -> {json.dumps(b)}"]


def flag_state(doc: dict, flag: str) -> dict:
    """Everything about `flag` that rollback needs to put back."""
    def fs_view(fs: dict) -> dict:
        return {
            "enabled": fs["enabled"],
            "value": fs["feature_state_value"],
            "django_id": fs.get("django_id"),
            "featurestate_uuid": fs.get("featurestate_uuid"),
            "weights": {mv["multivariate_feature_option"]["key"] or str(mv["multivariate_feature_option"]["id"]):
                        mv["percentage_allocation"] for mv in fs.get("multivariate_feature_state_values") or []},
        }
    env_fs = _env_fs(doc, flag)
    return {
        "feature_id": env_fs["feature"]["id"],
        "feature_type": env_fs["feature"]["type"],
        "env_default": fs_view(env_fs),
        "overrides": [{"segment_id": s["id"], "segment_name": s["name"],
                       "priority": fs["feature_segment"]["priority"], **fs_view(fs)}
                      for s, fs in _overrides(doc, flag)],
    }


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def save_plan(plan: dict) -> None:
    PLANS.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = PLANS / f"{plan['plan_id']}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(plan, indent=2, default=str))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def load_plan(plan_id: str) -> dict:
    path = PLANS / f"{plan_id}.json"
    if not path.exists():
        raise Refused(f"no plan {plan_id!r} in {PLANS}")
    return json.loads(path.read_text())


def snapshot_path(plan: dict, label: str) -> Path:
    return PLANS / f"{plan['plan_id']}.{label}.json"


def save_snapshot(plan: dict, label: str, doc: dict) -> None:
    PLANS.mkdir(parents=True, exist_ok=True, mode=0o700)
    p = snapshot_path(plan, label)
    p.write_text(json.dumps(_strip(doc, {"api_key"}), indent=2))
    os.chmod(p, 0o600)


# --------------------------------------------------------------------------- #
# Audit log: append-only JSON lines, each chained to the previous line's hash
# --------------------------------------------------------------------------- #
_ACTOR: str | None = None


def actor() -> str:
    global _ACTOR
    if _ACTOR is None:
        try:
            kind = json.loads(cli("auth", "status", "--json")).get("kind", "unknown")
        except Exception:
            kind = "unknown"
        _ACTOR = f"{getpass.getuser()}@{socket.gethostname()} via {kind} credential"
    return _ACTOR


def audit(action: str, plan: dict | None, result: str, **detail: Any) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    prev = "0" * 64
    if AUDIT.exists():
        lines = AUDIT.read_bytes().splitlines()
        if lines:
            prev = hashlib.sha256(lines[-1]).hexdigest()
    entry = {"ts": now(), "actor": actor(), "action": action,
             "plan_id": plan and plan.get("plan_id"),
             "env": plan and plan.get("env", {}).get("name"),
             "flag": plan and plan.get("flag"), "spec": plan and plan.get("spec"),
             "result": result, **detail, "prev": prev}
    fd = os.open(AUDIT, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def history(plan: dict, action: str, result: str, **detail: Any) -> None:
    plan.setdefault("history", []).append({"ts": now(), "action": action, "result": result, **detail})
    save_plan(plan)
    audit(action, plan, result, **detail)


# --------------------------------------------------------------------------- #
# Change specs
# --------------------------------------------------------------------------- #
def parse_value(raw: str) -> tuple[Any, str]:
    """CLI-compatible type inference -> (python value, --type)."""
    if raw in ("true", "false"):
        return raw == "true", "boolean"
    if re.fullmatch(r"-?\d+", raw):
        return int(raw), "integer"
    return raw, "string"


def parse_bool(raw: str) -> bool:
    if raw not in ("true", "false"):
        raise Refused(f"expected true or false, got {raw!r}")
    return raw == "true"


def parse_weights(raw: str) -> dict[str, float]:
    out = {}
    for part in raw.split(","):
        k, sep, v = part.partition("=")
        if not sep or not k:
            raise Refused(f"bad weight {part!r}; expected <variant>=<pct>")
        out[k] = float(v)
    return out


def parse_spec(flag: str, spec: str) -> dict:
    if spec.startswith("segment:"):
        _, seg, rest = (spec.split(":", 2) + ["", ""])[:3]
        if not seg or not rest:
            raise Refused("segment spec is segment:<name|id>:<enabled=..|value=..|weight:k=pct>")
        if rest.startswith("weight:"):
            return {"op": "mv_weights", "feature": flag, "segment": seg, "weights": parse_weights(rest[7:])}
        key, _, raw = rest.partition("=")
        if key == "enabled":
            return {"op": "segment_override", "feature": flag, "segment": seg, "enabled": parse_bool(raw)}
        if key == "value":
            return {"op": "segment_override", "feature": flag, "segment": seg, "value": parse_value(raw)[0]}
        raise Refused(f"unknown segment field {key!r}")
    if spec.startswith("rollout:"):
        pct = float(spec[8:])
        if not 0 < pct <= 100:
            raise Refused("rollout percentage must be in (0, 100]")
        return {"op": "percentage_rollout", "feature": flag, "percentage": pct, "enabled": True,
                "segment_name": f"preflight-{flag}-{pct:g}pct"}
    if spec.startswith("weight:"):
        return {"op": "mv_weights", "feature": flag, "weights": parse_weights(spec[7:])}
    key, sep, raw = spec.partition("=")
    if key == "enabled" and sep:
        return {"op": "env_default", "feature": flag, "enabled": parse_bool(raw)}
    if key == "value" and sep:
        return {"op": "env_default", "feature": flag, "value": parse_value(raw)[0]}
    raise Refused(f"unrecognised change spec {spec!r}")


def describe(before_doc: dict, m: dict) -> str:
    f = m["feature"]
    if m["op"] == "env_default":
        fs = _env_fs(before_doc, f)
        parts = []
        if "enabled" in m:
            parts.append(f"enabled {str(fs['enabled']).lower()} -> {str(m['enabled']).lower()}")
        if "value" in m:
            parts.append(f"value {json.dumps(fs['feature_state_value'])} -> {json.dumps(m['value'])}")
        return f"{f}: environment default " + ", ".join(parts)
    if m["op"] == "segment_override":
        seg = _segment(before_doc, m["segment"])
        fs = _segment_fs(seg, f)
        if fs is None:
            what = ", ".join(f"{k}={json.dumps(m[k])}" for k in ("enabled", "value") if k in m)
            return f"{f}: NEW override on segment {seg['name']} ({what}, lowest priority)"
        parts = [f"{k} {json.dumps(fs['enabled' if k == 'enabled' else 'feature_state_value'])} -> {json.dumps(m[k])}"
                 for k in ("enabled", "value") if k in m]
        return f"{f}: override on segment {seg['name']} (priority {fs['feature_segment']['priority']}) " + ", ".join(parts)
    if m["op"] == "percentage_rollout":
        return f"{f}: NEW segment {m['segment_name']} (PERCENTAGE_SPLIT {m['percentage']:g}%) + override enabled=true"
    if m["op"] == "mv_weights":
        where = "environment default" if m.get("segment") is None else f"segment {m['segment']}"
        cur = flag_state(before_doc, f)
        src = cur["env_default"] if m.get("segment") is None else next(
            (o for o in cur["overrides"] if str(o["segment_id"]) == str(m["segment"]) or o["segment_name"] == m["segment"]), {})
        ws = ", ".join(f"{k} {src.get('weights', {}).get(k, '?')} -> {v:g}" for k, v in m["weights"].items())
        return f"{f}: variant weights on {where}: {ws}"
    return json.dumps(m)


# --------------------------------------------------------------------------- #
# Identities
# --------------------------------------------------------------------------- #
def identities_from_api(env: Env, limit: int) -> list[tuple[str, dict]]:
    out = []
    page = cli_json("api", f"api/v1/environments/{env.key}/edge-identities/?page_size={min(limit, 100)}")
    for ident in page.get("results", [])[:limit]:
        traits = cli_json("api", f"api/v1/environments/{env.key}/edge-identities/{ident['identity_uuid']}/list-traits/")
        out.append((ident["identifier"], {t["trait_key"]: t["trait_value"] for t in traits}))
    if not out:
        raise Refused(f"no identities stored in {env.name}; use --identities N for a synthetic population")
    return out


def pick_sample(identities: list, before: dict, after: dict, flag: str, n: int = SAMPLE_SIZE) -> list:
    """Half changed, half unchanged, spread across groups, deterministic."""
    changed = [i for i in identities if before[i[0]].get(flag) != after[i[0]].get(flag)]
    same = [i for i in identities if before[i[0]].get(flag) == after[i[0]].get(flag)]

    def spread(pool: list, k: int) -> list:
        by: dict[str, list] = {}
        for i in pool:
            by.setdefault(json.dumps(i[1], sort_keys=True), []).append(i)
        out, groups = [], list(by.values())
        while len(out) < k and any(groups):
            for g in groups:
                if g and len(out) < k:
                    out.append(g.pop(0))
        return out

    k_changed = min(len(changed), n // 2 if same else n)
    picked = spread(changed, k_changed) + spread(same, n - k_changed)
    return [{"identifier": i, "traits": t,
             "before": {f: list(v) for f, v in before[i].items()},
             "after": {f: list(v) for f, v in after[i].items()}} for i, t in picked]


# --------------------------------------------------------------------------- #
# Risk assessment
# --------------------------------------------------------------------------- #
def running_experiments(env: Env, feature_id: int) -> list[dict] | None:
    try:
        res = cli_json("api", f"api/v1/environments/{env.key}/experiments/?status=running&page_size=100")
    except Refused:
        return None  # experiments API unavailable (plan/beta); doc metadata still applies
    return [{"id": e["id"], "name": e["name"], "status": e["status"]}
            for e in res.get("results", []) if (e.get("feature") or {}).get("id") == feature_id]


def assess(env: Env, before_doc: dict, mutation: dict, sim: dict, risk_pct: float) -> list[dict]:
    w: list[dict] = []
    flag = mutation["feature"]
    feature_id = _env_fs(before_doc, flag)["feature"]["id"]

    doc_exps = [e for e in sim["experiments"].get(flag, []) if e["in_experiment"]]
    api_exps = running_experiments(env, feature_id)
    seen = set()
    for e in doc_exps + (api_exps or []):
        if e["id"] in seen:
            continue
        seen.add(e["id"])
        w.append({"level": "block", "code": "EXPERIMENT_ACTIVE",
                  "message": f"experiment {e['id']} {e['name']!r} is running on {flag}; a change contaminates its results"})
    if api_exps is None:
        w.append({"level": "info", "code": "EXPERIMENTS_API_UNAVAILABLE",
                  "message": "could not query the experiments API; relied on env doc metadata only"})

    for e in sim["no_effect"]:
        if e["reason"] == "shadowed":
            by = ", ".join(f"{k} catches {v}/{e['members']}" for k, v in e["shadowed_by"].items())
            msg = f"0 identities change: {e['target']} loses on priority — {by}"
        elif e["reason"] == "noop":
            msg = "0 identities change: flag is already in the requested state"
        elif e["reason"] == "empty_target":
            msg = f"0 identities change: no sampled identity is in {e['target']}"
        else:
            msg = f"0 identities change ({e['reason']})"
        w.append({"level": "warn", "code": "NO_EFFECT", "message": msg})

    if re.search(r"prod", env.name, re.I):
        w.append({"level": "warn", "code": "RISK_PRODUCTION", "message": f"target environment is {env.name}"})
    share = 100 * sim["changed_identities"] / max(sim["identities"], 1)
    if share >= risk_pct:
        w.append({"level": "warn", "code": "RISK_LARGE_BLAST",
                  "message": f"{share:.1f}% of identities change (threshold {risk_pct:g}%)"})
    if mutation["op"] == "percentage_rollout" and mutation["percentage"] >= risk_pct:
        w.append({"level": "warn", "code": "RISK_LARGE_BLAST",
                  "message": f"rollout of {mutation['percentage']:g}% (threshold {risk_pct:g}%)"})
    if mutation["op"] == "percentage_rollout":
        w.append({"level": "warn", "code": "PROJECT_SCOPED_WRITE",
                  "message": f"creates a segment in project {env.project}: it appears in every environment's "
                             "document (no effect without an override) and invalidates their open plans"})
        w.append({"level": "info", "code": "APPROXIMATE_UNTIL_SEGMENT_EXISTS",
                  "message": "count is faithful; which identities land in the split is re-simulated after the segment exists"})
    if mutation["op"] == "env_default" and "value" in mutation and _env_fs(before_doc, flag)["feature_state_value"] is None:
        w.append({"level": "warn", "code": "ROLLBACK_LIMITED",
                  "message": "current value is null; the CLI cannot set a null value back on rollback"})
    if env.change_requests:
        w.append({"level": "warn", "code": "CHANGE_REQUESTS",
                  "message": f"{env.name} requires {env.change_requests} approval(s); apply may only open a change request"})
    return w


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _state(v: list | None) -> str:
    if v is None:
        return "absent"
    return ("on" if v[0] else "off") + ("" if v[1] is None else f":{json.dumps(v[1])}")


def render_plan(plan: dict) -> str:
    s, f = plan["simulation"], plan["flag"]
    lines = [
        f"Flag Preflight plan {plan['plan_id']}",
        f"env      {plan['env']['name']} (project {plan['project']})   doc {plan['doc']['sha256'][:12]} @ {plan['doc']['updated_at']}",
        f"change   {plan['summary']}",
        f"sample   {s['identities']} {s['source']} identities" + (f" (seed {s['seed']})" if s.get("seed") is not None else ""),
        f"impact   {s['changed_identities']}/{s['identities']} identities change ({100 * s['changed_identities'] / max(s['identities'], 1):.1f}%)",
    ]
    ft = s["features"].get(f, {"by_group": {}})
    for g, n in sorted(s["population"].items()):
        moves = {k: v for k, v in ft["by_group"].get(g, {}).items() if k != "users"}
        moved = sum(moves.values())
        lines.append(f"  {g:<6} {moved:>4}/{n:<4} " + ("; ".join(f"{k} ({v})" for k, v in moves.items()) or "-"))
    others = [k for k in s["features"] if k != f]
    if others:
        lines.append(f"  also changes: {', '.join(others)}")
    shown = [w for w in plan["warnings"] if w["level"] != "info"]
    if shown:
        lines.append("warnings")
        lines += [f"  {'X' if w['level'] == 'block' else '!'} {w['code']:<18} {w['message']}" for w in shown]
    lines.append(f"status   {plan['status'].upper()} (exit {plan['exit_code']})")
    if plan["status"] == "blocked":
        lines.append(f"next     resolve the experiment, or apply with --override-experiment \"<reason>\"")
    else:
        lines.append(f"next     preflight.py apply --plan {plan['plan_id']} --confirm {plan['env']['name']}")
    return "\n".join(lines[:25])


def emit(obj: dict, text: str, as_json: bool) -> None:
    print(json.dumps(obj, indent=2, default=str) if as_json else text)


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #
def _simulate_for_plan(doc: dict, mutation: dict, identities: list) -> tuple[dict, list]:
    sim = simulate(doc, mutation, identities)
    sample = pick_sample(identities, sim.pop("_before"), sim.pop("_after"), mutation["feature"])
    return sim, sample


def cmd_plan(a: argparse.Namespace) -> int:
    env = Env(a.project, a.env)
    doc, version = current_document(env, a.flag)
    mutation = parse_spec(a.flag, a.change)
    apply_mutation(doc, mutation)  # validate against this document before anything else

    if a.from_api:
        identities, source, seed = identities_from_api(env, a.identities), "stored", None
    else:
        identities, source, seed = synthetic_identities(a.identities, a.seed), "synthetic", a.seed
    sim, sample = _simulate_for_plan(doc, mutation, identities)
    warnings = assess(env, doc, mutation, sim, a.risk_pct)

    blocked = any(w["level"] == "block" for w in warnings)
    warned = any(w["level"] == "warn" for w in warnings)
    status, code = ("blocked", EXIT_BLOCKED) if blocked else ("warning", EXIT_WARN) if warned else ("safe", EXIT_SAFE)
    digest = doc_hash(doc)
    plan = {
        "plan_id": f"{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}-{a.flag}-{hashlib.sha256((digest + a.change).encode()).hexdigest()[:6]}",
        "created_at": now(), "created_by": actor(),
        "project": env.project,
        "env": {"name": env.name, "id": env.id, "v2_versioning": env.v2_versioning},
        "flag": a.flag, "spec": a.change, "mutation": mutation, "summary": describe(doc, mutation),
        "doc": {"sha256": digest, "updated_at": doc.get("updated_at"), "live_version": version},
        "rollback": {"before": flag_state(doc, a.flag), "live_version": version},
        "simulation": {"source": source, "seed": seed, **{k: sim[k] for k in ("identities", "population", "changed_identities", "features", "no_effect")}},
        "sample": sample,
        "warnings": warnings, "status": status, "exit_code": code,
        "stage": "planned",
    }
    save_snapshot(plan, "before", doc)
    save_plan(plan)
    audit("plan", plan, status, changed=sim["changed_identities"], identities=sim["identities"],
          warnings=[w["code"] for w in warnings])
    emit(plan, render_plan(plan), a.json)
    return code


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #
def _check_fresh(env: Env, plan: dict, key: str = "doc") -> dict:
    doc, version = current_document(env, plan["flag"])
    if version != plan[key].get("live_version"):
        raise Refused(f"{plan['flag']} has a new live version in {env.name} since this was recorded "
                      f"({str(plan[key].get('live_version'))[:8]} -> {str(version)[:8]}). Run `plan` again.")
    if doc_hash(doc) != plan[key]["sha256"] or doc.get("updated_at") != plan[key]["updated_at"]:
        snap = {"doc": "before", "applied": "applied", "segment_stage": "segment"}[key]
        diff = _diff_paths(semantic(json.loads(snapshot_path(plan, snap).read_text())), semantic(doc))
        raise Refused(f"{env.name} changed since this plan was made ({len(diff)} semantic differences"
                      f"{': ' + diff[0] if diff else '; new versions only'}). Run `plan` again.")
    return doc


def _seg_ref(doc: dict, ref: str) -> str:
    return str(_segment(doc, ref)["id"])


def _update_args(m: dict) -> list[str]:
    out = []
    if "enabled" in m:
        out.append("--enable" if m["enabled"] else "--disable")
    if "value" in m:
        v = m["value"]
        typ = "boolean" if isinstance(v, bool) else "integer" if isinstance(v, int) else "string"
        out += ["--value", json.dumps(v) if isinstance(v, bool) else str(v), "--type", typ]
    if "priority" in m:
        out += ["--priority", str(m["priority"])]
    return out


def _write(env: Env, plan: dict, *args: str) -> None:
    cli(*args, *env.args(), write=True)
    plan.setdefault("writes", []).append(" ".join(a for a in args))


def cmd_apply(a: argparse.Namespace) -> int:
    plan = load_plan(a.plan)
    try:
        if a.confirm != plan["env"]["name"]:
            raise Refused(f"--confirm {a.confirm!r} does not match the plan's environment {plan['env']['name']!r}")
        stage = plan["stage"]
        if stage in ("applied", "rolled_back"):
            raise Refused(f"plan is already {stage}; make a new plan")
        if plan["status"] == "blocked":
            if not a.override_experiment:
                raise Refused("plan is BLOCKED by an active experiment; pass --override-experiment \"<reason>\" to proceed")
            plan["experiment_override"] = {"reason": a.override_experiment, "by": actor(), "at": now()}
            audit("override-experiment", plan, "accepted", reason=a.override_experiment)
        env = Env.from_plan(plan)
        m, flag = plan["mutation"], plan["flag"]

        if stage == "awaiting_override_approval":
            if not a.approve_override:
                raise Refused("segment exists; re-run with --approve-override to add the override (second approval)")
            doc = _check_fresh(env, plan, "segment_stage")
            seg_id = str(plan["created_segment"]["id"])
            _write(env, plan, "flag", "update", flag, "--segment", seg_id, "--enable")
            return _finish_apply(env, plan, doc.get("updated_at"))

        doc = _check_fresh(env, plan)
        if m["op"] == "env_default":
            _write(env, plan, "flag", "update", flag, *_update_args(m))
        elif m["op"] == "segment_override":
            _write(env, plan, "flag", "update", flag, "--segment", _seg_ref(doc, m["segment"]), *_update_args(m))
        elif m["op"] == "mv_weights":
            weights = ",".join(f"{k}={v:g}" for k, v in m["weights"].items())
            seg = ["--segment", _seg_ref(doc, m["segment"])] if m.get("segment") is not None else []
            _write(env, plan, "flag", "update", flag, *seg, "--weight", weights)
        elif m["op"] == "percentage_rollout":
            return _apply_rollout_stage1(env, plan, doc)
        return _finish_apply(env, plan, doc.get("updated_at"))
    except Refused as e:
        history(plan, "apply", "refused", reason=str(e), writes=plan.get("writes", []))
        print(f"REFUSED: {e}", file=sys.stderr)
        return EXIT_FAIL


def _apply_rollout_stage1(env: Env, plan: dict, doc: dict) -> int:
    m, flag = plan["mutation"], plan["flag"]
    pre_updated = doc.get("updated_at")
    rules = {"type": "ALL", "rules": [{"type": "ANY", "conditions": [
        # An empty property is what the engine reads as "identity key", but the
        # Admin API answers 500 to it; the JSONPath form is accepted.
        {"property": "$.identity.key", "operator": "PERCENTAGE_SPLIT", "value": f"{m['percentage']:g}"}]}]}
    cli("segment", "create", m["segment_name"], "--rules", json.dumps(rules), "--feature", flag,
        "--description", f"Flag Preflight rollout, plan {plan['plan_id']}", "-p", env.project, write=True)
    plan.setdefault("writes", []).append(f"segment create {m['segment_name']}")
    seg = cli_json("segment", "get", m["segment_name"], "-p", env.project)
    plan["created_segment"] = {"id": seg["id"], "name": seg["name"]}

    # Precise re-simulation: the real segment id now seeds the split hash.
    doc, version = current_document(env, flag, newer_than=pre_updated)
    precise = dict(m, segment_id=seg["id"])
    if any(s["id"] == seg["id"] for s in doc["project"]["segments"]):
        precise = {"op": "segment_override", "feature": flag, "segment": seg["id"], "enabled": True}
    if plan["simulation"]["source"] == "stored":
        ids = identities_from_api(env, plan["simulation"]["identities"])
    else:
        ids = synthetic_identities(plan["simulation"]["identities"], plan["simulation"]["seed"])
    sim, sample = _simulate_for_plan(doc, precise, ids)
    plan["precise_simulation"] = {"mutation": precise, **{k: sim[k] for k in ("identities", "changed_identities", "features")}}
    plan["sample"] = sample
    plan["segment_stage"] = {"sha256": doc_hash(doc), "updated_at": doc.get("updated_at"), "live_version": version}
    save_snapshot(plan, "segment", doc)
    plan["stage"] = "awaiting_override_approval"
    history(plan, "apply", "segment_created", segment=plan["created_segment"],
            precise_changed=sim["changed_identities"], approx_changed=plan["simulation"]["changed_identities"])
    print(f"segment {seg['name']} ({seg['id']}) created WITHOUT override — nobody is affected yet.\n"
          f"precise impact: {sim['changed_identities']}/{sim['identities']} identities "
          f"(plan estimated {plan['simulation']['changed_identities']})\n"
          f"second approval: preflight.py apply --plan {plan['plan_id']} --confirm {env.name} --approve-override")
    return EXIT_WARN


def _finish_apply(env: Env, plan: dict, pre_updated: str | None) -> int:
    doc, version = current_document(env, plan["flag"], newer_than=pre_updated)
    if version is not None and version == plan["doc"].get("live_version"):
        raise Refused("write reported success but the flag has no new live version (change request pending?)")
    plan["applied"] = {"sha256": doc_hash(doc), "updated_at": doc.get("updated_at"), "live_version": version, "at": now(),
                       "state": flag_state(doc, plan["flag"])}
    save_snapshot(plan, "applied", doc)
    # Offline consistency: does the real post-apply document evaluate as predicted?
    ids = [(s["identifier"], s["traits"]) for s in plan["sample"]]
    got = evaluate(doc, ids)
    ok = sum(all(got[s["identifier"]].get(f) == tuple(v) for f, v in s["after"].items()) for s in plan["sample"])
    plan["stage"] = "applied"
    history(plan, "apply", "applied", writes=plan.get("writes", []), doc_prediction_match=f"{ok}/{len(ids)}")
    print(f"applied to {env.name}: {'; '.join(plan['writes'])}\n"
          f"post-apply document evaluates as predicted for {ok}/{len(ids)} sampled identities\n"
          f"next: preflight.py verify --plan {plan['plan_id']}")
    return EXIT_SAFE if ok == len(ids) else EXIT_WARN


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #
def remote_eval(env: Env, identifier: str, traits: dict) -> dict:
    args = ["eval", "--identity", identifier]
    for k, v in traits.items():
        args += ["--trait", f"{k}={json.dumps(v) if not isinstance(v, str) else v}"]
    flags = cli_json(*args, *env.args())["flags"]
    return {k: [v["enabled"], v["value"]] for k, v in flags.items()}


def compare_remote(env: Env, sample: list, key: str, wait_s: float = 20) -> dict:
    pending, results = list(sample), {}
    deadline = time.time() + wait_s
    while True:
        for s in pending:
            results[s["identifier"]] = remote_eval(env, s["identifier"], s["traits"])
        pending = [s for s in sample if results[s["identifier"]] != s[key]]
        if not pending or time.time() > deadline:
            break
        time.sleep(2)  # propagation to the SDK API is not instant
    mism = [{"identifier": s["identifier"], "predicted": s[key], "remote": results[s["identifier"]]} for s in pending]
    return {"matched": len(sample) - len(pending), "total": len(sample), "mismatches": mism[:5]}


def cmd_verify(a: argparse.Namespace) -> int:
    plan = load_plan(a.plan)
    if plan["stage"] != "applied":
        print(f"REFUSED: plan is {plan['stage']}, verify needs an applied plan", file=sys.stderr)
        return EXIT_FAIL
    env = Env.from_plan(plan)
    r = compare_remote(env, plan["sample"], "after")
    plan["verify"] = {"at": now(), **r}
    history(plan, "verify", "match" if r["matched"] == r["total"] else "mismatch", score=f"{r['matched']}/{r['total']}")
    text = f"verify {plan['plan_id']}: {r['matched']}/{r['total']} sampled identities match the prediction (remote, transient)"
    if r["mismatches"]:
        text += "\n" + "\n".join(f"  {x['identifier']}: predicted {x['predicted']} remote {x['remote']}" for x in r["mismatches"])
    emit(plan["verify"], text, a.json)
    return EXIT_SAFE if r["matched"] == r["total"] else EXIT_WARN


# --------------------------------------------------------------------------- #
# rollback
# --------------------------------------------------------------------------- #
def cmd_rollback(a: argparse.Namespace) -> int:
    plan = load_plan(a.plan)
    try:
        if plan["stage"] not in ("applied", "awaiting_override_approval"):
            raise Refused(f"plan is {plan['stage']}; nothing to roll back")
        env = Env.from_plan(plan)
        key = "applied" if plan["stage"] == "applied" else "segment_stage"
        if not a.force:
            try:
                _check_fresh(env, plan, key)
            except Refused as e:
                raise Refused(f"{e} (someone changed it after apply; --force rolls back anyway)")
        doc, _ = current_document(env, plan["flag"])
        m, flag, before = plan["mutation"], plan["flag"], plan["rollback"]["before"]
        plan["writes"] = []
        before_doc = json.loads(snapshot_path(plan, "before").read_text())
        if m["op"] != "percentage_rollout" and not _diff_paths(semantic(before_doc), semantic(doc)):
            print("  (already in the pre-plan state; nothing to write)", file=sys.stderr)
        elif m["op"] == "env_default":
            restore = {k: before["env_default"]["enabled" if k == "enabled" else "value"] for k in ("enabled", "value") if k in m}
            if "value" in restore and restore["value"] is None:
                raise Refused("cannot restore a null value through the CLI; restore it in the dashboard")
            _write(env, plan, "flag", "update", flag, *_update_args(restore))
        elif m["op"] == "segment_override":
            seg = _segment(doc, m["segment"])
            old = next((o for o in before["overrides"] if o["segment_id"] == seg["id"]), None)
            if old is None:
                _write(env, plan, "flag", "delete", flag, "--segment", str(seg["id"]))
            else:
                restore = {k: old["enabled" if k == "enabled" else "value"] for k in ("enabled", "value") if k in m}
                if "priority" in m:
                    restore["priority"] = old["priority"]
                _write(env, plan, "flag", "update", flag, "--segment", str(seg["id"]), *_update_args(restore))
        elif m["op"] == "mv_weights":
            if m.get("segment") is None:
                old_w = before["env_default"]["weights"]
                seg = []
            else:
                sid = _segment(doc, m["segment"])["id"]
                old_w = next(o for o in before["overrides"] if o["segment_id"] == sid)["weights"]
                seg = ["--segment", str(sid)]
            _write(env, plan, "flag", "update", flag, *seg, "--weight", ",".join(f"{k}={old_w[k]:g}" for k in m["weights"]))
        elif m["op"] == "percentage_rollout":
            sid = str(plan["created_segment"]["id"])
            if plan["stage"] == "applied":
                _write(env, plan, "flag", "delete", flag, "--segment", sid)
            cli("segment", "delete", sid, "-p", env.project, write=True)
            plan["writes"].append(f"segment delete {sid}")

        after, _ = current_document(env, plan["flag"], newer_than=doc.get("updated_at") if plan["writes"] else None)
        diff = _diff_paths(semantic(before_doc), semantic(after))
        r = compare_remote(env, plan["sample"], "before")
        plan["stage"] = "rolled_back"
        plan["rollback_result"] = {"at": now(), "semantic_diff": diff, "remote": r}
        ok = not diff and r["matched"] == r["total"]
        history(plan, "rollback", "restored" if ok else "restored_with_differences",
                writes=plan["writes"], semantic_diffs=len(diff), score=f"{r['matched']}/{r['total']}")
        text = (f"rolled back {env.name}: {'; '.join(plan['writes'])}\n"
                f"document vs pre-plan: {'IDENTICAL (ignoring version uuids/updated_at)' if not diff else f'{len(diff)} differences'}\n"
                + "".join(f"  {d}\n" for d in diff[:5])
                + f"remote sample matches pre-change prediction: {r['matched']}/{r['total']}")
        emit(plan["rollback_result"], text, a.json)
        return EXIT_SAFE if ok else EXIT_WARN
    except Refused as e:
        history(plan, "rollback", "refused", reason=str(e), writes=plan.get("writes", []))
        print(f"REFUSED: {e}", file=sys.stderr)
        return EXIT_FAIL


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="preflight.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="simulate a change (read-only) and save a plan")
    p.add_argument("--env", required=True)
    p.add_argument("--flag", required=True)
    p.add_argument("--change", required=True)
    p.add_argument("--identities", type=int, help="population size (default 1000 synthetic, 100 from API)")
    p.add_argument("--from-api", action="store_true", help="use identities stored in the environment")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--risk-pct", type=float, default=50, help="warn when this share of identities changes")

    p = sub.add_parser("apply", help="apply a saved plan")
    p.add_argument("--plan", required=True)
    p.add_argument("--confirm", required=True, help="the exact environment name")
    p.add_argument("--override-experiment", metavar="REASON")
    p.add_argument("--approve-override", action="store_true", help="second approval for a rollout's override")

    for name in ("verify", "rollback"):
        p = sub.add_parser(name)
        p.add_argument("--plan", required=True)
        if name == "rollback":
            p.add_argument("--force", action="store_true")

    for p in sub.choices.values():
        p.add_argument("--project", default=os.environ.get("FLAGSMITH_PROJECT"))
        p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "plan" and a.identities is None:
        a.identities = 100 if a.from_api else 1000
    try:
        if a.cmd == "plan" and not a.project:
            projects = cli_json("project", "list")
            if len(projects) != 1:
                raise Refused("several projects visible; pass --project or set FLAGSMITH_PROJECT: "
                              + ", ".join(p["name"] for p in projects))
            a.project = projects[0]["name"]
        return {"plan": cmd_plan, "apply": cmd_apply, "verify": cmd_verify, "rollback": cmd_rollback}[a.cmd](a)
    except (Refused, ValueError) as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
