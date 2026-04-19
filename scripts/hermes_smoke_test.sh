#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

echo "[1/4] gateway preflight"
"${PYTHON:-python3}" ./scripts/gateway-preflight

echo "[2/4] smoke tests"
"${PYTHON:-python3}" -m pytest \
  tests/smoke/test_gateway_ops_scripts.py \
  tests/smoke/test_gateway_healthcheck.py \
  tests/smoke/test_gateway_monitor.py \
  tests/smoke/test_gateway_preflight.py \
  --override-ini='addopts='

echo "[3/4] regression stability contracts"
"${PYTHON:-python3}" -m pytest \
  tests/regression/test_stability_v1_contracts.py \
  --override-ini='addopts='

echo "[4/4] integration state machine"
"${PYTHON:-python3}" -m pytest \
  tests/integration/test_task_state_machine.py \
  --override-ini='addopts='

echo "hermes stability v1 smoke test passed"
