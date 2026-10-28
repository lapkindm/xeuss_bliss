#!/bin/bash
# Copy the live BLISS configuration (~/bliss/beamline_configuration) into bliss_config/ before a commit.
# Left out: beacon.rdb (Beacon's Redis dump). The responsible person's e-mail address and the mail domain
# (host name) are replaced by placeholders.
set -euo pipefail
cd "$(dirname "$0")"
rsync -a --delete --exclude beacon.rdb --exclude __pycache__ ~/bliss/beamline_configuration/ bliss_config/
sed -i -E -e 's/^(xeuss_bliss\.notify\.ERRORS_ALWAYS_TO = ).*/\1"responsible@example.org"  # set on xeuss20/' \
    -e 's/^(xeuss_bliss\.notify\.MAIL_DOMAIN = ).*/\1"host.example.org"  # set on xeuss20/' \
    bliss_config/sessions/xeuss_setup.py
git status --short bliss_config
