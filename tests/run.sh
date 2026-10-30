#!/bin/sh
# Runs every hook against the fixtures. Needs only python3 (3.8 or later); no packages.
cd "$(dirname "$0")/.." && exec python3 -I tests/test_hooks.py "$@"
