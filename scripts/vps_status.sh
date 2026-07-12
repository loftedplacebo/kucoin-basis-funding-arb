#!/usr/bin/env bash
set -euo pipefail

systemctl --no-pager --full status \
  kucoin-basis-scanner \
  kucoin-basis-strategy \
  kucoin-basis-dashboard \
  kucoin-basis-convergence-scanner \
  kucoin-basis-convergence-strategy

echo
echo "Listening sockets:"
ss -ltnp | grep -E '(:8766|State)' || true

echo
echo "Recent scanner runs:"
tail -n 10 data/kucoin_basis/scanner_runs.csv 2>/dev/null || true

echo
echo "Recent convergence scanner runs:"
tail -n 10 data/kucoin_basis_convergence/scanner_runs.csv 2>/dev/null || true

echo
echo "Recent service logs:"
journalctl \
  -u kucoin-basis-scanner \
  -u kucoin-basis-strategy \
  -u kucoin-basis-dashboard \
  -u kucoin-basis-convergence-scanner \
  -u kucoin-basis-convergence-strategy \
  -n 120 --no-pager
