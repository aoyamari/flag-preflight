"""Flag Preflight test suite.

    python tests/cases.py            # engine cases + read-only CLI cases (no writes)
    python tests/cases.py --live     # also edge cases that write to Development, then restore

Run from the repository root with credentials loaded (`set -a; source .env; set +a`).
Expected counts are for the preflight-demo fixture (seed 42, 1000 synthetic identities).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "flag-preflight" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import preflight as P  # noqa: E402
import simulate as S  # noqa: E402

PROJECT = os.environ.get("FLAGSMITH_PROJECT", "preflight-demo")
DEV = "Development"
IDS = S.synthetic_identities(1000, 42)
RESULTS: list[tuple[str, bool, str]] = []
doc_cache: dict = {}


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not ok else ""))
    return bool(ok)


def preflight(*args: str, state: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(SCRIPTS / "preflight"), *args, "--project", PROJECT] if args[0] == "plan"
                          else [str(SCRIPTS / "preflight"), *args],
                          capture_output=True, text=True, cwd=ROOT, env={**os.environ, "PREFLIGHT_DIR": state})


# --------------------------------------------------------------------------- #
# 1. Engine: scenarios a–d
# --------------------------------------------------------------------------- #
SCENARIOS = {
    "a1 env default on": ({"op": "env_default", "feature": "new_checkout", "enabled": True}, 892),
    "a2 env default value v2": ({"op": "env_default", "feature": "new_checkout", "value": "v2"}, 892),
    "b1 beta_users override off": ({"op": "segment_override", "feature": "new_checkout", "segment": "beta_users", "enabled": False}, 108),
    "b2 new override shadowed by experiment": ({"op": "segment_override", "feature": "checkout_layout", "segment": "beta_users", "enabled": False}, 0),
    "b3 new override at priority 0": ([
        {"op": "segment_override", "feature": "checkout_layout", "segment": "experiment-534-rollout", "priority": 1},
        {"op": "segment_override", "feature": "checkout_layout", "segment": "beta_users", "enabled": False, "priority": 0}], 108),
    "c  20% rollout": ({"op": "percentage_rollout", "feature": "new_checkout", "percentage": 20}, (150, 210)),
    "d1 env default weights shadowed": ({"op": "mv_weights", "feature": "checkout_layout", "weights": {"compact": 50}}, 0),
    "d2 experiment segment weights 50->70": ({"op": "mv_weights", "feature": "checkout_layout", "segment": "experiment-534-rollout", "weights": {"compact": 70}}, (170, 230)),
}


def engine_cases(doc: dict) -> None:
    print("engine: scenarios a–d")
    for name, (m, want) in SCENARIOS.items():
        r = S.simulate(doc, m, IDS)
        got = r["changed_identities"]
        ok = (want[0] <= got <= want[1]) if isinstance(want, tuple) else got == want
        if want == 0:
            ok = ok and any(e["reason"] == "shadowed" and "experiment-534-rollout (priority 0)" in e["shadowed_by"]
                            for e in r["no_effect"])
        check(f"{name}: {got} changed", ok, f"expected {want}, no_effect={r['no_effect']}")

    print("engine: multivariate value (not weights)")
    before = S.evaluate(doc, IDS)
    control = sum(before[i]["checkout_layout"][1] == "control" for i, _ in IDS)
    compact = sum(before[i]["checkout_layout"][1] == "compact" for i, _ in IDS)
    r = S.simulate(doc, {"op": "segment_override", "feature": "checkout_layout",
                         "segment": "experiment-534-rollout", "value": "classic"}, IDS)
    t = r["features"].get("checkout_layout", {}).get("transitions", {})
    check(f"control value -> classic moves exactly the {control} control users",
          t == {'on:"control" -> on:"classic"': control}, f"transitions={t}")
    check(f"the {compact} users on variant 'compact' are untouched",
          compact > 0 and not any(k.startswith('on:"compact"') for k in t), f"transitions={t}")
    r = S.simulate(doc, {"op": "env_default", "feature": "checkout_layout", "value": "classic"}, IDS)
    check("env default value change is shadowed (0, NO_EFFECT explained)",
          r["changed_identities"] == 0 and r["no_effect"] and r["no_effect"][0]["reason"] == "shadowed",
          f"changed={r['changed_identities']} no_effect={r['no_effect']}")


# --------------------------------------------------------------------------- #
# 2. CLI, read-only: plan exit codes and error messages
# --------------------------------------------------------------------------- #
ERRORS = [
    (["plan", "--env", "Staging", "--flag", "new_checkout", "--change", "enabled=true"], r"environment 'Staging' not found .*have: .*Development"),
    (["plan", "--env", DEV, "--flag", "nope", "--change", "enabled=true"], r"flag 'nope' not found in Development .*new_checkout"),
    (["plan", "--env", DEV, "--flag", "new_checkout", "--change", "segment:nope:enabled=true"], r"segment 'nope' not found .*beta_users"),
    (["plan", "--env", DEV, "--flag", "new_checkout", "--change", "foo"], r"unrecognised change spec 'foo'.*enabled=true\|false"),
    (["plan", "--env", DEV, "--flag", "new_checkout", "--change", "enabled=maybe"], r"expected true or false, got 'maybe'"),
    (["plan", "--env", DEV, "--flag", "new_checkout", "--change", "rollout:0"], r"rollout percentage must be in \(0, 100\]"),
    (["plan", "--env", DEV, "--flag", "new_checkout", "--change", "rollout:abc"], r"rollout percentage must be a number"),
    (["plan", "--env", DEV, "--flag", "checkout_layout", "--change", "weight:compact"], r"bad weight 'compact'"),
    (["plan", "--env", DEV, "--flag", "checkout_layout", "--change", "weight:compact=lots"], r"bad weight 'compact=lots'"),
    (["plan", "--env", DEV, "--flag", "checkout_layout", "--change", "weight:nope=10"], r"variant 'nope' not found .*compact"),
    (["plan", "--env", DEV, "--flag", "checkout_layout", "--change", "weight:compact=120"], r"weights sum to 120"),
    (["plan", "--env", DEV, "--flag", "new_checkout", "--change", "weight:compact=10"], r"'new_checkout' is not multivariate"),
    (["plan", "--env", DEV, "--flag", "new_checkout", "--change", "segment:beta_users:color=red"], r"unknown segment field 'color'"),
    (["apply", "--plan", "does-not-exist", "--confirm", DEV], r"no plan 'does-not-exist'"),
    (["verify", "--plan", "does-not-exist"], r"no plan 'does-not-exist'"),
]


def cli_cases(env: "P.Env", state: str) -> None:
    print("cli: errors are clear, exit 1, no traceback")
    for args, rx in ERRORS:
        p = preflight(*args, state=state)
        err = p.stderr.strip()
        ok = p.returncode == 1 and err.startswith("REFUSED:") and "Traceback" not in err and re.search(rx, err)
        check(f"{' '.join(args[3:] if args[0] == 'plan' else args)} -> exit {p.returncode}", ok, err[:200])

    print("cli: plan exit codes")
    p = preflight("plan", "--env", DEV, "--flag", "checkout_layout", "--change", "segment:experiment-534-rollout:value=classic", state=state)
    check("MV value change on experiment flag -> exit 3 EXPERIMENT_ACTIVE", p.returncode == 3 and "EXPERIMENT_ACTIVE" in p.stdout, p.stdout[-300:])
    p = preflight("plan", "--env", DEV, "--flag", "checkout_layout", "--change", "segment:beta_users:enabled=false", state=state)
    check("b2 -> exit 3 with NO_EFFECT 'loses on priority'", p.returncode == 3 and "loses on priority" in p.stdout, p.stdout[-300:])

    print("cli: identity overrides (the Admin API document omits them; preflight rebuilds them)")
    real = P.cli_json
    beta = next(i for i, t in IDS if t["plan"] == "beta")
    P.cli_json = lambda *a: ([{"feature": "new_checkout", "identifier": beta, "enabled": False, "value": None}]
                             if "--identities" in a and "new_checkout" in a else [] if "--identities" in a else real(*a))
    try:
        stub = P.with_identity_overrides(env, {**json.loads(json.dumps(doc_cache["doc"])), "identity_overrides": []})
        ev = S.evaluate(stub, [(beta, {"plan": "beta"})])
        r = S.simulate(stub, {"op": "segment_override", "feature": "new_checkout", "segment": "beta_users", "enabled": False}, IDS)
        check(f"stubbed override: {beta} (beta) evaluates off; beta_users off moves 107",
              ev[beta]["new_checkout"][0] is False and r["changed_identities"] == 107, f"{ev} changed={r['changed_identities']}")
        check("rebuilt overrides hash the same on every fetch", P.doc_hash(stub) == P.doc_hash(P.with_identity_overrides(
            env, {**json.loads(json.dumps(doc_cache["doc"])), "identity_overrides": []})))
    finally:
        P.cli_json = real

    print("cli: --from-api")
    real = P.cli_json
    P.cli_json = lambda *a: {"results": []} if "edge-identities" in a[1] else real(*a)  # no stored identities
    try:
        P.identities_from_api(env, 10)
        check("no stored identities -> refused", False, "no exception")
    except P.Refused as e:
        check("no stored identities -> refused with a pointer to --identities", "no identities stored in Development" in str(e)
              and "--identities" in str(e), str(e))
    finally:
        P.cli_json = real
    p = preflight("plan", "--env", DEV, "--flag", "new_checkout", "--change", "enabled=true", "--from-api", "--json", state=state)
    ok = p.returncode in (0, 2) and json.loads(p.stdout)["simulation"]["source"] == "stored"
    check("--from-api with stored identities plans on them", ok, (p.stderr or p.stdout)[-200:])


# --------------------------------------------------------------------------- #
# 3. Live (Development only): identity override beats segment; two plans, one flag
# --------------------------------------------------------------------------- #
def _wait_doc(env: "P.Env", pred, what: str, timeout: float = 180) -> dict:
    end = time.time() + timeout
    while True:
        doc = env.document()
        if pred(doc):
            return doc
        if time.time() > end:
            raise AssertionError(f"document never showed: {what}")
        time.sleep(5)


def _identity_uuid(env: "P.Env", identifier: str) -> str | None:
    res = P.cli_json("api", f"api/v1/environments/{env.key}/edge-identities/?q={identifier}")
    return next((i["identity_uuid"] for i in res.get("results", []) if i["identifier"] == identifier), None)


def live_identity_override(env: "P.Env", state: str) -> None:
    print("live: identity override beats segment (Development)")
    ident = next(i for i, t in IDS if t["plan"] == "beta")  # a beta user: segment says ON
    other_beta = [x for x in IDS if x[1]["plan"] == "beta" and x[0] != ident][0]
    if _identity_uuid(env, ident):
        check(f"precondition: no stored identity {ident}", False, "already exists; refusing to touch it")
        return
    has = lambda d: ident in json.dumps(d.get("identity_overrides") or [])  # noqa: E731
    try:
        P.cli("flag", "disable", "new_checkout", "--identifier", ident, *env.args(), write=True)
        doc = _wait_doc(env, has, f"identity override for {ident}")
        ev = S.evaluate(doc, [(ident, {"plan": "beta"}), other_beta])
        check(f"offline: {ident} (beta, override off) -> off; other beta -> on",
              ev[ident]["new_checkout"][0] is False and ev[other_beta[0]]["new_checkout"][0] is True, str(ev))
        remote = P.remote_eval(env, ident, {"plan": "beta"})
        check("remote transient eval agrees (off)", remote["new_checkout"][0] is False, str(remote))
        r = S.simulate(doc, {"op": "segment_override", "feature": "new_checkout", "segment": "beta_users", "enabled": False}, IDS)
        check("beta_users override off now moves 107 (the overridden identity is already off)",
              r["changed_identities"] == 107, f"changed={r['changed_identities']}")
        r = S.simulate(doc, {"op": "env_default", "feature": "new_checkout", "enabled": True}, IDS)
        check("env default on: 892 change, overridden identity not among them",
              r["changed_identities"] == 892 and ident not in json.dumps(r["samples"]), f"changed={r['changed_identities']}")
        p = preflight("plan", "--env", DEV, "--flag", "new_checkout", "--change", "segment:beta_users:enabled=false", "--json", state=state)
        check("preflight plan reports 107", p.returncode in (0, 2) and json.loads(p.stdout)["simulation"]["changed_identities"] == 107,
              (p.stderr or p.stdout)[-200:])
    finally:
        try:
            P.cli("flag", "delete", "new_checkout", "--identifier", ident, *env.args(), write=True)
        except P.Refused as e:
            print(f"  (cleanup: {e})")
        uid = _identity_uuid(env, ident)
        if uid:
            P.cli("api", f"api/v1/environments/{env.key}/edge-identities/{uid}/", "-X", "DELETE", write=True)
        _wait_doc(env, lambda d: not has(d), f"identity override for {ident} removed")
        check(f"cleanup: identity {ident} and its override are gone", _identity_uuid(env, ident) is None)


def live_two_plans(env: "P.Env", state: str) -> None:
    print("live: two plans for one flag (Development)")
    a = json.loads(preflight("plan", "--env", DEV, "--flag", "new_checkout", "--change", "enabled=true", "--json", state=state).stdout)["plan_id"]
    b = json.loads(preflight("plan", "--env", DEV, "--flag", "new_checkout", "--change", "enabled=true", "--json", state=state).stdout)["plan_id"]
    applied = preflight("apply", "--plan", a, "--confirm", DEV, state=state)
    try:
        check("first plan applies", applied.returncode == 0, applied.stderr[-200:] + applied.stdout[-200:])
        p = preflight("apply", "--plan", b, "--confirm", DEV, state=state)
        check("second plan is REFUSED (new live version)", p.returncode == 1 and "new live version" in p.stderr, p.stderr[-200:])
        p = preflight("verify", "--plan", a, state=state)
        check("verify first plan", p.returncode == 0 and "30/30" in p.stdout, p.stdout[-200:])
    finally:
        if applied.returncode == 0:
            p = preflight("rollback", "--plan", a, state=state)
            check("rollback restores (IDENTICAL, 30/30)", p.returncode == 0 and "IDENTICAL" in p.stdout and "30/30" in p.stdout,
                  p.stderr[-200:] + p.stdout[-200:])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="run cases that write to Development (restored)")
    a = ap.parse_args()
    env = P.Env(PROJECT, DEV)
    doc, _ = P.current_document(env, "new_checkout")
    doc_cache["doc"] = doc
    start = P.semantic(doc)
    state = tempfile.mkdtemp(prefix="preflight-tests-")
    try:
        engine_cases(doc)
        cli_cases(env, state)
        if a.live:
            live_identity_override(env, state)
            live_two_plans(env, state)
            end = _wait_doc(env, lambda d: P.semantic(d) == start, "pre-test state", timeout=200)
            check("Development is back to its pre-test state", P.semantic(end) == start)
    except Exception:
        check("suite ran to completion", False, traceback.format_exc()[-400:])
    passed = sum(ok for _, ok, _ in RESULTS)
    print(f"\n{passed}/{len(RESULTS)} passed" + ("" if a.live else " (offline + read-only; --live adds Development write cases)"))
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
