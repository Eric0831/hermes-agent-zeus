#!/usr/bin/env python3
"""Seed the ZEUS federation clusters into agent_clusters.

Idempotent: re-running is safe and will only create clusters that are
missing. Invoke standalone for bootstrapping or re-seeding after a
state.db reset; gateway startup can also call brain.agent_society.
seed_federation(db) directly.

Usage:
    ./venv/bin/python scripts/seed_federation.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_state import DEFAULT_DB_PATH, SessionDB  # noqa: E402

from brain import agent_society  # noqa: E402


def main() -> int:
    db = SessionDB(DEFAULT_DB_PATH)
    result = agent_society.seed_federation(db)
    print(f"Seeded {len(result)} clusters into {DEFAULT_DB_PATH}:")
    for c in agent_society.get_all_clusters(db, status="active"):
        print(
            f"  {c['cluster_name']:18s} authority={c['authority_level']:13s} "
            f"trust={c['trust_score']:.2f}  {c['jurisdiction_json']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
