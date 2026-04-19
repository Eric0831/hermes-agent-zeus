# Gateway Operations Runbook

This runbook is the operational source of truth for the managed Hermes gateway.

It covers:

- start / stop / restart / status / logs / healthcheck
- runtime fingerprint interpretation
- `error_code` handling
- monitor auto-restart policy
- common incident response
- rollback flow

## Scope

This runbook applies to the unified gateway entrypoints under [`scripts/`](./../scripts):

- [`gateway-up`](../scripts/gateway-up)
- [`gateway-stop`](../scripts/gateway-stop)
- [`gateway-status`](../scripts/gateway-status)
- [`gateway-logs`](../scripts/gateway-logs)
- [`gateway-healthcheck`](../scripts/gateway-healthcheck)
- [`gateway-preflight`](../scripts/gateway-preflight)
- [`gateway-monitor`](../scripts/gateway-monitor)

Do not use deprecated or ad-hoc startup paths when operating production agents.

## Standard Commands

Run from the repo root:

```bash
scripts/gateway-up
scripts/gateway-stop
scripts/gateway-status
scripts/gateway-logs
scripts/gateway-healthcheck
scripts/gateway-preflight
scripts/gateway-monitor
```

Equivalent CLI commands:

```bash
python3 -m hermes_cli.main gateway start
python3 -m hermes_cli.main gateway stop
python3 -m hermes_cli.main gateway status
```

## Normal Lifecycle

### Start

```bash
scripts/gateway-up
```

Expected behavior:

- stale runtime state is repaired before startup
- stale scoped locks are cleared if no live gateway exists
- startup emits runtime fingerprint:
  - `git_sha`
  - `config_hash`
  - `prompt_version`
  - `model_name`

### Stop

```bash
scripts/gateway-stop
```

Use this before deploys, config changes, or rollback.

### Restart

```bash
scripts/gateway-stop && scripts/gateway-up
```

Do not mix manual backgrounding, old shell wrappers, or multiple startup paths.

### Status

```bash
scripts/gateway-status
```

This is the first command to run during triage.

### Logs

```bash
scripts/gateway-logs
tail -f /home/testai/zeus/data/logs/hermes_gateway_monitor.log
```

Use `gateway-logs` for gateway runtime output and the monitor log for auto-recovery decisions.

### Preflight

```bash
scripts/gateway-preflight
```

Use this before deploy or rollback.

It validates:

- current checkout/config fingerprint is readable
- official gateway scripts exist and are executable
- if a gateway is already running, its fingerprint matches the desired target

If preflight fails, fix the mismatch before assuming the restart picked up the new code or config.

`scripts/gateway-up` runs this gate in human-readable mode before restart, so blocked deploys show a concise mismatch summary instead of raw JSON.

## Status Output Reference

`gateway status` surfaces three classes of information.

### Runtime identity

- `git_sha`: exact deployed code revision
- `config_hash`: fingerprint of effective config
- `prompt_version`: prompt bundle version
- `model_name`: active model identifier

If behavior looks stale after deploy, compare these four values first.

### Last agent failure

`last_agent_failure` includes:

- `error_code`
- `error`
- `session_id`
- `platform`
- `failed_at`

This is the fastest way to tell whether the last failure was retryable, configuration-related, or schema-related.

### Last monitor decision

`last_monitor_check` includes:

- `checked_at`
- `health`
- `action`
- `reason`
- `restart_count_window`
- `restart_budget_window_seconds`
- `restart_budget_max`

This tells you whether the monitor restarted the gateway, refused to restart it, or only observed degraded behavior.

### Last preflight result

`last_preflight_check` includes:

- `checked_at`
- `status`
- `exit_code`
- `issues`
- `mismatches`

This tells you whether the last deploy/restart gate failed because of runtime fingerprint mismatch, unreadable desired metadata, or missing formal scripts.

## Healthcheck Contract

Machine-readable probe:

```bash
scripts/gateway-healthcheck
```

Exit codes:

- `0`: healthy
- `1`: gateway not running
- `2`: hard failure
- `3`: degraded

Output JSON includes:

- `health`
- `gateway_state`
- `runtime_metadata`
- `last_agent_failure`

## Preflight Contract

Deploy / rollback gate:

```bash
scripts/gateway-preflight
```

Exit codes:

- `0`: ready
- `2`: failed

Output JSON includes:

- `desired_runtime_metadata`
- `running_runtime_metadata`
- `mismatches`
- `issues`
- `scripts`

## Error Code Reference

### Retryable / transient

These are usually handled by retry, monitor observation, or bounded restart.

- `timeout`
- `connection_reset`
- `provider_transient_error`
- `provider_request_failed`
- `duplicate_request_suppressed`

### Degraded but not restart-worthy by default

