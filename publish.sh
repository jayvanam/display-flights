#!/usr/bin/env bash
# Regenerate index.html from the scraper's database and push it to GitHub Pages.
#
# Runs on the Pi from cron, after the flights sweep. Nothing here reaches into the
# Pi from outside: the Pi pushes out, so there is no inbound port, no tunnel and no
# VPN, and the page keeps serving the last snapshot even when the Pi is down.
#
#   crontab -e
#   20 */6 * * * /home/jay/Documents/display-flights/publish.sh >> /home/jay/Documents/display-flights/publish.log 2>&1
#
# The :20 offset gives the 0 */6 sweep time to write its alerts first.
#
# Requires push access to this repo from the Pi. Either an SSH remote with a
# deploy key, or a PAT in the https remote — check with `git -C <repo> push`.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLIGHTS="${FLIGHTS_REPO:-$(dirname "$REPO")/flights}"
PYTHON="${PYTHON:-$FLIGHTS/venv/bin/python}"
DB="${FARES_DB:-$FLIGHTS/data/fares.db}"
HOURS="${HOURS:-24}"

[ -x "$PYTHON" ] || { echo "no interpreter at $PYTHON" >&2; exit 1; }
[ -f "$DB" ] || { echo "no database at $DB" >&2; exit 1; }

cd "$REPO"

# Pull first: a hand-edit pushed from the laptop would otherwise make this diverge
# and every subsequent run would fail on a non-fast-forward.
git pull --quiet --rebase --autostash origin main || {
  echo "pull failed — resolve by hand in $REPO" >&2; exit 1; }

"$PYTHON" generate.py --db "$DB" --hours "$HOURS" --out "$REPO/index.html"

if git diff --quiet -- index.html; then
  echo "$(date -Is)  no change"
  exit 0
fi

git add index.html
git -c user.name="pi-publisher" \
    -c user.email="pi@localhost" \
    commit --quiet -m "fares: $(date -u '+%Y-%m-%d %H:%M') UTC snapshot"
git push --quiet origin main
echo "$(date -Is)  published"
