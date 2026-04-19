# Phase E Runbook — AgentEOS Evolution Loop

**Scope.** This runbook is the operational reference for the Phase E
(meta-learning → proposals → capability_versions → action_hints)
pipeline added in the 2026-04-19 federation upgrade. Read
[`GATEWAY_OPERATIONS_RUNBOOK.md`](./GATEWAY_OPERATIONS_RUNBOOK.md)
first — everything here assumes the gateway lifecycle is already
understood.

Audience: operators driving the Hermes federation day-to-day. Phase
E is designed to run unattended for days at a time, but you still
need to know where the decision points are.

---

## 1. The Loop at a Glance

```
Gateway message
    │
    ▼ Executive.triage
direct_reply  ◀──┐
    │            │
    ▼ Planner (with world_state + precedents)
plan with success_criteria + subtasks
    │
    ▼ Agent loop (existing run_agent + tool dispatch)
    │   ├─ pre-dispatch: brain.policy.evaluate (audit-mode)
    │   └─ evidence_records written with resolved tool_name
    │
    ▼ Verifier (heuristic + optional LLM)
completed / failed / fail_retriable
    │
    ▼ brain-evolution hook (session:end)
meta_learning.execute_run
    │  ├─ findings: family_performance, fast_family, evidence_rich,
    │  │            high_performing_tool, low_verification_rate,
    │  │            high_retry_rate, ...
    │  └─ auto chain into evolution_architect.generate_proposals
    ▼
capability_proposals (status=proposed, carries action_hint)
    │
    ▼ hermes-governance-auto (daily 04:15)
SAFE kinds + risk<=0.20 + gain>=0.30 auto-approve
    │
    ▼ capability_manager.promote_from_proposal
capability_versions (status=incubating)
    │
    ▼ /tasks experiment <vid>  (operator)
capability_versions (status=experimental)
    │
    ▼ /tasks execute <vid>  (operator; 10/10 kinds supported)
action_hint executor writes to the appropriate sink:
    extract_skill              → skill_registry (candidate)
    extract_precedents         → precedent_records
    update_recommended_tools   → doctrine_registry (proposed)
    update_planner_prompt      \
    update_planner_examples     |
    toggle_verifier_llm         |
    add_verifier_criterion_type  > planner_policies (is_active=0)
    add_tool_fallback           |
    increase_retry_budget       |
    increase_task_budget       /
    │
    ▼ /tasks rollout <vid>  (operator)  → limited_rollout
    ▼ /tasks adopt   <vid>  (operator)  → adopted (atomic swap)
    │
    └─ Side loops:
       • hermes-trust-update (daily 04:30)   — nudges
         agent_clusters.trust_score from 7d completion rate
       • hermes-skill-auto-promote (daily 04:45) — candidates with
         usage>=5 and success>=0.7 → active
       • hermes-audit-report (every 6h) — snapshot + diff
```

---

## 2. Federation Timer Table

All timers live at `~/.config/systemd/user/` with reference copies
in [`deploy/systemd/`](../deploy/systemd/). Enable with
`systemctl --user enable --now <timer>`.

| Time | Timer | Role |
|------|-------|------|
| 03:20 | `hermes-memory-v2-consolidate` | L0-L3 memory rollup (legacy) |
| 03:32 | `hermes-meta-learning-cycle` | 6-gateway meta_learning → findings → proposals |
| 04:15 | `hermes-governance-auto` | Low-risk proposals → incubating (gated) |
| 04:30 | `hermes-trust-update` | 7-day completion rate → trust_score delta |
| 04:45 | `hermes-skill-auto-promote` | Candidate skills → active (gated) |
| 04:19 / every 6h | `hermes-audit-report` | Snapshot federation state + diff |

Check the calendar: `systemctl --user list-timers | grep hermes-`

---

## 3. Operator CLI Reference (`/tasks <subcommand>`)

Brain-tracked surface exposed on every gateway.

### Observation

| Command | Purpose |
|---------|---------|
| `/tasks metrics` | Brain KPIs (tasks, verify rate, retry rate, ...) |
| `/tasks world` | Current session world_state summary |
| `/tasks clusters` | Federation roster + trust_score |
| `/tasks governance [limit]` | Recent governance_reviews (auto + manual) |
| `/tasks capabilities` | capability_versions status counts |
| `/tasks doctrines` | Active routing / policy doctrines |

