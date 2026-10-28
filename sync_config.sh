#!/bin/bash
# Copy the live BLISS configuration (~/bliss/beamline_configuration) into bliss_config/ before a commit.
# Left out: beacon.rdb (Beacon's Redis dump). The responsible person's e-mail address is replaced by a placeholder.
set -euo pipefail
cd "$(dirname "$0")"
rsync -a --delete --exclude beacon.rdb --exclude __pycache__ ~/bliss/beamline_configuration/ bliss_config/
sed -i -E 's/^(xeuss_bliss\.notify\.ERRORS_ALWAYS_TO = ).*/\1"responsible@example.org"  # set on xeuss20/' \
    bliss_config/sessions/xeuss_setup.py
git status --short bliss_config
