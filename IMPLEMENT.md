# Hermes Stability V1 Implementation Plan

## Scope

This document tracks only stability v1 work. It explicitly excludes feature expansion unrelated to:

- single startup path
- request resilience
- structured tool and model I/O
- task recovery/state tracking
- observability, smoke tests, rollback safety

## Milestones

### Milestone 1: Stop The Bleeding

Status: completed

- unified gateway runtime path
- formal ops scripts
- startup runtime fingerprint
- stale runtime state repair
- healthcheck and monitor loop
- preflight gate before manual and automatic restart

### Milestone 2: Structured I/O

Status: completed for v1 contracts

- tool input schema validation in `tools/registry.py`
- tool output JSON enforcement in `tools/registry.py`
- tool output schema hook support
- baseline JSON Schemas in `schemas/`

### Milestone 3: Recoverable Long Tasks

Status: partial

- existing OpenAI Responses API path retained
- request retry/backoff/dedupe in `agent/request_resilience.py`
- background process state machine strengthened in `tools/process_registry.py`
- webhook adapter already available for background-style delivery integration

Deferred beyond v1:

- provider-wide migration to Responses API background mode
- universal webhook completion receiver for every provider

### Milestone 4: Observability

Status: completed for v1

- runtime fingerprint in status
- `last_agent_failure`
- `last_monitor_check`
- `last_preflight_check`
- task state transition journal for background processes

### Milestone 5: Regression Safety

Status: completed for v1

- smoke tests for gateway scripts, preflight, healthcheck, monitor
- regression contract tests for schemas/docs/scripts
- integration test for task state machine
- deterministic `scripts/hermes_smoke_test.sh`

## Out Of Scope

- unrelated prompt redesign
- tool feature expansion
- provider-specific optimization work
- broad refactors outside gateway / task runtime / schema contracts

## Validation Commands

```bash
scripts/hermes_smoke_test.sh
python3 -m pytest tests/tools/test_registry_validation.py tests/tools/test_process_registry_state_machine.py --override-ini='addopts='
python3 -m pytest tests/gateway/test_runtime_metadata.py tests/hermes_cli/test_gateway_runtime_health.py --override-ini='addopts='
```
