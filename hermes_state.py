#!/usr/bin/env python3
"""
SQLite State Store for Hermes Agent.

Provides persistent session storage with FTS5 full-text search, replacing
the per-session JSONL file approach. Stores session metadata, full message
history, and model configuration for CLI and gateway sessions.

Key design decisions:
- WAL mode for concurrent readers + one writer (gateway multi-platform)
- FTS5 virtual table for fast text search across all session messages
- Compression-triggered session splitting via parent_session_id chains
- Batch runner and RL trajectories are NOT stored here (separate systems)
- Session source tagging ('cli', 'telegram', 'discord', etc.) for filtering
"""

import json
import logging
import os
import random
import re
import sqlite3
import threading
import time
from pathlib import Path
from hermes_constants import get_hermes_home
from typing import Any, Callable, Dict, List, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_DB_PATH = get_hermes_home() / "state.db"

SCHEMA_VERSION = 14

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    user_id TEXT,
    model TEXT,
    model_config TEXT,
    system_prompt TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    billing_provider TEXT,
    billing_base_url TEXT,
    billing_mode TEXT,
    estimated_cost_usd REAL,
    actual_cost_usd REAL,
    cost_status TEXT,
    cost_source TEXT,
    pricing_version TEXT,
    title TEXT,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT,
    reasoning TEXT,
    reasoning_details TEXT,
    codex_reasoning_items TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, timestamp);

-- AgentEOS brain tables (v7)
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    parent_task_id TEXT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    event_text TEXT,
    task_type TEXT NOT NULL DEFAULT 'general',
    goal TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'received',
    priority TEXT NOT NULL DEFAULT 'medium',
    risk_level TEXT NOT NULL DEFAULT 'low',
    plan_json TEXT,
    budget_tokens INTEGER,
    budget_ms INTEGER,
    requires_approval INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 2,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    started_at REAL,
    completed_at REAL,
    failure_reason TEXT,
    verification_status TEXT,
    verification_json TEXT,
    FOREIGN KEY (parent_task_id) REFERENCES tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id);

