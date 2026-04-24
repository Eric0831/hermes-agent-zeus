#!/usr/bin/env python3
"""Read-only precedent hygiene report across the Hermes federation.

Reports which existing task_family precedents would be hidden from
Planner recall by brain.precedent_hygiene. This script does not delete,
update, or migrate any database.

Usage:
    ./scripts/hermes_precedent_hygiene_report.py
    ./scripts/hermes_precedent_hygiene_report.py --json
    ./scripts/hermes_precedent_hygiene_report.py --gateway main --limit 20
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from brain.precedent_hygiene import (  # noqa: E402
    DEFAULT_MIN_EVIDENCE,
    is_clean_precedent_row,
)


GATEWAYS = {
    "main": Path.home() / ".hermes" / "state.db",
    "ocean": Path.home() / "zeus" / "agents" / "ocean" / "eos" / "state.db",
    "eleven": Path.home() / "zeus" / "agents" / "eleven" / "eos" / "state.db",
    "wilson": Path.home() / "zeus" / "agents" / "wilson" / "eos" / "state.db",
    "susan": Path.home() / "zeus" / "agents" / "susan" / "eos" / "state.db",
    "crypto": Path.home() / "zeus" / "agents" / "crypto" / "eos" / "state.db",
}


def _connect_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def collect_gateway(
    gateway: str,
    path: Path,
    *,
    min_evidence: int,
    sample_limit: int,
) -> dict[str, Any]:
    if not path.exists():
        return {"gateway": gateway, "present": False, "path": str(path)}

    try:
        conn = _connect_ro(path)
    except Exception as exc:
        return {
            "gateway": gateway,
            "present": False,
            "path": str(path),
            "error": str(exc),
        }

    try:
        rows = conn.execute(
            """SELECT id, precedent_type, subject_type, subject_id,
                      decision_json, binding_strength, created_at
               FROM precedent_records
               WHERE subject_type = 'task_family'
               ORDER BY created_at DESC"""
        ).fetchall()
    except Exception as exc:
        conn.close()
        return {
            "gateway": gateway,
            "present": False,
            "path": str(path),
            "error": str(exc),
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass

    total = len(rows)
    clean = 0
    rejected = 0
    reason_counts: Counter[str] = Counter()
    family_counts: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []

    for row in rows:
        rd = dict(row)
        ok, reason = is_clean_precedent_row(rd, min_evidence=min_evidence)
        family = str(rd.get("subject_id") or "unknown")
        if ok:
            clean += 1
            continue
        rejected += 1
        reason_counts[reason] += 1
        family_counts[family] += 1
        if len(samples) < sample_limit:
            decision = _load_json(rd.get("decision_json"))
            samples.append({
                "id": rd.get("id"),
                "family": family,
                "reason": reason,
                "binding_strength": float(rd.get("binding_strength") or 0.0),
                "evidence_count": _as_int(decision.get("evidence_count")),
                "goal": _compact(str(decision.get("goal") or ""), 120),
                "created_at": _fmt_time(rd.get("created_at")),
            })

    return {
        "gateway": gateway,
        "present": True,
        "path": str(path),
        "total_task_family_precedents": total,
        "clean": clean,
        "rejected": rejected,
        "rejected_pct": round(100.0 * rejected / total, 1) if total else 0.0,
        "reason_counts": dict(reason_counts.most_common()),
        "family_counts": dict(family_counts.most_common()),
        "samples": samples,
    }


def collect_all(args: argparse.Namespace) -> dict[str, Any]:
    gateways = args.gateway or list(GATEWAYS.keys())
    results = {
        name: collect_gateway(
            name,
            GATEWAYS[name],
            min_evidence=args.min_evidence,
            sample_limit=args.limit,
        )
        for name in gateways
    }
    totals = Counter()
    reason_totals: Counter[str] = Counter()
    for item in results.values():
        if not item.get("present"):
            continue
        totals["total"] += int(item.get("total_task_family_precedents") or 0)
        totals["clean"] += int(item.get("clean") or 0)
        totals["rejected"] += int(item.get("rejected") or 0)
        reason_totals.update(item.get("reason_counts") or {})

    return {
        "timestamp": time.time(),
        "timestamp_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "min_evidence": args.min_evidence,
        "sample_limit": args.limit,
        "totals": {
            "total_task_family_precedents": totals["total"],
            "clean": totals["clean"],
            "rejected": totals["rejected"],
            "rejected_pct": (
                round(100.0 * totals["rejected"] / totals["total"], 1)
                if totals["total"] else 0.0
            ),
            "reason_counts": dict(reason_totals.most_common()),
        },
        "gateways": results,
    }


def format_text(snapshot: dict[str, Any]) -> str:
    totals = snapshot["totals"]
    lines = [
        f"Hermes Precedent Hygiene Report  {snapshot['timestamp_iso']}",
        "=" * 80,
        (
            f"min_evidence={snapshot['min_evidence']}  "
            "mode=read-only/no-delete"
        ),
        "",
        (
            "Federation totals: "
            f"total={totals['total_task_family_precedents']} "
            f"clean={totals['clean']} rejected={totals['rejected']} "
            f"({totals['rejected_pct']:.1f}%)"
        ),
    ]
    if totals["reason_counts"]:
        reason_text = ", ".join(
            f"{reason}={count}"
            for reason, count in totals["reason_counts"].items()
        )
        lines.append(f"Reject reasons: {reason_text}")
    lines.append("")

    for name, item in snapshot["gateways"].items():
        if not item.get("present"):
            lines.append(f"{name:8s} skipped: {item.get('error') or 'state.db not found'}")
            continue
        lines.append(
            f"{name:8s} total={item['total_task_family_precedents']:>4d} "
            f"clean={item['clean']:>4d} rejected={item['rejected']:>4d} "
            f"({item['rejected_pct']:>5.1f}%)"
        )
        if item["reason_counts"]:
            reason_text = ", ".join(
                f"{reason}={count}"
                for reason, count in item["reason_counts"].items()
            )
            lines.append(f"  reasons: {reason_text}")
        if item["family_counts"]:
            family_text = ", ".join(
                f"{family}={count}"
                for family, count in list(item["family_counts"].items())[:5]
            )
            lines.append(f"  families: {family_text}")
        for sample in item["samples"]:
            lines.append(
                f"  - {sample['id']} [{sample['family']}] "
                f"{sample['reason']} evidence={sample['evidence_count']} "
                f"bind={sample['binding_strength']:.2f}"
            )
            if sample["goal"]:
                lines.append(f"    {sample['goal']}")
        lines.append("")

    lines.append("This report only shows precedents hidden from Planner recall; it does not delete records.")
    return "\n".join(lines)


def _load_json(raw: Any) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _compact(text: str, limit: int) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)] + "..."


def _fmt_time(ts: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts or 0)))
    except Exception:
        return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true",
                        help="emit machine-readable JSON")
    parser.add_argument("--gateway", action="append", choices=sorted(GATEWAYS),
                        help="gateway to scan; repeatable; default all")
    parser.add_argument("--limit", type=int, default=8,
                        help="max rejected samples to show per gateway")
    parser.add_argument("--min-evidence", type=int, default=DEFAULT_MIN_EVIDENCE)
    args = parser.parse_args()

    snapshot = collect_all(args)
    if args.json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str))
    else:
        print(format_text(snapshot))
    return 0


if __name__ == "__main__":
    sys.exit(main())