### Proposals

| Command | Purpose |
|---------|---------|
| `/tasks propose [family]` | Manually generate proposals for a family |
| `/tasks proposals [family] [status]` | List proposals (default status=proposed) |
| `/tasks proposal <id>` | Detail — title, risk, gain, suggestion, action_hint |
| `/tasks approve <id>` | proposed → approved |
| `/tasks reject <id> [reason]` | → rejected |
| `/tasks incubate <id>` | approved → incubating (creates capability_version) |

### Capability Lifecycle

| Command | Purpose |
|---------|---------|
| `/tasks versions [status]` | List capability_versions |
| `/tasks experiment <vid>` | incubating → experimental |
| `/tasks execute <vid>` | Run the version's action_hint (10/10 kinds) |
| `/tasks rollout <vid>` | experimental → limited_rollout |
| `/tasks adopt <vid>` | limited_rollout → adopted (atomic swap) |
| `/tasks deprecate <vid> [reason]` | Any stage → deprecated |

### Evolution / Reflection

| Command | Purpose |
|---------|---------|
| `/tasks evolve` | Run meta_learning on this session NOW |
| `/tasks proactive` | Scan for proactive nudge signals |
| `/tasks reflect [family]` | Recursive reflection on a family |

---

## 4. Running a Proposal End-to-End (the usual path)

Typical flow when an operator reviews proposals manually:

```text
1. See what's pending:
   /tasks proposals

2. Inspect one:
   /tasks proposal cprop_…

   The response shows title, risk_score, expected_gain, plus a
   human-readable "suggestion" and a structured "action_hint".

3. If you agree with the suggestion:
   /tasks approve cprop_…            # proposed → approved
   /tasks incubate cprop_…           # creates capability_version in incubating

4. (Alternative short-circuit) wait for 04:15 daily auto-governance
   which does steps 3 for you iff:
     - risk_score ≤ 0.20
     - expected_gain ≥ 0.30
     - action_hint.kind is in SAFE_KINDS (extract_skill,
       extract_precedents, update_recommended_tools)

5. When ready to actually apply:
   /tasks experiment capv_…          # incubating → experimental
   /tasks execute   capv_…           # runs the executor

   Executors never activate anything automatically — they write
   to candidate / proposed / is_active=0 rows. Humans ratify.

6. Test the effect in practice (could be 1 day, could be 1 week).

7. Promote through the rest of the lifecycle:
   /tasks rollout capv_…             # experimental → limited_rollout
   /tasks adopt   capv_…             # → adopted (atomic swap)

8. If the experiment didn't pan out at any stage:
   /tasks deprecate capv_… "reason"
```

---

## 5. Manual Override Cheatsheet

Direct SQL or Python for surgical fixes. Use sparingly.

```bash
# Inspect a task's transitions
./venv/bin/python -c "
from hermes_state import SessionDB, DEFAULT_DB_PATH
db = SessionDB(DEFAULT_DB_PATH)
for r in db._conn.execute(\"\"\"SELECT from_state, to_state, reason
    FROM task_transitions WHERE task_id=? ORDER BY id\"\"\", ('task_…',)):
    print(r)
"

# Force-cancel a stuck running task
./venv/bin/python -c "
from hermes_state import SessionDB, DEFAULT_DB_PATH
from brain import task_store
db = SessionDB(DEFAULT_DB_PATH)
task_store.update_task_status(db, 'task_…', 'cancelled',
    reason='operator_force_cancel')
"

# Bulk reject obviously-stale proposals (> 30 days old, still 'proposed')
./venv/bin/python -c "
from hermes_state import SessionDB, DEFAULT_DB_PATH
from brain.evolution_architect import get_proposals, update_proposal_status
import time
db = SessionDB(DEFAULT_DB_PATH)
cutoff = time.time() - 30*86400
for p in get_proposals(db, status='proposed', limit=100):
    if p['created_at'] < cutoff:
        update_proposal_status(db, p['id'], 'rejected',
            reason='stale_>30d_operator_cleanup')
"
```

---

## 6. Audit Trail

Every automated decision writes to `governance_reviews` with a
distinguishing `reviewer_id`:

