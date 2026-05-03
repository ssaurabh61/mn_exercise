# Scripts — Midnight FNO Preprod

## Section 3 Choice: Option C — Node Health Checker

**Why Option C over A and B:**

| Option | What it does | Why not chosen |
|---|---|---|
| A — Key collection | Sends requests to FNO operators for public keys | Requires mocking an external coordination layer; the interesting part is simulated, not real |
| B — Maintenance notification | Generates maintenance window notifications | More useful as a process/template than a script; the ack tracking is largely mock logic |
| C — Node health checker | Polls live metrics endpoints, writes structured report, diffs regressions | **Works against the actual running node.** All signals are real, not mocked. Practical operational utility — you'd run this in cron or alongside Grafana. |

Option C is the strongest demonstration because every health check runs against the actual preprod stack we've built, not simulated data.

---

## Script: `node_health_check.py`

### What it does

1. **Polls** Prometheus metrics endpoints for each running service:
   - `cardano-node` → `http://localhost:12798/metrics`
   - `cardano-db-sync` → `http://localhost:8080/metrics`
   - `midnight-node` → `http://localhost:9615/metrics`
   - System resources via `node_exporter` → `http://localhost:9100/metrics`

2. **Evaluates** a set of health conditions against configurable thresholds:
   - Block height has advanced since last check (chain is moving)
   - Peer count is above minimum threshold (default: 3)
   - DB sync lag is below a threshold (default: 300 slots)
   - All services are reachable (HTTP 200)
   - Disk usage on `/mnt/d` is below threshold (default: 90%)

3. **Writes** a timestamped JSON health report to a configurable output directory

4. **Diffs** against the previous report and prints any regressions (conditions that were healthy and are now degraded)

5. **Exits non-zero** if any critical condition fails — makes it cron/CI friendly

### Output format

```json
{
  "timestamp": "2026-05-02T14:32:00Z",
  "overall": "healthy",
  "services": {
    "cardano-node": {
      "reachable": true,
      "block_height": 12345678,
      "peer_count": 12,
      "conditions": {
        "block_advancing": { "status": "ok", "value": 12345678 },
        "peers_sufficient": { "status": "ok", "value": 12 }
      }
    },
    "cardano-db-sync": {
      "reachable": true,
      "sync_lag_slots": 42,
      "conditions": {
        "sync_lag_ok": { "status": "ok", "value": 42 }
      }
    },
    "midnight-node": {
      "reachable": true,
      "block_height": 87654,
      "conditions": {
        "block_advancing": { "status": "ok", "value": 87654 }
      }
    }
  },
  "system": {
    "disk_pct_mnt_d": 61.2,
    "conditions": {
      "disk_ok": { "status": "ok", "value": 61.2 }
    }
  },
  "regressions": []
}
```

### Regression diff example

```
[REGRESSION] cardano-node.peers_sufficient: was ok (12 peers) → now degraded (2 peers)
[REGRESSION] midnight-node.block_advancing: was ok → now degraded (height unchanged for 2 checks)
```

---

## Usage

```bash
# Basic run (defaults: output to ./reports/, thresholds from defaults)
python3 scripts/node_health_check.py

# Custom output directory and peer threshold
python3 scripts/node_health_check.py --output-dir /var/log/fno-health --min-peers 5

# Continuous mode (poll every 60s)
python3 scripts/node_health_check.py --interval 60

# Cron example (every 5 minutes, log to file)
*/5 * * * * /usr/bin/python3 /home/knight/mn_exercise/scripts/node_health_check.py \
    --output-dir /var/log/fno-health >> /var/log/fno-health/cron.log 2>&1
```

### Exit codes

| Code | Meaning |
|---|---|
| `0` | All conditions healthy |
| `1` | One or more conditions degraded |
| `2` | One or more services unreachable |

---

## TODO (Implementation)

- [ ] Write `node_health_check.py` (Python 3, stdlib only — no external deps needed)
- [ ] Implement metrics parser for Prometheus text format
- [ ] Implement health condition evaluators
- [ ] Implement JSON report writer with timestamp-named files
- [ ] Implement diff logic against previous report
- [ ] Implement `--interval` continuous mode
- [ ] Add `--config` flag to load thresholds from a YAML file
- [ ] Test against live preprod node once sync completes
