# Hermes Stability V1 Documentation

## What Changed

Hermes stability v1 formalizes the service around one supported runtime path and one supported operational surface.

Key outcomes:

- one formal gateway startup path
- deterministic operator scripts
- startup fingerprint visibility
- stale state cleanup before startup
- bounded health monitor with restart budget
- preflight gate before restart
- request retry/backoff/dedupe
- structured tool input and output validation
- task state transition journal for background jobs
- smoke/regression/integration test contracts

## Operator Entry Points

- `scripts/hermes_up.sh`
- `scripts/hermes_stop.sh`
- `scripts/hermes_restart.sh`
- `scripts/hermes_status.sh`
- `scripts/hermes_logs.sh`
- `scripts/hermes_smoke_test.sh`

These wrap the gateway runtime entrypoints and should be treated as the human-facing v1 ops surface.

## Structured Contracts

JSON schemas live in [`schemas/`](schemas):

- `plan_result.schema.json`
- `tool_selection.schema.json`
- `tool_input.schema.json`
- `tool_output.schema.json`
- `final_response.schema.json`
- `error_report.schema.json`

The registry now validates:

- tool input before dispatch
- tool output parseability after dispatch
- tool output schema when provided

## Task State Machine

Canonical states:

- `queued`
- `running`
- `waiting_model`
- `waiting_tool`
- `retrying`
- `completed`
- `failed`
- `aborted`

Background process sessions now record a transition journal with:

- `task_id`
- `from_state`
- `to_state`
- `reason`
- `request_id`
- `tool_name`
- `attempt_no`
- `timestamp`

## Known Limits

- full provider-wide Responses API background mode is not universal yet
- webhook completion handling remains platform-specific
- legacy modules still exist in the repo, but the supported startup/ops path is the gateway flow documented in the runbook

## Primary Docs

- [`README.md`](README.md)
- [`docs/GATEWAY_OPERATIONS_RUNBOOK.md`](docs/GATEWAY_OPERATIONS_RUNBOOK.md)
- [`IMPLEMENT.md`](IMPLEMENT.md)