| reviewer_id | What |
|-------------|------|
| `auto_governance` | Proposal approvals from 04:15 timer |
| `trust_updater` | Trust-score nudges from 04:30 timer |
| `skill_promoter` | Skill promotions from 04:45 timer |
| `operator_*` | Manual CLI actions (approve / reject / etc.) |
| `system` | Default for helper utilities |

Inspect via `/tasks governance 50` or:

```bash
./venv/bin/python -c "
from hermes_state import SessionDB, DEFAULT_DB_PATH
db = SessionDB(DEFAULT_DB_PATH)
for r in db._conn.execute(\"\"\"
    SELECT reviewer_id, decision, COUNT(*)
    FROM governance_reviews
    WHERE created_at >= ?
    GROUP BY reviewer_id, decision
    ORDER BY COUNT(*) DESC
\"\"\", (0,)):
    print(r[0], r[1], r[2])
"
```

---

## 7. Federation Snapshot

```bash
./scripts/hermes_audit_report.py              # one-shot
./scripts/hermes_audit_report.py --snapshot   # save to ~/.hermes/audit/
./scripts/hermes_audit_report.py --diff       # vs latest saved snapshot
./scripts/hermes_audit_report.py --json       # machine-readable
```

Healthy federation targets:

- `stale == 0` everywhere (the `end_session` orphan-cancel fix keeps
  this clean)
- `named% → 90%+` over time (post-c8cb2504 evidence captures real tool
  names)
- `types >= 3` per gateway (meta_learning finding diversity)
- `capv >= prop` for each non-empty gateway (proposals are flowing
  through the lifecycle, not piling up)
- `trust_score` drifting from the 0.500 seed in both directions
  according to real activity

---

## 8. Upstream Sync

Fork is ~2063 commits behind upstream (2026-04-19). Never do a full
merge — use the triage tool:

```bash
./scripts/hermes_upstream_triage.py --limit 200 --min-safe > /tmp/upstream.log
```

Safe workflow:

1. Read `/tmp/upstream.log`, pick a batch of 5-10 `safe` commits.
2. `git cherry-pick <sha1> <sha2> ...`
3. `./scripts/hermes_smoke_test.sh` (Stability V1 gate).
4. If green, keep. If red, `git cherry-pick --abort`.

Avoid cherry-picking `conflict` category — those touch fork-modified
files (`brain/`, `hermes_state.py`, `gateway/run.py`, etc.) and
require manual resolution.

---

## 9. Disabling the Loop

If Phase E needs to be paused (debugging, incident response):

```bash
# Stop just the timers — the gateway keeps serving traffic
systemctl --user disable --now hermes-meta-learning-cycle.timer
systemctl --user disable --now hermes-governance-auto.timer
systemctl --user disable --now hermes-trust-update.timer
systemctl --user disable --now hermes-skill-auto-promote.timer
```

Re-enable with `systemctl --user enable --now <timer>`.

Gateway still functions fully with all timers off — proposals just
don't accumulate and capability_versions don't advance. No data loss.

---

## 10. Recovery

If any single component panics:

- **Gateway startup fails after a cherry-pick**: `git reset --hard
  <known-good-sha>`, `systemctl --user restart hermes-gateway`.
- **state.db schema mismatch**: schema migration is idempotent and
  forward-only. Never edit `schema_version` manually.
- **Runaway auto_governance**: set `--max-risk 0.0` temporarily to
  freeze approvals, then investigate the proposal generator.
- **Bad action_hint execution**: executors only write to gated
  sinks. Roll back by deleting the specific `skill_registry` /
  `precedent_records` / `planner_policies` row. `/tasks deprecate
  <vid>` also records the rollback in governance_reviews.

---

## 11. See Also

- [`phase0-agenteos-brain.md`](./phase0-agenteos-brain.md) — original
  Phase 0 spec.
- [`GATEWAY_OPERATIONS_RUNBOOK.md`](./GATEWAY_OPERATIONS_RUNBOOK.md) —
  gateway lifecycle.
- [`IMPLEMENT.md`](../IMPLEMENT.md) — Stability V1 scope.
- [`DOCUMENTATION.md`](../DOCUMENTATION.md) — Stability V1 changes.
