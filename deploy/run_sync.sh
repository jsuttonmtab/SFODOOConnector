#!/bin/bash
# Odoo <-> Salesforce sync, every 5 minutes (docs/operations.md)
export PATH=/usr/local/bin:/usr/bin:/bin
REPO=/opt/sfsync/SFODOOConnector
LOGDIR=/opt/sfsync/logs
LOCK=/opt/sfsync/sync.lock
mkdir -p "$LOGDIR"
LOG="$LOGDIR/sync-$(date +%Y%m%d).log"

exec 9>>"$LOCK"
if ! flock -n 9; then
    echo "$(date '+%F %T') skipped: a run or a manual sync holds the lock" >> "$LOG"
    exit 0
fi

cd "$REPO" || exit 1
{
    echo "=== $(date '+%F %T') run start $(git rev-parse --short HEAD)"
    python3 -m connector.sync_accounts; rc1=$?
    echo "=== accounts exit $rc1"
    python3 -m connector.sync_orders; rc2=$?
    echo "=== orders exit $rc2 at $(date '+%F %T')"
} >> "$LOG" 2>&1

find "$LOGDIR" -name 'sync-*.log' -mtime +14 -delete
exit $rc2
