#!/usr/bin/env bash
set -euo pipefail

echo "DRY-RUN: listing items that WOULD be removed by the cleanup (non-destructive)"
echo

check_and_print() {
  local path="$1"
  if compgen -G "$path" > /dev/null; then
    for p in $path; do
      if [ -e "$p" ]; then
        echo "FOUND: $p"
        if [ -d "$p" ]; then
          du -sh "$p" 2>/dev/null || true
        else
          ls -l "$p" 2>/dev/null || true
        fi
      else
        echo "MISSING: $p"
      fi
    done
  else
    echo "MISSING GLOB: $path"
  fi
  echo
}

echo "-- Systemd unit files --"
check_and_print "/etc/systemd/system/cardano-db-sync.service"
check_and_print "/etc/systemd/system/cardano-node.service"
check_and_print "/etc/systemd/system/cardano-db-sync.service.d"

echo "-- fstab bind entry --"
if sudo grep -n "/mnt/d/cardano/node /var/lib/cardano/node" /etc/fstab >/dev/null 2>&1; then
  echo "Found fstab bind entry for /mnt/d/cardano/node -> /var/lib/cardano/node"
else
  echo "No fstab bind entry found for /mnt/d/cardano/node"
fi
echo

echo "-- Binaries and configs --"
check_and_print "/usr/local/bin/cardano-db-sync"
check_and_print "/usr/local/bin/cardano-node"
check_and_print "/usr/local/bin/cardano-cli"
check_and_print "/etc/cardano"

echo "-- Cardano data and logs --"
check_and_print "/var/lib/cardano"
check_and_print "/var/log/cardano"

echo "-- Temporary extracted releases --"
check_and_print "/tmp/cardano-db-sync-*"
check_and_print "/home/saurabh/tmp/cardano-db-sync-*"

echo "-- PostgreSQL (non-destructive checks) --"
if command -v psql >/dev/null 2>&1; then
  echo "Postgres client found; listing DBs and roles mentioning 'cardano' (non-destructive):"
  sudo -u postgres psql -Atc "SELECT datname FROM pg_database WHERE datname='cardano'" | sed -n '1,200p' || true
  sudo -u postgres psql -Atc "SELECT rolname FROM pg_roles WHERE rolname='cardano'" | sed -n '1,200p' || true
else
  echo "psql not found; skipping Postgres checks"
fi
echo

echo "-- Systemd enablement status (non-destructive) --"
if systemctl list-unit-files | grep -q cardano-node; then
  systemctl is-enabled cardano-node 2>/dev/null || echo "cardano-node not enabled"
else
  echo "cardano-node unit not present"
fi
if systemctl list-unit-files | grep -q cardano-db-sync; then
  systemctl is-enabled cardano-db-sync 2>/dev/null || echo "cardano-db-sync not enabled"
else
  echo "cardano-db-sync unit not present"
fi
echo

echo "DRY-RUN complete. To perform removal, review the Cleanup section in notes/RUNBOOK.md and run the destructive commands there."
