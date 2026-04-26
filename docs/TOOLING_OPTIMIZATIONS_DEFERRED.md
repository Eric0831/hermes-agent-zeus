# Tooling Optimizations — Deferred Work

This doc captures Tier 3 optimizations that were considered but deferred
during the 2026-04-26 tooling overhaul. Each section explains the design,
why it wasn't shipped, and the trigger condition that should reopen it.

## Already shipped (this session)

- **L1-L4 protection** (3a9ebf77) — commit-time test, startup smoke,
  runtime circuit breaker, weekly poisoned-session detector
- **Schema hints + did-you-mean** (3d55d7b8) — registry returns
  `expected_args_example` on validation fail, `did_you_mean` on unknown tool
- **Within-turn dedup cache** (3d55d7b8) — 16 idempotent read tools
  cached for one assistant turn
- **Result size cap 50KB** (3d55d7b8) — head/tail truncate above
- **Per-tool timeout** (7b62be1f) — 19 tuned, 60s default, returns
  structured `tool_timeout` payload
- **Transient-error retry × 1, 1s backoff** (7b62be1f) — TimeoutError,
  503/502/504/429, rate-limit, connection errors retried; programming
  errors not
- **LLM-summarize > 100KB** (9b3cef1a) — 14 summarization-friendly tools
  hand result to 9B compression instead of head/tail dropping middle.
  Content-hash cache, fallback to truncate if summarizer errors.
- **Tavily → DDG/httpx fallback** (bf355986, feffae55) — transparent
  backend swap when Tavily 432 quota exhausted

## #7 Lazy MCP discovery (deferred)

**Status**: not implemented.

**Idea**: don't spawn MCP servers (zeus, ddg, fetch, sqlite, filesystem,
git) at gateway startup; spawn each on first tool call referencing it.

**Why deferred**:
- High refactor surface — `tools/mcp_tool.py:discover_mcp_tools()`
  currently parallel-spawns at module import via `model_tools.py:177`.
  Lazy load means restructuring the dispatch path to detect "server not
  yet up → spawn → wait → call".
- Concurrency risk — if two parallel tool calls hit a not-yet-spawned
  server simultaneously, need careful locking to avoid double spawn.
- First-call latency — each MCP tool's first invocation gains ~1-2s
  cold-start. Defeats the dedup-cache speedup we just shipped.
- Diminishing returns — gateway has plenty of RAM headroom (system has
  503GB total, gateway peak ~3GB), and the 6 MCP servers add maybe 200MB
  total. Startup is already <10s.

**Reopen when**: gateway memory pressure becomes real, OR startup time
matters (e.g. frequent cold-starts in CI/test envs), OR adding 10+ more
MCP servers tips the balance.

**Reference design** (when ready):
- Replace eager `_servers[name] = server` with `_server_factories[name] = cfg`
- New `_get_server(name)` lazy-spawns under `_lock` if not present
- Tool dispatch path: `await _get_server(name).call_tool(...)`
- Add server-level idle-timeout to also stop unused servers after N minutes

## #8 Health-aware tool router (deferred)

**Status**: not implemented (manual fallback exists for web tools only).

**Idea**: when multiple backends exist for the same task (web_search has
Tavily, DDG, Exa, Firecrawl), track per-backend rolling success rate and
auto-promote the healthiest. Demote a backend that fails repeatedly;
auto-promote it back when it recovers.

**Why deferred**:
- We already have transparent fallback (Tavily quota → DDG / httpx) at
  the call site. That handles the common case.
- True health-aware needs persistent metrics store (rolling window per
  backend), monitoring loop, demotion thresholds, recovery probes —
  significant scaffolding for marginal gain over current fallback.
- Most multi-backend tools have ONE primary that's clearly better (Tavily
  vs DDG: Tavily is higher quality when working). Switching primary
  hurts result quality more than it helps.

**Reopen when**: multiple backends become equally viable AND we're seeing
sustained outages on the primary that the simple fallback doesn't catch.

**Reference design** (when ready):
- Add `tools/backend_health.py` — per-backend `BackendStats(success_count,
  failure_count, last_failure_ts)` in shared dict
- After each call, update stats; if backend.failure_rate > 0.5 in
  last 20 calls, demote
- Periodic recovery probe: every 5 min, send a cheap call to demoted
  backend; on success, reinstate
- `_get_backend()` consults health, returns highest-priority healthy
  backend instead of static config

## #10 Batch tool API (deferred)

**Status**: not implemented.

**Idea**: allow `read_files([a, b, c])` instead of `read_file(a)` ×3.
Reduces LLM round trips: agent emits one tool call returning multiple
results.

**Why deferred**:
- Disruptive — every tool that's batch-able needs new schema, new
  handler signature, and the LLM has to learn to use it. Toolset gets
  duplicated (read_file vs read_files).
- The dedup cache (within-turn) already mitigates the worst case (same
  file × 2). LLMs rarely emit 5 distinct read_file in one tool_calls
  array; they sequentialise across turns.
- Concurrent execution path (`_execute_tool_calls_concurrent`) already
  parallelises independent tool calls via ThreadPoolExecutor — the wall
  time is no worse than batch.
- Maintenance: each new tool needs a batch sibling kept in sync.

**Reopen when**: clear evidence (from outcome_tracking metrics, once
accumulated for ≥1 month) shows agents emit ≥5 same-tool calls per turn
frequently AND the bottleneck is dispatch overhead (not LLM thinking time).

**Reference design** (when ready):
- Schema: `read_files(paths: List[str])` returns
  `{paths: {a: {content...}, b: {error: ...}, ...}}`
- Per-path errors don't fail the whole call
- Internally fan out to existing `read_file_tool` per path

## How to track "should we revisit?"

Wire into `zeus_weekly_health_check.py:check_poisoned_sessions()` similar
checks for the deferred items:

- **Lazy MCP**: warn if gateway RSS > 4GB or startup > 30s
- **Health router**: warn if any backend fallback rate > 30% in 7 days
- **Batch API**: warn if any single turn has >5 calls of the same tool

Then revisit when warnings appear.