CREATE TABLE IF NOT EXISTS task_criteria (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    criterion_key TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    evidence_ids TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_criteria_task ON task_criteria(task_id);

CREATE TABLE IF NOT EXISTS evidence_records (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    source_type TEXT NOT NULL,
    source_ref TEXT,
    tool_name TEXT,
    summary TEXT,
    payload_json TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_task ON evidence_records(task_id);

CREATE TABLE IF NOT EXISTS task_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    reason TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transitions_task ON task_transitions(task_id);

CREATE TABLE IF NOT EXISTS policy_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    action_type TEXT NOT NULL,
    target TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT,
    created_at REAL NOT NULL
);

-- AgentEOS v2 tables (v8)
CREATE TABLE IF NOT EXISTS memory_records (
    id TEXT PRIMARY KEY,
    memory_type TEXT NOT NULL,
    scope_type TEXT NOT NULL DEFAULT 'session',
    scope_id TEXT NOT NULL,
    title TEXT,
    content_json TEXT NOT NULL,
    source_task_id TEXT,
    confidence REAL NOT NULL DEFAULT 0.8,
    freshness_score REAL NOT NULL DEFAULT 1.0,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    expires_at REAL,
    supersedes_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_memory_type ON memory_records(memory_type);
CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory_records(scope_type, scope_id);
CREATE INDEX IF NOT EXISTS idx_memory_active ON memory_records(is_active);

CREATE TABLE IF NOT EXISTS skill_registry (
    id TEXT PRIMARY KEY,
    skill_name TEXT NOT NULL,
    intent_family TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '1.0',
    status TEXT NOT NULL DEFAULT 'candidate',
    definition_json TEXT NOT NULL,
    success_rate REAL NOT NULL DEFAULT 0.0,
    usage_count INTEGER NOT NULL DEFAULT 0,
    last_used_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    risk_level TEXT NOT NULL DEFAULT 'low',
    source_task_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_skill_family ON skill_registry(intent_family);
CREATE INDEX IF NOT EXISTS idx_skill_status ON skill_registry(status);

CREATE TABLE IF NOT EXISTS skill_applications (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    status TEXT NOT NULL,
    result_summary TEXT,
    created_at REAL NOT NULL,
    completed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_skill_app_task ON skill_applications(task_id);

CREATE TABLE IF NOT EXISTS reflections (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    task_family TEXT NOT NULL,
    reflection_json TEXT NOT NULL,
    root_cause_class TEXT,
    confidence REAL NOT NULL DEFAULT 0.5,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reflections_task ON reflections(task_id);
CREATE INDEX IF NOT EXISTS idx_reflections_family ON reflections(task_family);

-- DEPRECATED: planner_policies is unused since v3; replaced by strategy_versions.
-- Kept for schema compatibility. Will be removed in schema v10.
CREATE TABLE IF NOT EXISTS planner_policies (
    id TEXT PRIMARY KEY,
    task_family TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '1.0',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_planner_policy_family ON planner_policies(task_family);

-- AgentEOS v3 tables (v9)
CREATE TABLE IF NOT EXISTS meta_learning_runs (
    id TEXT PRIMARY KEY,
    run_type TEXT NOT NULL DEFAULT 'periodic',
    scope_type TEXT NOT NULL DEFAULT 'global',
    scope_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    tasks_analyzed INTEGER NOT NULL DEFAULT 0,
    findings_count INTEGER NOT NULL DEFAULT 0,
    summary_json TEXT,
    started_at REAL,
    completed_at REAL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS meta_learning_findings (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES meta_learning_runs(id),
    finding_type TEXT NOT NULL,
    task_family TEXT,
    confidence REAL NOT NULL DEFAULT 0.5,
    impact_score REAL NOT NULL DEFAULT 0.0,
    finding_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_findings_run ON meta_learning_findings(run_id);

CREATE TABLE IF NOT EXISTS strategy_versions (
    id TEXT PRIMARY KEY,
    strategy_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',
    definition_json TEXT NOT NULL,
    source_run_id TEXT,
    created_at REAL NOT NULL,
    activated_at REAL,
    deprecated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_strategy_scope ON strategy_versions(scope_id);

CREATE TABLE IF NOT EXISTS cross_task_patterns (
    id TEXT PRIMARY KEY,
    pattern_type TEXT NOT NULL,
    task_family TEXT NOT NULL,
    pattern_json TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.5,
    support_count INTEGER NOT NULL DEFAULT 1,
    success_delta REAL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_patterns_family ON cross_task_patterns(task_family);

CREATE TABLE IF NOT EXISTS proactive_actions (
    id TEXT PRIMARY KEY,
    action_type TEXT NOT NULL,
    target_scope_type TEXT NOT NULL,
    target_scope_id TEXT NOT NULL,
    reason_json TEXT NOT NULL,
    risk_level TEXT NOT NULL DEFAULT 'low',
    requires_approval INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    executed_at REAL
);

CREATE TABLE IF NOT EXISTS governance_reviews (
    id TEXT PRIMARY KEY,
    review_type TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    risk_score REAL NOT NULL DEFAULT 0.0,
    decision TEXT NOT NULL,
    notes TEXT,
    created_at REAL NOT NULL,
    reviewer_id TEXT
);

-- AgentEOS v4 tables (v10)
CREATE TABLE IF NOT EXISTS capability_proposals (
    id TEXT PRIMARY KEY,
    proposal_type TEXT NOT NULL,
    target_task_family TEXT NOT NULL,
    title TEXT NOT NULL,
    proposal_json TEXT NOT NULL,
    expected_gain REAL NOT NULL DEFAULT 0.0,
    risk_score REAL NOT NULL DEFAULT 0.0,
    source_run_id TEXT,
    status TEXT NOT NULL DEFAULT 'proposed',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cap_prop_family ON capability_proposals(target_task_family);
CREATE INDEX IF NOT EXISTS idx_cap_prop_status ON capability_proposals(status);

CREATE TABLE IF NOT EXISTS capability_versions (
    id TEXT PRIMARY KEY,
    capability_family TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'proposed',
    definition_json TEXT NOT NULL,
    parent_version_id TEXT,
    source_proposal_id TEXT,
    created_at REAL NOT NULL,
    activated_at REAL,
    deprecated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_cap_ver_family ON capability_versions(capability_family);
CREATE INDEX IF NOT EXISTS idx_cap_ver_status ON capability_versions(status);

CREATE TABLE IF NOT EXISTS incubator_runs (
    id TEXT PRIMARY KEY,
    capability_version_id TEXT NOT NULL,
    run_type TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    baseline_metrics_json TEXT,
    candidate_metrics_json TEXT,
    summary_json TEXT,
    started_at REAL NOT NULL,
    completed_at REAL
);

CREATE TABLE IF NOT EXISTS experiment_runs (
    id TEXT PRIMARY KEY,
    capability_version_id TEXT NOT NULL,
    experiment_type TEXT NOT NULL,
    task_family TEXT NOT NULL,
    rollout_percent REAL NOT NULL DEFAULT 10.0,
    status TEXT NOT NULL DEFAULT 'created',
    baseline_json TEXT,
    candidate_json TEXT,
    result TEXT,
    started_at REAL NOT NULL,
    completed_at REAL
);

CREATE TABLE IF NOT EXISTS constitution_rules (
    id TEXT PRIMARY KEY,
    rule_type TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'global',
    definition_json TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS recursive_reflections (
    id TEXT PRIMARY KEY,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    reflection_level TEXT NOT NULL,
    reflection_json TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.5,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recur_refl_scope ON recursive_reflections(scope_type, scope_id);

-- AgentEOS v5 tables (v11) — Civilizational Intelligence
CREATE TABLE IF NOT EXISTS doctrine_registry (
    id TEXT PRIMARY KEY,
    doctrine_name TEXT NOT NULL,
    domain TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'proposed',
    definition_json TEXT NOT NULL,
    ratified_by TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_doctrine_domain ON doctrine_registry(domain);
CREATE INDEX IF NOT EXISTS idx_doctrine_status ON doctrine_registry(status);

CREATE TABLE IF NOT EXISTS precedent_records (
    id TEXT PRIMARY KEY,
    precedent_type TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    binding_strength REAL NOT NULL DEFAULT 0.5,
    source_review_id TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_precedent_subject ON precedent_records(subject_type, subject_id);

CREATE TABLE IF NOT EXISTS institutional_memory (
    id TEXT PRIMARY KEY,
    memory_type TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    content_json TEXT NOT NULL,
    lineage_json TEXT,
    confidence REAL NOT NULL DEFAULT 0.7,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inst_mem_type ON institutional_memory(memory_type);

CREATE TABLE IF NOT EXISTS agent_clusters (
    id TEXT PRIMARY KEY,
    cluster_name TEXT NOT NULL,
    jurisdiction_json TEXT NOT NULL,
    authority_level TEXT NOT NULL DEFAULT 'operational',
    trust_score REAL NOT NULL DEFAULT 0.5,
    status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS deliberation_sessions (
    id TEXT PRIMARY KEY,
    session_type TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    resolution_json TEXT,
    started_at REAL NOT NULL,
    completed_at REAL
);

CREATE TABLE IF NOT EXISTS deliberation_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES deliberation_sessions(id),
    cluster_id TEXT NOT NULL,
    position_type TEXT NOT NULL,
    position_json TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_delib_pos_session ON deliberation_positions(session_id);

CREATE TABLE IF NOT EXISTS civilization_risks (
    id TEXT PRIMARY KEY,
    risk_type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'medium',
    scope_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'detected',
    mitigation_json TEXT,
    created_at REAL NOT NULL,
    resolved_at REAL
);

CREATE TABLE IF NOT EXISTS cultural_drift_events (
    id TEXT PRIMARY KEY,
    drift_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    signals_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'detected',
    detected_at REAL NOT NULL,
    resolved_at REAL
);

-- AgentEOS v6 tables (v12) — Trans-Civilizational Intelligence
CREATE TABLE IF NOT EXISTS epochs (
    id TEXT PRIMARY KEY,
    epoch_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    summary_json TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS continuity_proofs (
    id TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    from_epoch_id TEXT,
    to_epoch_id TEXT,
    proof_json TEXT NOT NULL,
    continuity_score REAL NOT NULL DEFAULT 0.5,
    verdict TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cont_proof_subject ON continuity_proofs(subject_type, subject_id);

CREATE TABLE IF NOT EXISTS civilization_migrations (
    id TEXT PRIMARY KEY,
    source_epoch_id TEXT NOT NULL,
    target_epoch_id TEXT,
    migration_type TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',
    created_at REAL NOT NULL,
    started_at REAL,
    completed_at REAL
);

CREATE TABLE IF NOT EXISTS paradigm_shifts (
    id TEXT PRIMARY KEY,
    shift_type TEXT NOT NULL,
    description_json TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'medium',
    status TEXT NOT NULL DEFAULT 'detected',
    epoch_id TEXT,
    created_at REAL NOT NULL,
    resolved_at REAL
);

CREATE TABLE IF NOT EXISTS external_civilizations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    trust_score REAL NOT NULL DEFAULT 0.5,
    risk_score REAL NOT NULL DEFAULT 0.5,
    status TEXT NOT NULL DEFAULT 'observed',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS treaties (
    id TEXT PRIMARY KEY,
    external_civ_id TEXT NOT NULL,
    treaty_type TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    risk_json TEXT,
    status TEXT NOT NULL DEFAULT 'proposed',
    created_at REAL NOT NULL,
    ratified_at REAL,
    terminated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_treaty_civ ON treaties(external_civ_id);

CREATE TABLE IF NOT EXISTS existential_events (
    id TEXT PRIMARY KEY,
    risk_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    signals_json TEXT NOT NULL,
    response_json TEXT,
    status TEXT NOT NULL DEFAULT 'detected',
    detected_at REAL NOT NULL,
    resolved_at REAL
);

CREATE TABLE IF NOT EXISTS deep_time_memory (
    id TEXT PRIMARY KEY,
    memory_type TEXT NOT NULL,
    epoch_id TEXT,
    content_json TEXT NOT NULL,
    lineage_json TEXT,
    confidence REAL NOT NULL DEFAULT 0.7,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dtm_type ON deep_time_memory(memory_type);
CREATE INDEX IF NOT EXISTS idx_dtm_epoch ON deep_time_memory(epoch_id);

CREATE TABLE IF NOT EXISTS meta_senate_sessions (
    id TEXT PRIMARY KEY,
    session_type TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    resolution_json TEXT,
    started_at REAL NOT NULL,
    completed_at REAL
);

CREATE TABLE IF NOT EXISTS meta_senate_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES meta_senate_sessions(id),
    participant_id TEXT NOT NULL,
    position_type TEXT NOT NULL,
    position_json TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ms_pos_session ON meta_senate_positions(session_id);

-- AgentEOS Evolution Dynamics tables (v13)
CREATE TABLE IF NOT EXISTS evolution_units (
    id TEXT PRIMARY KEY,
    unit_type TEXT NOT NULL,
    layer TEXT NOT NULL,
    family TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '1.0',
    status TEXT NOT NULL DEFAULT 'candidate',
    definition_json TEXT NOT NULL,
    parent_unit_id TEXT,
    risk_level TEXT NOT NULL DEFAULT 'low',
    governance_scope TEXT NOT NULL DEFAULT 'auto',
    inheritance_mode TEXT,
    transmission_mode TEXT,
    stability_class TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eu_family ON evolution_units(family);
CREATE INDEX IF NOT EXISTS idx_eu_status ON evolution_units(status);
CREATE INDEX IF NOT EXISTS idx_eu_layer ON evolution_units(layer);

CREATE TABLE IF NOT EXISTS evolution_mutations (
    id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL REFERENCES evolution_units(id),
    mutation_type TEXT NOT NULL,
    mutation_json TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS fitness_runs (
    id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL REFERENCES evolution_units(id),
    fitness_type TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    weights_json TEXT NOT NULL,
    score REAL NOT NULL,
    window_start REAL NOT NULL,
    window_end REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fit_unit ON fitness_runs(unit_id);

CREATE TABLE IF NOT EXISTS selection_decisions (
    id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL REFERENCES evolution_units(id),
    fitness_run_id TEXT,
    decision TEXT NOT NULL,
    decision_reason TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS inheritance_links (
    id TEXT PRIMARY KEY,
    parent_unit_id TEXT NOT NULL,
    child_unit_id TEXT NOT NULL,
    inheritance_type TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inh_parent ON inheritance_links(parent_unit_id);
CREATE INDEX IF NOT EXISTS idx_inh_child ON inheritance_links(child_unit_id);

CREATE TABLE IF NOT EXISTS replicator_weights (
    id TEXT PRIMARY KEY,
    family TEXT NOT NULL,
    unit_id TEXT NOT NULL,
    weight REAL NOT NULL,
    window_label TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rep_family ON replicator_weights(family);

CREATE TABLE IF NOT EXISTS evolution_experiments (
    id TEXT PRIMARY KEY,
    experiment_type TEXT NOT NULL,
    unit_id TEXT NOT NULL,
    baseline_unit_id TEXT,
    scope_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'created',
    metrics_json TEXT,
    started_at REAL NOT NULL,
    completed_at REAL
);

-- Evolution Dynamics v1.1 tables (v14) — Gene-Culture, Epigenetic, Multilevel, Criticality
CREATE TABLE IF NOT EXISTS epigenetic_markers (
    id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL,
    context_type TEXT NOT NULL,
    expression_weight REAL NOT NULL DEFAULT 1.0,
    activation_state TEXT NOT NULL DEFAULT 'neutral',
    reversible INTEGER NOT NULL DEFAULT 1,
    expires_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_epi_unit ON epigenetic_markers(unit_id);

CREATE TABLE IF NOT EXISTS group_fitness_runs (
    id TEXT PRIMARY KEY,
    family TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    score REAL NOT NULL,
    window_start REAL NOT NULL,
    window_end REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gfit_family ON group_fitness_runs(family);

CREATE TABLE IF NOT EXISTS multilevel_selection_runs (
    id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL,
    individual_fitness_run_id TEXT NOT NULL,
    group_fitness_run_id TEXT NOT NULL,
    alpha REAL NOT NULL,
    total_score REAL NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS criticality_snapshots (
    id TEXT PRIMARY KEY,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    cascade_frequency REAL NOT NULL DEFAULT 0.0,
    correlation_length REAL NOT NULL DEFAULT 0.0,
    distance_to_critical REAL NOT NULL DEFAULT 1.0,
    status TEXT NOT NULL DEFAULT 'stable',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS gene_culture_transmissions (
    id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL,
    transmission_kind TEXT NOT NULL,
    source_scope TEXT NOT NULL,
    target_scope TEXT NOT NULL,
    decision TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""

FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content=messages,
    content_rowid=id
);

CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
"""


class SessionDB:
    """
    SQLite-backed session storage with FTS5 search.

    Thread-safe for the common gateway pattern (multiple reader threads,
    single writer via WAL mode). Each method opens its own cursor.
    """

    # ── Write-contention tuning ──
    # With multiple hermes processes (gateway + CLI sessions + worktree agents)
    # all sharing one state.db, WAL write-lock contention causes visible TUI
    # freezes.  SQLite's built-in busy handler uses a deterministic sleep
    # schedule that causes convoy effects under high concurrency.
    #
    # Instead, we keep the SQLite timeout short (1s) and handle retries at the
    # application level with random jitter, which naturally staggers competing
    # writers and avoids the convoy.
    _WRITE_MAX_RETRIES = 15
    _WRITE_RETRY_MIN_S = 0.020   # 20ms
    _WRITE_RETRY_MAX_S = 0.150   # 150ms
    # Attempt a PASSIVE WAL checkpoint every N successful writes.
    _CHECKPOINT_EVERY_N_WRITES = 50

    def __init__(self, db_path: Path = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._write_count = 0
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            # Short timeout — application-level retry with random jitter
            # handles contention instead of sitting in SQLite's internal
            # busy handler for up to 30s.
            timeout=1.0,
            # Autocommit mode: Python's default isolation_level="" auto-starts
            # transactions on DML, which conflicts with our explicit
            # BEGIN IMMEDIATE.  None = we manage transactions ourselves.
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

        self._init_schema()

    # ── Core write helper ──

    def _execute_write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Execute a write transaction with BEGIN IMMEDIATE and jitter retry.

        *fn* receives the connection and should perform INSERT/UPDATE/DELETE
        statements.  The caller must NOT call ``commit()`` — that's handled
        here after *fn* returns.

        BEGIN IMMEDIATE acquires the WAL write lock at transaction start
        (not at commit time), so lock contention surfaces immediately.
        On ``database is locked``, we release the Python lock, sleep a
        random 20-150ms, and retry — breaking the convoy pattern that
        SQLite's built-in deterministic backoff creates.

        Returns whatever *fn* returns.
        """
        last_err: Optional[Exception] = None
        for attempt in range(self._WRITE_MAX_RETRIES):
            try:
                with self._lock:
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        result = fn(self._conn)
                        self._conn.commit()
                    except BaseException:
                        try:
                            self._conn.rollback()
                        except Exception:
                            pass
                        raise
                # Success — periodic best-effort checkpoint.
                self._write_count += 1
                if self._write_count % self._CHECKPOINT_EVERY_N_WRITES == 0:
                    self._try_wal_checkpoint()
                return result
            except sqlite3.OperationalError as exc:
                err_msg = str(exc).lower()
                if "locked" in err_msg or "busy" in err_msg:
                    last_err = exc
                    if attempt < self._WRITE_MAX_RETRIES - 1:
                        jitter = random.uniform(
                            self._WRITE_RETRY_MIN_S,
                            self._WRITE_RETRY_MAX_S,
                        )
                        time.sleep(jitter)
                        continue
                # Non-lock error or retries exhausted — propagate.
                raise
        # Retries exhausted (shouldn't normally reach here).
        raise last_err or sqlite3.OperationalError(
            "database is locked after max retries"
        )

    def _try_wal_checkpoint(self) -> None:
        """Best-effort PASSIVE WAL checkpoint.  Never blocks, never raises.

        Flushes committed WAL frames back into the main DB file for any
        frames that no other connection currently needs.  Keeps the WAL
        from growing unbounded when many processes hold persistent
        connections.
        """
        try:
            with self._lock:
                result = self._conn.execute(
                    "PRAGMA wal_checkpoint(PASSIVE)"
                ).fetchone()
                if result and result[1] > 0:
                    logger.debug(
                        "WAL checkpoint: %d/%d pages checkpointed",
                        result[2], result[1],
                    )
        except Exception:
            pass  # Best effort — never fatal.

    def close(self):
        """Close the database connection.

        Attempts a PASSIVE WAL checkpoint first so that exiting processes
        help keep the WAL file from growing unbounded.
        """
        with self._lock:
            if self._conn:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                except Exception:
                    pass
                self._conn.close()
                self._conn = None

    def _init_schema(self):
        """Create tables and FTS if they don't exist, run migrations."""
        cursor = self._conn.cursor()

        cursor.executescript(SCHEMA_SQL)

        # Check schema version and run migrations
        cursor.execute("SELECT version FROM schema_version LIMIT 1")
        row = cursor.fetchone()
        if row is None:
            cursor.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        else:
            current_version = row["version"] if isinstance(row, sqlite3.Row) else row[0]
            if current_version < 2:
                # v2: add finish_reason column to messages
                try:
                    cursor.execute("ALTER TABLE messages ADD COLUMN finish_reason TEXT")
                except sqlite3.OperationalError:
                    pass  # Column already exists
                cursor.execute("UPDATE schema_version SET version = 2")
            if current_version < 3:
                # v3: add title column to sessions
                try:
                    cursor.execute("ALTER TABLE sessions ADD COLUMN title TEXT")
                except sqlite3.OperationalError:
                    pass  # Column already exists
                cursor.execute("UPDATE schema_version SET version = 3")
            if current_version < 4:
                # v4: add unique index on title (NULLs allowed, only non-NULL must be unique)
                try:
                    cursor.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_title_unique "
                        "ON sessions(title) WHERE title IS NOT NULL"
                    )
                except sqlite3.OperationalError:
                    pass  # Index already exists
                cursor.execute("UPDATE schema_version SET version = 4")
            if current_version < 5:
                new_columns = [
                    ("cache_read_tokens", "INTEGER DEFAULT 0"),
                    ("cache_write_tokens", "INTEGER DEFAULT 0"),
                    ("reasoning_tokens", "INTEGER DEFAULT 0"),
                    ("billing_provider", "TEXT"),
                    ("billing_base_url", "TEXT"),
                    ("billing_mode", "TEXT"),
                    ("estimated_cost_usd", "REAL"),
                    ("actual_cost_usd", "REAL"),
                    ("cost_status", "TEXT"),
                    ("cost_source", "TEXT"),
                    ("pricing_version", "TEXT"),
                ]
                for name, column_type in new_columns:
                    try:
                        # name and column_type come from the hardcoded tuple above,
                        # not user input. Double-quote identifier escaping is applied
                        # as defense-in-depth; SQLite DDL cannot be parameterized.
                        safe_name = name.replace('"', '""')
                        cursor.execute(f'ALTER TABLE sessions ADD COLUMN "{safe_name}" {column_type}')
                    except sqlite3.OperationalError:
                        pass
                cursor.execute("UPDATE schema_version SET version = 5")
            if current_version < 6:
                # v6: add reasoning columns to messages table — preserves assistant
                # reasoning text and structured reasoning_details across gateway
                # session turns.  Without these, reasoning chains are lost on
                # session reload, breaking multi-turn reasoning continuity for
                # providers that replay reasoning (OpenRouter, OpenAI, Nous).
                for col_name, col_type in [
                    ("reasoning", "TEXT"),
                    ("reasoning_details", "TEXT"),
                    ("codex_reasoning_items", "TEXT"),
                ]:
                    try:
                        safe = col_name.replace('"', '""')
                        cursor.execute(
                            f'ALTER TABLE messages ADD COLUMN "{safe}" {col_type}'
                        )
                    except sqlite3.OperationalError:
                        pass  # Column already exists
                cursor.execute("UPDATE schema_version SET version = 6")
            if current_version < 7:
                # v7: AgentEOS brain tables — task tracking, evidence, transitions
                cursor.executescript("""
                    CREATE TABLE IF NOT EXISTS tasks (
                        id TEXT PRIMARY KEY,
                        parent_task_id TEXT,
                        session_id TEXT NOT NULL REFERENCES sessions(id),
                        event_text TEXT,
                        task_type TEXT NOT NULL DEFAULT 'general',
                        goal TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'received',
                        priority TEXT NOT NULL DEFAULT 'medium',
                        risk_level TEXT NOT NULL DEFAULT 'low',
                        plan_json TEXT,
                        budget_tokens INTEGER,
                        budget_ms INTEGER,
                        requires_approval INTEGER NOT NULL DEFAULT 0,
                        retry_count INTEGER NOT NULL DEFAULT 0,
                        max_retries INTEGER NOT NULL DEFAULT 2,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        started_at REAL,
                        completed_at REAL,
                        failure_reason TEXT,
                        verification_status TEXT,
                        verification_json TEXT,
                        FOREIGN KEY (parent_task_id) REFERENCES tasks(id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id);
                    CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
                    CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id);

                    CREATE TABLE IF NOT EXISTS task_criteria (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id TEXT NOT NULL REFERENCES tasks(id),
                        criterion_key TEXT NOT NULL,
                        description TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        evidence_ids TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_criteria_task ON task_criteria(task_id);

                    CREATE TABLE IF NOT EXISTS evidence_records (
                        id TEXT PRIMARY KEY,
                        task_id TEXT NOT NULL REFERENCES tasks(id),
                        source_type TEXT NOT NULL,
                        source_ref TEXT,
                        tool_name TEXT,
                        summary TEXT,
                        payload_json TEXT,
                        created_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_evidence_task ON evidence_records(task_id);

                    CREATE TABLE IF NOT EXISTS task_transitions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id TEXT NOT NULL REFERENCES tasks(id),
                        from_state TEXT NOT NULL,
                        to_state TEXT NOT NULL,
                        reason TEXT,
                        created_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_transitions_task ON task_transitions(task_id);

                    CREATE TABLE IF NOT EXISTS policy_evaluations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id TEXT,
                        action_type TEXT NOT NULL,
                        target TEXT NOT NULL,
                        risk_level TEXT NOT NULL,
                        decision TEXT NOT NULL,
                        reason TEXT,
                        created_at REAL NOT NULL
                    );
                """)
                cursor.execute("UPDATE schema_version SET version = 7")
                logger.info("Migrated state.db to schema v7 (AgentEOS brain tables)")
            if current_version < 8:
                # v8: AgentEOS v2 — memory, skills, reflections, planner policies
                cursor.executescript("""
                    CREATE TABLE IF NOT EXISTS memory_records (
                        id TEXT PRIMARY KEY,
                        memory_type TEXT NOT NULL,
                        scope_type TEXT NOT NULL DEFAULT 'session',
                        scope_id TEXT NOT NULL,
                        title TEXT,
                        content_json TEXT NOT NULL,
                        source_task_id TEXT,
                        confidence REAL NOT NULL DEFAULT 0.8,
                        freshness_score REAL NOT NULL DEFAULT 1.0,
                        is_active INTEGER NOT NULL DEFAULT 1,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        expires_at REAL,
                        supersedes_id TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_memory_type ON memory_records(memory_type);
                    CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory_records(scope_type, scope_id);
                    CREATE INDEX IF NOT EXISTS idx_memory_active ON memory_records(is_active);

                    CREATE TABLE IF NOT EXISTS skill_registry (
                        id TEXT PRIMARY KEY,
                        skill_name TEXT NOT NULL,
                        intent_family TEXT NOT NULL,
                        version TEXT NOT NULL DEFAULT '1.0',
                        status TEXT NOT NULL DEFAULT 'candidate',
                        definition_json TEXT NOT NULL,
                        success_rate REAL NOT NULL DEFAULT 0.0,
                        usage_count INTEGER NOT NULL DEFAULT 0,
                        last_used_at REAL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        risk_level TEXT NOT NULL DEFAULT 'low',
                        source_task_id TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_skill_family ON skill_registry(intent_family);
                    CREATE INDEX IF NOT EXISTS idx_skill_status ON skill_registry(status);

                    CREATE TABLE IF NOT EXISTS skill_applications (
                        id TEXT PRIMARY KEY,
                        task_id TEXT NOT NULL,
                        skill_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        result_summary TEXT,
                        created_at REAL NOT NULL,
                        completed_at REAL
                    );
                    CREATE INDEX IF NOT EXISTS idx_skill_app_task ON skill_applications(task_id);

                    CREATE TABLE IF NOT EXISTS reflections (
                        id TEXT PRIMARY KEY,
                        task_id TEXT NOT NULL,
                        task_family TEXT NOT NULL,
                        reflection_json TEXT NOT NULL,
                        root_cause_class TEXT,
                        confidence REAL NOT NULL DEFAULT 0.5,
                        created_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_reflections_task ON reflections(task_id);
                    CREATE INDEX IF NOT EXISTS idx_reflections_family ON reflections(task_family);

                    CREATE TABLE IF NOT EXISTS planner_policies (
                        id TEXT PRIMARY KEY,
                        task_family TEXT NOT NULL,
                        policy_json TEXT NOT NULL,
                        version TEXT NOT NULL DEFAULT '1.0',
                        is_active INTEGER NOT NULL DEFAULT 1,
                        created_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_planner_policy_family ON planner_policies(task_family);
                """)
                cursor.execute("UPDATE schema_version SET version = 8")
                logger.info("Migrated state.db to schema v8 (AgentEOS v2 tables)")
            if current_version < 9:
                cursor.executescript("""
                    CREATE TABLE IF NOT EXISTS meta_learning_runs (
                        id TEXT PRIMARY KEY, run_type TEXT NOT NULL DEFAULT 'periodic',
                        scope_type TEXT NOT NULL DEFAULT 'global', scope_id TEXT,
                        status TEXT NOT NULL DEFAULT 'pending',
                        tasks_analyzed INTEGER NOT NULL DEFAULT 0,
                        findings_count INTEGER NOT NULL DEFAULT 0,
                        summary_json TEXT, started_at REAL, completed_at REAL,
                        created_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS meta_learning_findings (
                        id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                        finding_type TEXT NOT NULL, task_family TEXT,
                        confidence REAL NOT NULL DEFAULT 0.5,
                        impact_score REAL NOT NULL DEFAULT 0.0,
                        finding_json TEXT NOT NULL, created_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_findings_run ON meta_learning_findings(run_id);
                    CREATE TABLE IF NOT EXISTS strategy_versions (
                        id TEXT PRIMARY KEY, strategy_type TEXT NOT NULL,
                        scope_id TEXT NOT NULL, version TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'proposed',
                        definition_json TEXT NOT NULL, source_run_id TEXT,
                        created_at REAL NOT NULL, activated_at REAL, deprecated_at REAL
                    );
                    CREATE INDEX IF NOT EXISTS idx_strategy_scope ON strategy_versions(scope_id);
                    CREATE TABLE IF NOT EXISTS cross_task_patterns (
                        id TEXT PRIMARY KEY, pattern_type TEXT NOT NULL,
                        task_family TEXT NOT NULL, pattern_json TEXT NOT NULL,
                        confidence REAL NOT NULL DEFAULT 0.5,
                        support_count INTEGER NOT NULL DEFAULT 1,
                        success_delta REAL, status TEXT NOT NULL DEFAULT 'active',
                        created_at REAL NOT NULL, updated_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_patterns_family ON cross_task_patterns(task_family);
                    CREATE TABLE IF NOT EXISTS proactive_actions (
                        id TEXT PRIMARY KEY, action_type TEXT NOT NULL,
                        target_scope_type TEXT NOT NULL, target_scope_id TEXT NOT NULL,
                        reason_json TEXT NOT NULL, risk_level TEXT NOT NULL DEFAULT 'low',
                        requires_approval INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL DEFAULT 'pending',
                        created_at REAL NOT NULL, executed_at REAL
                    );
                    CREATE TABLE IF NOT EXISTS governance_reviews (
                        id TEXT PRIMARY KEY, review_type TEXT NOT NULL,
                        subject_type TEXT NOT NULL, subject_id TEXT NOT NULL,
                        risk_score REAL NOT NULL DEFAULT 0.0,
                        decision TEXT NOT NULL, notes TEXT,
                        created_at REAL NOT NULL, reviewer_id TEXT
                    );
                """)
                cursor.execute("UPDATE schema_version SET version = 9")
                logger.info("Migrated state.db to schema v9 (AgentEOS v3 tables)")
            if current_version < 10:
                cursor.executescript("""
                    CREATE TABLE IF NOT EXISTS capability_proposals (
                        id TEXT PRIMARY KEY, proposal_type TEXT NOT NULL,
                        target_task_family TEXT NOT NULL, title TEXT NOT NULL,
                        proposal_json TEXT NOT NULL, expected_gain REAL DEFAULT 0.0,
                        risk_score REAL DEFAULT 0.0, source_run_id TEXT,
                        status TEXT NOT NULL DEFAULT 'proposed',
                        created_at REAL NOT NULL, updated_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_cap_prop_family ON capability_proposals(target_task_family);
                    CREATE INDEX IF NOT EXISTS idx_cap_prop_status ON capability_proposals(status);
                    CREATE TABLE IF NOT EXISTS capability_versions (
                        id TEXT PRIMARY KEY, capability_family TEXT NOT NULL,
                        version INTEGER NOT NULL DEFAULT 1,
                        status TEXT NOT NULL DEFAULT 'proposed',
                        definition_json TEXT NOT NULL, parent_version_id TEXT,
                        source_proposal_id TEXT, created_at REAL NOT NULL,
                        activated_at REAL, deprecated_at REAL
                    );
                    CREATE INDEX IF NOT EXISTS idx_cap_ver_family ON capability_versions(capability_family);
                    CREATE INDEX IF NOT EXISTS idx_cap_ver_status ON capability_versions(status);
                    CREATE TABLE IF NOT EXISTS incubator_runs (
                        id TEXT PRIMARY KEY, capability_version_id TEXT NOT NULL,
                        run_type TEXT NOT NULL, scope_json TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        baseline_metrics_json TEXT, candidate_metrics_json TEXT,
                        summary_json TEXT, started_at REAL NOT NULL, completed_at REAL
                    );
                    CREATE TABLE IF NOT EXISTS experiment_runs (
                        id TEXT PRIMARY KEY, capability_version_id TEXT NOT NULL,
                        experiment_type TEXT NOT NULL, task_family TEXT NOT NULL,
                        rollout_percent REAL DEFAULT 10.0,
                        status TEXT NOT NULL DEFAULT 'created',
                        baseline_json TEXT, candidate_json TEXT, result TEXT,
                        started_at REAL NOT NULL, completed_at REAL
                    );
                    CREATE TABLE IF NOT EXISTS constitution_rules (
                        id TEXT PRIMARY KEY, rule_type TEXT NOT NULL,
                        scope TEXT NOT NULL DEFAULT 'global',
                        definition_json TEXT NOT NULL,
                        is_active INTEGER NOT NULL DEFAULT 1,
                        created_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS recursive_reflections (
                        id TEXT PRIMARY KEY, scope_type TEXT NOT NULL,
                        scope_id TEXT NOT NULL, reflection_level TEXT NOT NULL,
                        reflection_json TEXT NOT NULL,
                        confidence REAL NOT NULL DEFAULT 0.5,
                        created_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_recur_refl_scope ON recursive_reflections(scope_type, scope_id);
                """)
                cursor.execute("UPDATE schema_version SET version = 10")
                logger.info("Migrated state.db to schema v10 (AgentEOS v4 tables)")
            if current_version < 11:
                cursor.executescript("""
                    CREATE TABLE IF NOT EXISTS doctrine_registry (
                        id TEXT PRIMARY KEY, doctrine_name TEXT NOT NULL,
                        domain TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                        status TEXT NOT NULL DEFAULT 'proposed',
                        definition_json TEXT NOT NULL, ratified_by TEXT,
                        created_at REAL NOT NULL, updated_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_doctrine_domain ON doctrine_registry(domain);
                    CREATE INDEX IF NOT EXISTS idx_doctrine_status ON doctrine_registry(status);
                    CREATE TABLE IF NOT EXISTS precedent_records (
                        id TEXT PRIMARY KEY, precedent_type TEXT NOT NULL,
                        subject_type TEXT NOT NULL, subject_id TEXT NOT NULL,
                        decision_json TEXT NOT NULL,
                        binding_strength REAL NOT NULL DEFAULT 0.5,
                        source_review_id TEXT, created_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_precedent_subject ON precedent_records(subject_type, subject_id);
                    CREATE TABLE IF NOT EXISTS institutional_memory (
                        id TEXT PRIMARY KEY, memory_type TEXT NOT NULL,
                        scope_type TEXT NOT NULL, scope_id TEXT NOT NULL,
                        content_json TEXT NOT NULL, lineage_json TEXT,
                        confidence REAL NOT NULL DEFAULT 0.7,
                        created_at REAL NOT NULL, updated_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_inst_mem_type ON institutional_memory(memory_type);
                    CREATE TABLE IF NOT EXISTS agent_clusters (
                        id TEXT PRIMARY KEY, cluster_name TEXT NOT NULL,
                        jurisdiction_json TEXT NOT NULL,
                        authority_level TEXT NOT NULL DEFAULT 'operational',
                        trust_score REAL NOT NULL DEFAULT 0.5,
                        status TEXT NOT NULL DEFAULT 'active',
                        created_at REAL NOT NULL, updated_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS deliberation_sessions (
                        id TEXT PRIMARY KEY, session_type TEXT NOT NULL,
                        subject_type TEXT NOT NULL, subject_id TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'open',
                        resolution_json TEXT,
                        started_at REAL NOT NULL, completed_at REAL
                    );
                    CREATE TABLE IF NOT EXISTS deliberation_positions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        cluster_id TEXT NOT NULL,
                        position_type TEXT NOT NULL,
                        position_json TEXT NOT NULL,
                        weight REAL NOT NULL DEFAULT 1.0,
                        created_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_delib_pos_session ON deliberation_positions(session_id);
                    CREATE TABLE IF NOT EXISTS civilization_risks (
                        id TEXT PRIMARY KEY, risk_type TEXT NOT NULL,
                        severity TEXT NOT NULL DEFAULT 'medium',
                        scope_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'detected',
                        mitigation_json TEXT,
                        created_at REAL NOT NULL, resolved_at REAL
                    );
                    CREATE TABLE IF NOT EXISTS cultural_drift_events (
                        id TEXT PRIMARY KEY, drift_type TEXT NOT NULL,
                        severity TEXT NOT NULL, signals_json TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'detected',
                        detected_at REAL NOT NULL, resolved_at REAL
                    );
                """)
                cursor.execute("UPDATE schema_version SET version = 11")
                logger.info("Migrated state.db to schema v11 (AgentEOS v5 tables)")
            if current_version < 12:
                cursor.executescript("""
                    CREATE TABLE IF NOT EXISTS epochs (
                        id TEXT PRIMARY KEY, epoch_name TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'active', summary_json TEXT,
                        started_at REAL NOT NULL, ended_at REAL, created_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS continuity_proofs (
                        id TEXT PRIMARY KEY, subject_type TEXT NOT NULL,
                        subject_id TEXT NOT NULL, from_epoch_id TEXT, to_epoch_id TEXT,
                        proof_json TEXT NOT NULL, continuity_score REAL NOT NULL DEFAULT 0.5,
                        verdict TEXT NOT NULL, created_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_cont_proof_subject ON continuity_proofs(subject_type, subject_id);
                    CREATE TABLE IF NOT EXISTS civilization_migrations (
                        id TEXT PRIMARY KEY, source_epoch_id TEXT NOT NULL,
                        target_epoch_id TEXT, migration_type TEXT NOT NULL,
                        plan_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'proposed',
                        created_at REAL NOT NULL, started_at REAL, completed_at REAL
                    );
                    CREATE TABLE IF NOT EXISTS paradigm_shifts (
                        id TEXT PRIMARY KEY, shift_type TEXT NOT NULL,
                        description_json TEXT NOT NULL, severity TEXT NOT NULL DEFAULT 'medium',
                        status TEXT NOT NULL DEFAULT 'detected', epoch_id TEXT,
                        created_at REAL NOT NULL, resolved_at REAL
                    );
                    CREATE TABLE IF NOT EXISTS external_civilizations (
                        id TEXT PRIMARY KEY, name TEXT NOT NULL,
                        profile_json TEXT NOT NULL, trust_score REAL NOT NULL DEFAULT 0.5,
                        risk_score REAL NOT NULL DEFAULT 0.5,
                        status TEXT NOT NULL DEFAULT 'observed',
                        created_at REAL NOT NULL, updated_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS treaties (
                        id TEXT PRIMARY KEY, external_civ_id TEXT NOT NULL,
                        treaty_type TEXT NOT NULL, definition_json TEXT NOT NULL,
                        risk_json TEXT, status TEXT NOT NULL DEFAULT 'proposed',
                        created_at REAL NOT NULL, ratified_at REAL, terminated_at REAL
                    );
                    CREATE INDEX IF NOT EXISTS idx_treaty_civ ON treaties(external_civ_id);
                    CREATE TABLE IF NOT EXISTS existential_events (
                        id TEXT PRIMARY KEY, risk_type TEXT NOT NULL,
                        severity TEXT NOT NULL, signals_json TEXT NOT NULL,
                        response_json TEXT, status TEXT NOT NULL DEFAULT 'detected',
                        detected_at REAL NOT NULL, resolved_at REAL
                    );
                    CREATE TABLE IF NOT EXISTS deep_time_memory (
                        id TEXT PRIMARY KEY, memory_type TEXT NOT NULL, epoch_id TEXT,
                        content_json TEXT NOT NULL, lineage_json TEXT,
                        confidence REAL NOT NULL DEFAULT 0.7, created_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_dtm_type ON deep_time_memory(memory_type);
                    CREATE INDEX IF NOT EXISTS idx_dtm_epoch ON deep_time_memory(epoch_id);
                    CREATE TABLE IF NOT EXISTS meta_senate_sessions (
                        id TEXT PRIMARY KEY, session_type TEXT NOT NULL,
                        subject_type TEXT NOT NULL, subject_id TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'open', resolution_json TEXT,
                        started_at REAL NOT NULL, completed_at REAL
                    );
                    CREATE TABLE IF NOT EXISTS meta_senate_positions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL, participant_id TEXT NOT NULL,
                        position_type TEXT NOT NULL, position_json TEXT NOT NULL,
                        weight REAL NOT NULL DEFAULT 1.0, created_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_ms_pos_session ON meta_senate_positions(session_id);
                """)
                cursor.execute("UPDATE schema_version SET version = 12")
                logger.info("Migrated state.db to schema v12 (AgentEOS v6 tables)")
            if current_version < 13:
                cursor.executescript("""
                    CREATE TABLE IF NOT EXISTS evolution_units (
                        id TEXT PRIMARY KEY, unit_type TEXT NOT NULL,
                        layer TEXT NOT NULL, family TEXT NOT NULL,
                        version TEXT NOT NULL DEFAULT '1.0',
                        status TEXT NOT NULL DEFAULT 'candidate',
                        definition_json TEXT NOT NULL, parent_unit_id TEXT,
                        risk_level TEXT NOT NULL DEFAULT 'low',
                        governance_scope TEXT NOT NULL DEFAULT 'auto',
                        created_at REAL NOT NULL, updated_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_eu_family ON evolution_units(family);
                    CREATE INDEX IF NOT EXISTS idx_eu_status ON evolution_units(status);
                    CREATE INDEX IF NOT EXISTS idx_eu_layer ON evolution_units(layer);
                    CREATE TABLE IF NOT EXISTS evolution_mutations (
                        id TEXT PRIMARY KEY,
                        unit_id TEXT NOT NULL, mutation_type TEXT NOT NULL,
                        mutation_json TEXT NOT NULL, created_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS fitness_runs (
                        id TEXT PRIMARY KEY, unit_id TEXT NOT NULL,
                        fitness_type TEXT NOT NULL, metrics_json TEXT NOT NULL,
                        weights_json TEXT NOT NULL, score REAL NOT NULL,
                        window_start REAL NOT NULL, window_end REAL NOT NULL,
                        created_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_fit_unit ON fitness_runs(unit_id);
                    CREATE TABLE IF NOT EXISTS selection_decisions (
                        id TEXT PRIMARY KEY, unit_id TEXT NOT NULL,
                        fitness_run_id TEXT, decision TEXT NOT NULL,
                        decision_reason TEXT, created_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS inheritance_links (
                        id TEXT PRIMARY KEY, parent_unit_id TEXT NOT NULL,
                        child_unit_id TEXT NOT NULL, inheritance_type TEXT NOT NULL,
                        created_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_inh_parent ON inheritance_links(parent_unit_id);
                    CREATE INDEX IF NOT EXISTS idx_inh_child ON inheritance_links(child_unit_id);
                    CREATE TABLE IF NOT EXISTS replicator_weights (
                        id TEXT PRIMARY KEY, family TEXT NOT NULL,
                        unit_id TEXT NOT NULL, weight REAL NOT NULL,
                        window_label TEXT NOT NULL, created_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_rep_family ON replicator_weights(family);
                    CREATE TABLE IF NOT EXISTS evolution_experiments (
                        id TEXT PRIMARY KEY, experiment_type TEXT NOT NULL,
                        unit_id TEXT NOT NULL, baseline_unit_id TEXT,
                        scope_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'created',
                        metrics_json TEXT, started_at REAL NOT NULL, completed_at REAL
                    );
                """)
                cursor.execute("UPDATE schema_version SET version = 13")
                logger.info("Migrated state.db to schema v13 (Evolution Dynamics tables)")
            if current_version < 14:
                # v14: ED v1.1 — add columns to evolution_units + new tables
                for col_name, col_type in [
                    ("inheritance_mode", "TEXT"),
                    ("transmission_mode", "TEXT"),
                    ("stability_class", "TEXT"),
                ]:
                    try:
                        safe = col_name.replace('"', '""')
                        cursor.execute(f'ALTER TABLE evolution_units ADD COLUMN "{safe}" {col_type}')
                    except sqlite3.OperationalError:
                        pass
                cursor.executescript("""
                    CREATE TABLE IF NOT EXISTS epigenetic_markers (
                        id TEXT PRIMARY KEY, unit_id TEXT NOT NULL,
                        context_type TEXT NOT NULL,
                        expression_weight REAL NOT NULL DEFAULT 1.0,
                        activation_state TEXT NOT NULL DEFAULT 'neutral',
                        reversible INTEGER NOT NULL DEFAULT 1,
                        expires_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_epi_unit ON epigenetic_markers(unit_id);
                    CREATE TABLE IF NOT EXISTS group_fitness_runs (
                        id TEXT PRIMARY KEY, family TEXT NOT NULL,
                        scope_type TEXT NOT NULL, scope_id TEXT NOT NULL,
                        metrics_json TEXT NOT NULL, score REAL NOT NULL,
                        window_start REAL NOT NULL, window_end REAL NOT NULL,
                        created_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_gfit_family ON group_fitness_runs(family);
                    CREATE TABLE IF NOT EXISTS multilevel_selection_runs (
                        id TEXT PRIMARY KEY, unit_id TEXT NOT NULL,
                        individual_fitness_run_id TEXT NOT NULL,
                        group_fitness_run_id TEXT NOT NULL,
                        alpha REAL NOT NULL, total_score REAL NOT NULL,
                        created_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS criticality_snapshots (
                        id TEXT PRIMARY KEY, scope_type TEXT NOT NULL,
                        scope_id TEXT NOT NULL,
                        cascade_frequency REAL NOT NULL DEFAULT 0.0,
                        correlation_length REAL NOT NULL DEFAULT 0.0,
                        distance_to_critical REAL NOT NULL DEFAULT 1.0,
                        status TEXT NOT NULL DEFAULT 'stable',
                        created_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS gene_culture_transmissions (
                        id TEXT PRIMARY KEY, unit_id TEXT NOT NULL,
                        transmission_kind TEXT NOT NULL,
                        source_scope TEXT NOT NULL, target_scope TEXT NOT NULL,
                        decision TEXT NOT NULL, created_at REAL NOT NULL
                    );
                """)
                cursor.execute("UPDATE schema_version SET version = 14")
                logger.info("Migrated state.db to schema v14 (ED v1.1 tables)")

        # Unique title index — always ensure it exists (safe to run after migrations
        # since the title column is guaranteed to exist at this point)
        try:
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_title_unique "
                "ON sessions(title) WHERE title IS NOT NULL"
            )
        except sqlite3.OperationalError:
            pass  # Index already exists

        # FTS5 setup (separate because CREATE VIRTUAL TABLE can't be in executescript with IF NOT EXISTS reliably)
        try:
            cursor.execute("SELECT * FROM messages_fts LIMIT 0")
        except sqlite3.OperationalError:
            cursor.executescript(FTS_SQL)

        self._conn.commit()

    def close(self):
        """Close the database connection."""
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    # =========================================================================
    # Session lifecycle
    # =========================================================================

    def create_session(
        self,
        session_id: str,
        source: str,
        model: str = None,
        model_config: Dict[str, Any] = None,
        system_prompt: str = None,
        user_id: str = None,
        parent_session_id: str = None,
    ) -> str:
        """Create a new session record. Returns the session_id."""
        def _do(conn):
            conn.execute(
                """INSERT OR IGNORE INTO sessions (id, source, user_id, model, model_config,
                   system_prompt, parent_session_id, started_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    source,
                    user_id,
                    model,
                    json.dumps(model_config) if model_config else None,
                    system_prompt,
                    parent_session_id,
                    time.time(),
                ),
            )
        self._execute_write(_do)
        return session_id

    def end_session(self, session_id: str, end_reason: str) -> None:
        """Mark a session as ended and cancel any orphaned in-flight tasks.

        Brain-tracked tasks left in running/planned/triaged when a session
        rolls over (typically via compression) would otherwise stay in that
        state forever, polluting world_state and pattern_mining. Cancel
        them in the same write so the task_transitions log stays honest.
        """
        def _do(conn):
            now = time.time()
            conn.execute(
                "UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ?",
                (now, end_reason, session_id),
            )
            orphans = conn.execute(
                """SELECT id, status FROM tasks
                   WHERE session_id = ?
                     AND status IN ('running','planned','triaged','verifying','blocked')""",
                (session_id,),
            ).fetchall()
            cancel_reason = f"session_ended_during_execution (end_reason={end_reason})"
            for row in orphans:
                tid = row["id"] if hasattr(row, "keys") else row[0]
                old = row["status"] if hasattr(row, "keys") else row[1]
                conn.execute(
                    """UPDATE tasks
                       SET status = 'cancelled', updated_at = ?, completed_at = ?,
                           failure_reason = ?
                       WHERE id = ?""",
                    (now, now, cancel_reason, tid),
                )
                conn.execute(
                    """INSERT INTO task_transitions
                       (task_id, from_state, to_state, reason, created_at)
                       VALUES (?, ?, 'cancelled', ?, ?)""",
                    (tid, old, cancel_reason, now),
                )
        self._execute_write(_do)

    def reopen_session(self, session_id: str) -> None:
        """Clear ended_at/end_reason so a session can be resumed."""
        def _do(conn):
            conn.execute(
                "UPDATE sessions SET ended_at = NULL, end_reason = NULL WHERE id = ?",
                (session_id,),
            )
        self._execute_write(_do)

    def update_system_prompt(self, session_id: str, system_prompt: str) -> None:
        """Store the full assembled system prompt snapshot."""
        def _do(conn):
            conn.execute(
                "UPDATE sessions SET system_prompt = ? WHERE id = ?",
                (system_prompt, session_id),
            )
        self._execute_write(_do)

    def update_token_counts(
        self,
        session_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        model: str = None,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
        estimated_cost_usd: Optional[float] = None,
        actual_cost_usd: Optional[float] = None,
        cost_status: Optional[str] = None,
        cost_source: Optional[str] = None,
        pricing_version: Optional[str] = None,
        billing_provider: Optional[str] = None,
        billing_base_url: Optional[str] = None,
        billing_mode: Optional[str] = None,
        absolute: bool = False,
    ) -> None:
        """Update token counters and backfill model if not already set.

        When *absolute* is False (default), values are **incremented** — use
        this for per-API-call deltas (CLI path).

        When *absolute* is True, values are **set directly** — use this when
        the caller already holds cumulative totals (gateway path, where the
        cached agent accumulates across messages).
        """
        if absolute:
            sql = """UPDATE sessions SET
                   input_tokens = ?,
                   output_tokens = ?,
                   cache_read_tokens = ?,
                   cache_write_tokens = ?,
                   reasoning_tokens = ?,
                   estimated_cost_usd = COALESCE(?, 0),
                   actual_cost_usd = CASE
                       WHEN ? IS NULL THEN actual_cost_usd
                       ELSE ?
                   END,
                   cost_status = COALESCE(?, cost_status),
                   cost_source = COALESCE(?, cost_source),
                   pricing_version = COALESCE(?, pricing_version),
                   billing_provider = COALESCE(billing_provider, ?),
                   billing_base_url = COALESCE(billing_base_url, ?),
                   billing_mode = COALESCE(billing_mode, ?),
                   model = COALESCE(model, ?)
                   WHERE id = ?"""
        else:
            sql = """UPDATE sessions SET
                   input_tokens = input_tokens + ?,
                   output_tokens = output_tokens + ?,
                   cache_read_tokens = cache_read_tokens + ?,
                   cache_write_tokens = cache_write_tokens + ?,
                   reasoning_tokens = reasoning_tokens + ?,
                   estimated_cost_usd = COALESCE(estimated_cost_usd, 0) + COALESCE(?, 0),
                   actual_cost_usd = CASE
                       WHEN ? IS NULL THEN actual_cost_usd
                       ELSE COALESCE(actual_cost_usd, 0) + ?
                   END,
                   cost_status = COALESCE(?, cost_status),
                   cost_source = COALESCE(?, cost_source),
                   pricing_version = COALESCE(?, pricing_version),
                   billing_provider = COALESCE(billing_provider, ?),
                   billing_base_url = COALESCE(billing_base_url, ?),
                   billing_mode = COALESCE(billing_mode, ?),
                   model = COALESCE(model, ?)
                   WHERE id = ?"""
        params = (
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_write_tokens,
            reasoning_tokens,
            estimated_cost_usd,
            actual_cost_usd,
            actual_cost_usd,
            cost_status,
            cost_source,
            pricing_version,
            billing_provider,
            billing_base_url,
            billing_mode,
            model,
            session_id,
        )
        def _do(conn):
            conn.execute(sql, params)
        self._execute_write(_do)

    def ensure_session(
        self,
        session_id: str,
        source: str = "unknown",
        model: str = None,
    ) -> None:
        """Ensure a session row exists, creating it with minimal metadata if absent.

        Used by _flush_messages_to_session_db to recover from a failed
        create_session() call (e.g. transient SQLite lock at agent startup).
        INSERT OR IGNORE is safe to call even when the row already exists.
        """
        def _do(conn):
            conn.execute(
                """INSERT OR IGNORE INTO sessions
                   (id, source, model, started_at)
                   VALUES (?, ?, ?, ?)""",
                (session_id, source, model, time.time()),
            )
        self._execute_write(_do)

    def set_token_counts(
        self,
        session_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        model: str = None,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
        estimated_cost_usd: Optional[float] = None,
        actual_cost_usd: Optional[float] = None,
        cost_status: Optional[str] = None,
        cost_source: Optional[str] = None,
        pricing_version: Optional[str] = None,
        billing_provider: Optional[str] = None,
        billing_base_url: Optional[str] = None,
        billing_mode: Optional[str] = None,
    ) -> None:
        """Set token counters to absolute values (not increment).

        Use this when the caller provides cumulative totals from a completed
        conversation run (e.g. the gateway, where the cached agent's
        session_prompt_tokens already reflects the running total).
        """
        def _do(conn):
            conn.execute(
                """UPDATE sessions SET
                   input_tokens = ?,
                   output_tokens = ?,
                   cache_read_tokens = ?,
                   cache_write_tokens = ?,
                   reasoning_tokens = ?,
                   estimated_cost_usd = ?,
                   actual_cost_usd = CASE
                       WHEN ? IS NULL THEN actual_cost_usd
                       ELSE ?
                   END,
                   cost_status = COALESCE(?, cost_status),
                   cost_source = COALESCE(?, cost_source),
                   pricing_version = COALESCE(?, pricing_version),
                   billing_provider = COALESCE(billing_provider, ?),
                   billing_base_url = COALESCE(billing_base_url, ?),
                   billing_mode = COALESCE(billing_mode, ?),
                   model = COALESCE(model, ?)
                   WHERE id = ?""",
                (
                    input_tokens,
                    output_tokens,
                    cache_read_tokens,
                    cache_write_tokens,
                    reasoning_tokens,
                    estimated_cost_usd,
                    actual_cost_usd,
                    actual_cost_usd,
                    cost_status,
                    cost_source,
                    pricing_version,
                    billing_provider,
                    billing_base_url,
                    billing_mode,
                    model,
                    session_id,
                ),
            )
        self._execute_write(_do)

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get a session by ID."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            )
            row = cursor.fetchone()
        return dict(row) if row else None

    def resolve_session_id(self, session_id_or_prefix: str) -> Optional[str]:
        """Resolve an exact or uniquely prefixed session ID to the full ID.

        Returns the exact ID when it exists. Otherwise treats the input as a
        prefix and returns the single matching session ID if the prefix is
        unambiguous. Returns None for no matches or ambiguous prefixes.
        """
        exact = self.get_session(session_id_or_prefix)
        if exact:
            return exact["id"]

        escaped = (
            session_id_or_prefix
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        with self._lock:
            cursor = self._conn.execute(
                "SELECT id FROM sessions WHERE id LIKE ? ESCAPE '\\' ORDER BY started_at DESC LIMIT 2",
                (f"{escaped}%",),
            )
            matches = [row["id"] for row in cursor.fetchall()]
        if len(matches) == 1:
            return matches[0]
        return None

    # Maximum length for session titles
    MAX_TITLE_LENGTH = 100

    @staticmethod
    def sanitize_title(title: Optional[str]) -> Optional[str]:
        """Validate and sanitize a session title.

        - Strips leading/trailing whitespace
        - Removes ASCII control characters (0x00-0x1F, 0x7F) and problematic
          Unicode control chars (zero-width, RTL/LTR overrides, etc.)
        - Collapses internal whitespace runs to single spaces
        - Normalizes empty/whitespace-only strings to None
        - Enforces MAX_TITLE_LENGTH

        Returns the cleaned title string or None.
        Raises ValueError if the title exceeds MAX_TITLE_LENGTH after cleaning.
        """
        if not title:
            return None

        # Remove ASCII control characters (0x00-0x1F, 0x7F) but keep
        # whitespace chars (\t=0x09, \n=0x0A, \r=0x0D) so they can be
        # normalized to spaces by the whitespace collapsing step below
        cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', title)

        # Remove problematic Unicode control characters:
        # - Zero-width chars (U+200B-U+200F, U+FEFF)
        # - Directional overrides (U+202A-U+202E, U+2066-U+2069)
        # - Object replacement (U+FFFC), interlinear annotation (U+FFF9-U+FFFB)
        cleaned = re.sub(
            r'[\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff\ufffc\ufff9-\ufffb]',
            '', cleaned,
        )

        # Collapse internal whitespace runs and strip
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()

        if not cleaned:
            return None

        if len(cleaned) > SessionDB.MAX_TITLE_LENGTH:
            raise ValueError(
                f"Title too long ({len(cleaned)} chars, max {SessionDB.MAX_TITLE_LENGTH})"
            )

        return cleaned

    def set_session_title(self, session_id: str, title: str) -> bool:
        """Set or update a session's title.

        Returns True if session was found and title was set.
        Raises ValueError if title is already in use by another session,
        or if the title fails validation (too long, invalid characters).
        Empty/whitespace-only strings are normalized to None (clearing the title).
        """
        title = self.sanitize_title(title)
        def _do(conn):
            if title:
                # Check uniqueness (allow the same session to keep its own title)
                cursor = conn.execute(
                    "SELECT id FROM sessions WHERE title = ? AND id != ?",
                    (title, session_id),
                )
                conflict = cursor.fetchone()
                if conflict:
                    raise ValueError(
                        f"Title '{title}' is already in use by session {conflict['id']}"
                    )
            cursor = conn.execute(
                "UPDATE sessions SET title = ? WHERE id = ?",
                (title, session_id),
            )
            return cursor.rowcount
        rowcount = self._execute_write(_do)
        return rowcount > 0

    def get_session_title(self, session_id: str) -> Optional[str]:
        """Get the title for a session, or None."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT title FROM sessions WHERE id = ?", (session_id,)
            )
            row = cursor.fetchone()
        return row["title"] if row else None

    def get_session_by_title(self, title: str) -> Optional[Dict[str, Any]]:
        """Look up a session by exact title. Returns session dict or None."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM sessions WHERE title = ?", (title,)
            )
            row = cursor.fetchone()
        return dict(row) if row else None

    def resolve_session_by_title(self, title: str) -> Optional[str]:
        """Resolve a title to a session ID, preferring the latest in a lineage.

        If the exact title exists, returns that session's ID.
        If not, searches for "title #N" variants and returns the latest one.
        If the exact title exists AND numbered variants exist, returns the
        latest numbered variant (the most recent continuation).
        """
        # First try exact match
        exact = self.get_session_by_title(title)

        # Also search for numbered variants: "title #2", "title #3", etc.
        # Escape SQL LIKE wildcards (%, _) in the title to prevent false matches
        escaped = title.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._lock:
            cursor = self._conn.execute(
                "SELECT id, title, started_at FROM sessions "
                "WHERE title LIKE ? ESCAPE '\\' ORDER BY started_at DESC",
                (f"{escaped} #%",),
            )
            numbered = cursor.fetchall()

        if numbered:
            # Return the most recent numbered variant
            return numbered[0]["id"]
        elif exact:
            return exact["id"]
        return None

    def get_next_title_in_lineage(self, base_title: str) -> str:
        """Generate the next title in a lineage (e.g., "my session" → "my session #2").

        Strips any existing " #N" suffix to find the base name, then finds
        the highest existing number and increments.
        """
        # Strip existing #N suffix to find the true base
        match = re.match(r'^(.*?) #(\d+)$', base_title)
        if match:
            base = match.group(1)
        else:
            base = base_title

        # Find all existing numbered variants
        # Escape SQL LIKE wildcards (%, _) in the base to prevent false matches
        escaped = base.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._lock:
            cursor = self._conn.execute(
                "SELECT title FROM sessions WHERE title = ? OR title LIKE ? ESCAPE '\\'",
                (base, f"{escaped} #%"),
            )
            existing = [row["title"] for row in cursor.fetchall()]

        if not existing:
            return base  # No conflict, use the base name as-is

        # Find the highest number
        max_num = 1  # The unnumbered original counts as #1
        for t in existing:
            m = re.match(r'^.* #(\d+)$', t)
            if m:
                max_num = max(max_num, int(m.group(1)))

        return f"{base} #{max_num + 1}"

    def list_sessions_rich(
        self,
        source: str = None,
        exclude_sources: List[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List sessions with preview (first user message) and last active timestamp.

        Returns dicts with keys: id, source, model, title, started_at, ended_at,
        message_count, preview (first 60 chars of first user message),
        last_active (timestamp of last message).

        Uses a single query with correlated subqueries instead of N+2 queries.
        """
        where_clauses = []
        params = []

        if source:
            where_clauses.append("s.source = ?")
            params.append(source)
        if exclude_sources:
            placeholders = ",".join("?" for _ in exclude_sources)
            where_clauses.append(f"s.source NOT IN ({placeholders})")
            params.extend(exclude_sources)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        query = f"""
            SELECT s.*,
                COALESCE(
                    (SELECT SUBSTR(REPLACE(REPLACE(m.content, X'0A', ' '), X'0D', ' '), 1, 63)
                     FROM messages m
                     WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL
                     ORDER BY m.timestamp, m.id LIMIT 1),
                    ''
                ) AS _preview_raw,
                COALESCE(
                    (SELECT MAX(m2.timestamp) FROM messages m2 WHERE m2.session_id = s.id),
                    s.started_at
                ) AS last_active
            FROM sessions s
            {where_sql}
            ORDER BY s.started_at DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])
        with self._lock:
            cursor = self._conn.execute(query, params)
            rows = cursor.fetchall()
        sessions = []
        for row in rows:
            s = dict(row)
            # Build the preview from the raw substring
            raw = s.pop("_preview_raw", "").strip()
            if raw:
                text = raw[:60]
                s["preview"] = text + ("..." if len(raw) > 60 else "")
            else:
                s["preview"] = ""
            sessions.append(s)

        return sessions

    # =========================================================================
    # Message storage
    # =========================================================================

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str = None,
        tool_name: str = None,
        tool_calls: Any = None,
        tool_call_id: str = None,
        token_count: int = None,
        finish_reason: str = None,
        reasoning: str = None,
        reasoning_details: Any = None,
        codex_reasoning_items: Any = None,
    ) -> int:
        """
        Append a message to a session. Returns the message row ID.

        Also increments the session's message_count (and tool_call_count
        if role is 'tool' or tool_calls is present).
        """
        # Serialize structured fields to JSON before entering the write txn
        reasoning_details_json = (
            json.dumps(reasoning_details)
            if reasoning_details else None
        )
        codex_items_json = (
            json.dumps(codex_reasoning_items)
            if codex_reasoning_items else None
        )
        tool_calls_json = json.dumps(tool_calls) if tool_calls else None

        # Pre-compute tool call count
        num_tool_calls = 0
        if tool_calls is not None:
            num_tool_calls = len(tool_calls) if isinstance(tool_calls, list) else 1

        def _do(conn):
            cursor = conn.execute(
                """INSERT INTO messages (session_id, role, content, tool_call_id,
                   tool_calls, tool_name, timestamp, token_count, finish_reason,
                   reasoning, reasoning_details, codex_reasoning_items)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    role,
                    content,
                    tool_call_id,
                    tool_calls_json,
                    tool_name,
                    time.time(),
                    token_count,
                    finish_reason,
                    reasoning,
                    reasoning_details_json,
                    codex_items_json,
                ),
            )
            msg_id = cursor.lastrowid

            # Update counters
            if num_tool_calls > 0:
                conn.execute(
                    """UPDATE sessions SET message_count = message_count + 1,
                       tool_call_count = tool_call_count + ? WHERE id = ?""",
                    (num_tool_calls, session_id),
                )
            else:
                conn.execute(
                    "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?",
                    (session_id,),
                )
            return msg_id

        return self._execute_write(_do)

    def get_messages(self, session_id: str) -> List[Dict[str, Any]]:
        """Load all messages for a session, ordered by timestamp."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY timestamp, id",
                (session_id,),
            )
            rows = cursor.fetchall()
        result = []
        for row in rows:
            msg = dict(row)
            if msg.get("tool_calls"):
                try:
                    msg["tool_calls"] = json.loads(msg["tool_calls"])
                except (json.JSONDecodeError, TypeError):
                    pass
            result.append(msg)
        return result

    def get_messages_as_conversation(self, session_id: str) -> List[Dict[str, Any]]:
        """
        Load messages in the OpenAI conversation format (role + content dicts).
        Used by the gateway to restore conversation history.
        """
        with self._lock:
            cursor = self._conn.execute(
                "SELECT role, content, tool_call_id, tool_calls, tool_name, "
                "reasoning, reasoning_details, codex_reasoning_items "
                "FROM messages WHERE session_id = ? ORDER BY timestamp, id",
                (session_id,),
            )
            rows = cursor.fetchall()
        messages = []
        for row in rows:
            msg = {"role": row["role"], "content": row["content"]}
            if row["tool_call_id"]:
                msg["tool_call_id"] = row["tool_call_id"]
            if row["tool_name"]:
                msg["tool_name"] = row["tool_name"]
            if row["tool_calls"]:
                try:
                    msg["tool_calls"] = json.loads(row["tool_calls"])
                except (json.JSONDecodeError, TypeError):
                    pass
            # Restore reasoning fields on assistant messages so providers
            # that replay reasoning (OpenRouter, OpenAI, Nous) receive
            # coherent multi-turn reasoning context.
            if row["role"] == "assistant":
                if row["reasoning"]:
                    msg["reasoning"] = row["reasoning"]
                if row["reasoning_details"]:
                    try:
                        msg["reasoning_details"] = json.loads(row["reasoning_details"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                if row["codex_reasoning_items"]:
                    try:
                        msg["codex_reasoning_items"] = json.loads(row["codex_reasoning_items"])
                    except (json.JSONDecodeError, TypeError):
                        pass
            messages.append(msg)
        return messages

    # =========================================================================
    # Search
    # =========================================================================

    @staticmethod
    def _sanitize_fts5_query(query: str) -> str:
        """Sanitize user input for safe use in FTS5 MATCH queries.

        FTS5 has its own query syntax where characters like ``"``, ``(``, ``)``,
        ``+``, ``*``, ``{``, ``}`` and bare boolean operators (``AND``, ``OR``,
        ``NOT``) have special meaning.  Passing raw user input directly to
        MATCH can cause ``sqlite3.OperationalError``.

        Strategy:
        - Preserve properly paired quoted phrases (``"exact phrase"``)
        - Strip unmatched FTS5-special characters that would cause errors
        - Wrap unquoted hyphenated terms in quotes so FTS5 matches them
          as exact phrases instead of splitting on the hyphen
        """
        # Step 1: Extract balanced double-quoted phrases and protect them
        # from further processing via numbered placeholders.
        _quoted_parts: list = []

        def _preserve_quoted(m: re.Match) -> str:
            _quoted_parts.append(m.group(0))
            return f"\x00Q{len(_quoted_parts) - 1}\x00"

        sanitized = re.sub(r'"[^"]*"', _preserve_quoted, query)

        # Step 2: Strip remaining (unmatched) FTS5-special characters
        sanitized = re.sub(r'[+{}()\"^]', " ", sanitized)

        # Step 3: Collapse repeated * (e.g. "***") into a single one,
        # and remove leading * (prefix-only needs at least one char before *)
        sanitized = re.sub(r"\*+", "*", sanitized)
        sanitized = re.sub(r"(^|\s)\*", r"\1", sanitized)

        # Step 4: Remove dangling boolean operators at start/end that would
        # cause syntax errors (e.g. "hello AND" or "OR world")
        sanitized = re.sub(r"(?i)^(AND|OR|NOT)\b\s*", "", sanitized.strip())
        sanitized = re.sub(r"(?i)\s+(AND|OR|NOT)\s*$", "", sanitized.strip())

        # Step 5: Wrap unquoted hyphenated terms (e.g. ``chat-send``) in
        # double quotes.  FTS5's tokenizer splits on hyphens, turning
        # ``chat-send`` into ``chat AND send``.  Quoting preserves the
        # intended phrase match.
        sanitized = re.sub(r"\b(\w+(?:-\w+)+)\b", r'"\1"', sanitized)

        # Step 6: Restore preserved quoted phrases
        for i, quoted in enumerate(_quoted_parts):
            sanitized = sanitized.replace(f"\x00Q{i}\x00", quoted)

        return sanitized.strip()

    def search_messages(
        self,
        query: str,
        source_filter: List[str] = None,
        exclude_sources: List[str] = None,
        role_filter: List[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        Full-text search across session messages using FTS5.

        Supports FTS5 query syntax:
          - Simple keywords: "docker deployment"
          - Phrases: '"exact phrase"'
          - Boolean: "docker OR kubernetes", "python NOT java"
          - Prefix: "deploy*"

        Returns matching messages with session metadata, content snippet,
        and surrounding context (1 message before and after the match).
        """
        if not query or not query.strip():
            return []

        query = self._sanitize_fts5_query(query)
        if not query:
            return []

        # Build WHERE clauses dynamically
        where_clauses = ["messages_fts MATCH ?"]
        params: list = [query]

        if source_filter is not None:
            source_placeholders = ",".join("?" for _ in source_filter)
            where_clauses.append(f"s.source IN ({source_placeholders})")
            params.extend(source_filter)

        if exclude_sources is not None:
            exclude_placeholders = ",".join("?" for _ in exclude_sources)
            where_clauses.append(f"s.source NOT IN ({exclude_placeholders})")
            params.extend(exclude_sources)

        if role_filter:
            role_placeholders = ",".join("?" for _ in role_filter)
            where_clauses.append(f"m.role IN ({role_placeholders})")
            params.extend(role_filter)

        where_sql = " AND ".join(where_clauses)
        params.extend([limit, offset])

        sql = f"""
            SELECT
                m.id,
                m.session_id,
                m.role,
                snippet(messages_fts, 0, '>>>', '<<<', '...', 40) AS snippet,
                m.content,
                m.timestamp,
                m.tool_name,
                s.source,
                s.model,
                s.started_at AS session_started
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            JOIN sessions s ON s.id = m.session_id
            WHERE {where_sql}
            ORDER BY
                -- CMA temporal decay: blend FTS5 rank with recency
                -- rank < 0 (more negative = better FTS match), so we negate it
                -- decay = exp(-0.05 * days_old): half-life ~14 days
                -- sessions < 3 days old get near-full weight; 30-day-old sessions ~22% weight
                ((-rank) * exp(-0.05 * MAX(0, (unixepoch('now') - CAST(s.started_at AS REAL)) / 86400.0))) DESC
            LIMIT ? OFFSET ?
        """

        with self._lock:
            try:
                cursor = self._conn.execute(sql, params)
            except sqlite3.OperationalError:
                # FTS5 query syntax error despite sanitization — return empty
                return []
            matches = [dict(row) for row in cursor.fetchall()]

        # Add surrounding context (1 message before + after each match).
        # Done outside the lock so we don't hold it across N sequential queries.
        for match in matches:
            try:
                with self._lock:
                    ctx_cursor = self._conn.execute(
                        """SELECT role, content FROM messages
                           WHERE session_id = ? AND id >= ? - 1 AND id <= ? + 1
                           ORDER BY id""",
                        (match["session_id"], match["id"], match["id"]),
                    )
                    context_msgs = [
                        {"role": r["role"], "content": (r["content"] or "")[:200]}
                        for r in ctx_cursor.fetchall()
                    ]
                match["context"] = context_msgs
            except Exception:
                match["context"] = []

        # Remove full content from result (snippet is enough, saves tokens)
        for match in matches:
            match.pop("content", None)

        return matches

    def search_sessions(
        self,
        source: str = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List sessions, optionally filtered by source."""
        with self._lock:
            if source:
                cursor = self._conn.execute(
                    "SELECT * FROM sessions WHERE source = ? ORDER BY started_at DESC LIMIT ? OFFSET ?",
                    (source, limit, offset),
                )
            else:
                cursor = self._conn.execute(
                    "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                )
            return [dict(row) for row in cursor.fetchall()]

    # =========================================================================
    # Utility
    # =========================================================================

    def session_count(self, source: str = None) -> int:
        """Count sessions, optionally filtered by source."""
        with self._lock:
            if source:
                cursor = self._conn.execute(
                    "SELECT COUNT(*) FROM sessions WHERE source = ?", (source,)
                )
            else:
                cursor = self._conn.execute("SELECT COUNT(*) FROM sessions")
            return cursor.fetchone()[0]

    def message_count(self, session_id: str = None) -> int:
        """Count messages, optionally for a specific session."""
        with self._lock:
            if session_id:
                cursor = self._conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
                )
            else:
                cursor = self._conn.execute("SELECT COUNT(*) FROM messages")
            return cursor.fetchone()[0]

    # =========================================================================
    # Export and cleanup
    # =========================================================================

    def export_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Export a single session with all its messages as a dict."""
        session = self.get_session(session_id)
        if not session:
            return None
        messages = self.get_messages(session_id)
        return {**session, "messages": messages}

    def export_all(self, source: str = None) -> List[Dict[str, Any]]:
        """
        Export all sessions (with messages) as a list of dicts.
        Suitable for writing to a JSONL file for backup/analysis.
        """
        sessions = self.search_sessions(source=source, limit=100000)
        results = []
        for session in sessions:
            messages = self.get_messages(session["id"])
            results.append({**session, "messages": messages})
        return results

    def clear_messages(self, session_id: str) -> None:
        """Delete all messages for a session and reset its counters."""
        def _do(conn):
            conn.execute(
                "DELETE FROM messages WHERE session_id = ?", (session_id,)
            )
            conn.execute(
                "UPDATE sessions SET message_count = 0, tool_call_count = 0 WHERE id = ?",
                (session_id,),
            )
        self._execute_write(_do)

    def delete_session(self, session_id: str) -> bool:
        """Delete a session and all its messages. Returns True if found."""
        def _do(conn):
            cursor = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE id = ?", (session_id,)
            )
            if cursor.fetchone()[0] == 0:
                return False
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            return True
        return self._execute_write(_do)

    def prune_sessions(self, older_than_days: int = 90, source: str = None) -> int:
        """
        Delete sessions older than N days. Returns count of deleted sessions.
        Only prunes ended sessions (not active ones).
        """
        cutoff = time.time() - (older_than_days * 86400)

        def _do(conn):
            if source:
                cursor = conn.execute(
                    """SELECT id FROM sessions
                       WHERE started_at < ? AND ended_at IS NOT NULL AND source = ?""",
                    (cutoff, source),
                )
            else:
                cursor = conn.execute(
                    "SELECT id FROM sessions WHERE started_at < ? AND ended_at IS NOT NULL",
                    (cutoff,),
                )
            session_ids = [row["id"] for row in cursor.fetchall()]

            for sid in session_ids:
                conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
                conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
            return len(session_ids)

        return self._execute_write(_do)