- `rate_limited`

This usually means wait, not restart.

### Hard failure / operator action likely required

- `provider_4xx_non_retryable`
- `schema_validation_failed`
- `local_validation_error`

These usually require config, prompt, tool schema, or provider request changes.

### Input or context problems

- `payload_too_large`
- `context_length_exceeded`
- `invalid_api_response`

These normally require prompt/context reduction or request shaping fixes.

## Monitor Policy

The monitor is a bounded auto-recovery loop:

- timer unit: `hermes-gateway-monitor.timer`
- service unit: `hermes-gateway-monitor.service`

Current policy:

- auto-restart:
  - `timeout`
  - `connection_reset`
  - `provider_transient_error`
- preflight gate:
  - monitor runs `scripts/gateway-preflight` before restart
  - if preflight fails, monitor records `preflight_failed:*` and skips restart
- no restart:
  - `rate_limited`
  - `provider_4xx_non_retryable`
  - `schema_validation_failed`
  - `local_validation_error`
  - payload/context problems
- restart budget:
  - max `3` restarts per `1 hour`

This prevents infinite crash loops while still auto-healing transient transport failures.

## Incident Playbooks

### 1. Gateway not running

Symptoms:

- `gateway-healthcheck` exits `1`
- `gateway status` shows stopped or missing PID

Actions:

1. Run `scripts/gateway-up`
2. Run `scripts/gateway-status`
3. Verify runtime fingerprint changed as expected
4. If startup fails again, inspect `scripts/gateway-logs`

### 2. Repeated timeouts or disconnects

Symptoms:

- `error_code=timeout`
- `error_code=connection_reset`
- monitor may auto-restart within budget

Actions:

1. Check `gateway status` for `last_monitor_check`
2. Confirm restart budget is not exhausted
3. Check provider/network health
4. If the same failure persists after bounded restarts, treat as upstream incident

### 3. Rate limiting

Symptoms:

- `error_code=rate_limited`
- healthcheck degraded
- monitor does not restart

Actions:

1. Do not loop manual restarts
2. Reduce load or wait for provider window reset
3. Inspect recent request volume and concurrency

### 4. Schema or tool-output breakage

Symptoms:

- `error_code=schema_validation_failed`
- tool call rejected with structured validation error

Actions:

1. Inspect the tool schema and emitted arguments
2. Check recent prompt or tool contract changes
3. Fix schema or caller output
4. Restart gateway after deploying the fix

### 5. Provider/client configuration failure

Symptoms:

- `error_code=provider_4xx_non_retryable`
- healthcheck exits `2`
- monitor refuses restart

Actions:

1. Check API key / base URL / model name / provider-side request rules
2. Re-deploy corrected config
3. Start gateway again and confirm fingerprint

### 6. Context too large

Symptoms:

- `error_code=payload_too_large`
- `error_code=context_length_exceeded`

Actions:

1. Reduce prompt/context size
2. Verify compression / summarization paths are active
3. Re-run after deploy

## Deployment Checklist

Before deploy:

1. `scripts/gateway-stop`
2. deploy code/config
3. `scripts/gateway-preflight`
4. `scripts/gateway-up`
5. `scripts/gateway-status`
6. verify:
   - new `git_sha`
   - new `config_hash` if config changed
   - expected `prompt_version`
   - expected `model_name`
7. `scripts/gateway-healthcheck`

After deploy:

1. confirm healthcheck exit code is `0`
2. confirm monitor remains `noop` under healthy conditions
3. tail logs for the first live requests

## Rollback Procedure

Use rollback when a new deploy introduces persistent hard failures or behavior regressions.

1. `scripts/gateway-stop`
2. restore previous code/config revision
3. `scripts/gateway-preflight`
4. `scripts/gateway-up`
5. run:
   - `scripts/gateway-status`
   - `scripts/gateway-healthcheck`
6. confirm runtime fingerprint matches the intended rollback target

If the rollback target still shows the new `git_sha` or `config_hash`, treat that as a stale deploy/state issue and re-check startup repair plus effective config paths.

## Debug Priorities

Use this order:

1. `scripts/gateway-status`
2. `scripts/gateway-healthcheck`
3. `scripts/gateway-preflight`
4. `scripts/gateway-logs`
5. `tail -f /home/testai/zeus/data/logs/hermes_gateway_monitor.log`

Avoid starting with raw log digging if `status` already tells you the error code and last monitor action.

## Operational Rules

- Use only the unified gateway entrypoints
- Do not background `run.py` manually
- Do not keep parallel startup wrappers alive
- Do not restart repeatedly on `rate_limited`
- Treat `schema_validation_failed` as a code/schema defect, not an infrastructure blip
- Always verify runtime fingerprint after deploy or rollback
