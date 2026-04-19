#!/usr/bin/env python3
"""Federation-wide meta-learning cycle.

Runs brain.meta_learning.execute_run on every gateway's state.db
(main + 5 apostles). The run automatically chains into
evolution_architect.generate_proposals for each affected task family,
so one invocation advances the full findings -> proposals pipeline
across the federation.

Intended to be fired by the hermes-meta-learning-cycle.timer on a
regular cadence (daily) so the evolution loop moves forward without
manual triggering. Also safe to run ad-hoc.

Exit code is 0 even on per-gateway errors — missing state.dbs or
analyzer failures are logged to stderr and the remaining gateways
still process.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_state import SessionDB  # noqa: E402
from brain import meta_learning  # noqa: E402


GATEWAYS = {
    "main": Path.home() / ".hermes" / "state.db",
    "ocean": Path.home() / "zeus" / "agents" / "ocean" / "eos" / "state.db",
    "eleven": Path.home() / "zeus" / "agents" / "eleven" / "eos" / "state.db",
    "wilson": Path.home() / "zeus" / "agents" / "wilson" / "eos" / "state.db",
    "susan": Path.home() / "zeus" / "agents" / "susan" / "eos" / "state.db",
    "crypto": Path.home() / "zeus" / "agents" / "crypto" / "eos" / "state.db",
}

# 30-day window — long enough for weekly cycles to see multiple runs,
# short enough to keep findings fresh.
WINDOW_SECONDS = 30 * 86400


def run_cycle() -> dict:
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    print(f"Hermes federation meta-learning cycle — {timestamp}")
    print("=" * 72)
    print(
        f"{'gw':8s} {'tasks':>6s} {'findings':>9s} {'types':>6s} "
        f"{'new_props':>10s}  result"
    )
    results = {}
    grand_new = 0
    for name, path in GATEWAYS.items():
        if not path.exists():
            print(f"{name:8s} (no state.db at {path})")
            continue
        try:
            db = SessionDB(path)
            before = db._conn.execute(
                "SELECT COUNT(*) FROM capability_proposals"
            ).fetchone()[0]
            res = meta_learning.execute_run(
                db, scope_type="global", window_seconds=WINDOW_SECONDS,
            )
            after = db._conn.execute(
                "SELECT COUNT(*) FROM capability_proposals"
            ).fetchone()[0]
            new_props = after - before
            grand_new += new_props
            n_types = len({f.get("type") for f in res.get("findings", [])})
            print(
                f"{name:8s} {res['tasks_analyzed']:>6d} "
                f"{len(res.get('findings', [])):>9d} {n_types:>6d} "
                f"{new_props:>+10d}  run_id={res['run_id']}"
            )
            results[name] = {
                "run_id": res["run_id"],
                "tasks": res["tasks_analyzed"],
                "findings": len(res.get("findings", [])),
                "types": n_types,
                "new_proposals": new_props,
                "proposals_created": res.get("proposals_created", []),
            }
        except Exception as exc:
            print(f"{name:8s} ERROR: {exc}", file=sys.stderr)
            results[name] = {"error": str(exc)}

    print()
    print(f"Cycle complete: {grand_new} new proposal(s) across federation.")

    # Show new proposals concisely
    if grand_new:
        print()
        print("New proposals this cycle:")
        for name, path in GATEWAYS.items():
            r = results.get(name, {})
            pids = r.get("proposals_created", [])
            if not pids:
                continue
            try:
                db = SessionDB(path)
                for pid in pids:
                    row = db._conn.execute(
                        """SELECT id, proposal_type, target_task_family,
                                  title, proposal_json
                           FROM capability_proposals WHERE id = ?""",
                        (pid,),
                    ).fetchone()
                    if not row:
                        continue
                    rd = dict(row)
                    pj = {}
                    try:
                        pj = json.loads(rd.get("proposal_json") or "{}")
                    except Exception:
                        pass
                    hint = pj.get("action_hint", {}).get("kind", "?")
                    print(
                        f"  [{name:7s}] {rd['id']} "
                        f"[{rd['proposal_type']:22s}] fam={rd['target_task_family']:8s} "
                        f"hint={hint}"
                    )
            except Exception as exc:
                print(f"  [{name:7s}] (listing failed: {exc})", file=sys.stderr)

    return results


def main() -> int:
    run_cycle()
    return 0


if __name__ == "__main__":
    sys.exit(main())
