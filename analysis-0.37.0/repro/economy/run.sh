#!/bin/sh
# Run one economy reproduction in a fresh data folder. From ember/:
#   sh ../analysis-0.37.0/repro/economy/run.sh ../analysis-0.37.0/repro/economy/e13_hold_clamp_5x.py
D=$(mktemp -d)
PYTHONPATH=. EMBER_DATA_DIR=$D EMBER_SCHEDULER=off EMBER_FAKE_DELAY_MS=0 python "$@"
