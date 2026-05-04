# Scripts — Midnight FNO Preprod

Both Option A and Option C from the assessment were implemented.

| Script | Option | Location |
|---|---|---|
| `fno_key_collection.py` | A — Key collection | `scripts/key_collection/` |
| `node_health_check.py` | C — Node health checker | `scripts/node_health/` |

---

## Script: `fno_key_collection.py`

### What it does

1. **Reads** a list of FNO operators from `operators.json` (id, name, key endpoint)
2. **Requests** each operator's public key via HTTP GET to their configured endpoint — expects a JSON response with a `public_key` field
3. **Persists state** to `reports/key_collection_state.json` so re-runs skip operators who already responded (idempotent by design)
4. **Writes** a timestamped JSON + CSV report to the output directory
5. **Prints** a summary of who responded and who didn't, with error details for pending operators
6. **Exits non-zero** if any operators are still pending — makes it CI/cron friendly

Mock key endpoints are provided in `key_collection/mock_keys/` (served as static JSON files) for local testing without live operator infrastructure.

### Output format

```json
{
  "generated_at": "2026-05-02T14:32:00Z",
  "total": 3,
  "collected": 2,
  "pending": 1,
  "results": [
    {
      "id": "fno-alpha",
      "name": "Alpha Operator",
      "public_key": "0xabc123...",
      "collected_at": "2026-05-02T14:32:00Z",
      "status": "collected"
    },
    {
      "id": "fno-bravo",
      "name": "Bravo Operator",
      "public_key": "",
      "error": "HTTP 503",
      "collected_at": "2026-05-02T14:32:01Z",
      "status": "pending"
    }
  ]
}
```

A matching CSV is written alongside the JSON for easy import into spreadsheets or other tooling.

### Usage

```bash
# Basic run — reads operators.json, writes reports/ directory
python3 scripts/key_collection/fno_key_collection.py

# Preview what would run without making any HTTP requests
python3 scripts/key_collection/fno_key_collection.py --dry-run

# Re-request keys from all operators, including those already collected
python3 scripts/key_collection/fno_key_collection.py --retry

# Re-request a single operator only (useful after one failure)
python3 scripts/key_collection/fno_key_collection.py --operator fno-delta --retry

# Target multiple specific operators
python3 scripts/key_collection/fno_key_collection.py --operator fno-delta --operator fno-echo

# Slower network: increase timeout and allow more retry attempts
python3 scripts/key_collection/fno_key_collection.py --timeout 30 --attempts 5

# Custom output directory
python3 scripts/key_collection/fno_key_collection.py --output-dir /var/log/fno-keys
```

### Exit codes

| Code | Meaning |
|---|---|
| `0` | All operators responded |
| `1` | One or more operators still pending |
| `2` | Fatal error (missing file, bad config, invalid URL) |

### Flags

| Flag | Default | Description |
|---|---|---|
| `--operators FILE` | `key_collection/operators.json` | Operator list |
| `--state FILE` | `reports/key_collection_state.json` | Persistent state file |
| `--output-dir DIR` | `reports/` | Where to write JSON + CSV reports |
| `--timeout SECS` | `10` | Per-attempt HTTP connection timeout |
| `--attempts N` | `3` | Max retry attempts per operator on transient failures (timeouts, 5xx). Exponential backoff: 1s, 2s, 4s between tries. 4xx and malformed responses are not retried. |
| `--retry` | off | Re-request even already-collected keys |
| `--operator ID` | all | Only collect from this ID (repeatable) |
| `--dry-run` | off | Preview without making any HTTP requests |

---

## Section 3 Choice: Option C — Node Health Checker

**Why Option C over B:**

| Option | What it does | Why not chosen |
|---|---|---|
| B — Maintenance notification | Generates maintenance window notifications | More useful as a process/template than a script; the ack tracking is largely mock logic |
| C — Node health checker | Polls live metrics endpoints, writes structured report, diffs regressions | **Works against the actual running node.** All signals are real, not mocked. Practical operational utility — you'd run this in cron or alongside Grafana. |

Option C is the strongest demonstration because every health check runs against the actual preprod stack we've built, not simulated data.

---

## Script: `node_health_check.py`

### Configuration

Service definitions live in `services.json` alongside the script — separate from the code so you can add, remove, or reconfigure services without touching Python. Each entry specifies a URL, a `scrape_type`, the health checks to run, and which metrics to snapshot for stall detection.

Two scrape types are supported:

| `scrape_type` | Protocol | Used by |
|---|---|---|
| `prometheus` | HTTP GET `/metrics`, Prometheus text format | cardano-node, cardano-db-sync, host |
| `substrate_rpc` | HTTP POST JSON-RPC 2.0 (`system_health`, `chain_getHeader`, etc.) | midnight-node |

`midnight-node` uses `substrate_rpc` because the Substrate JSON-RPC port (9944) is open by default on any running node, whereas the Prometheus metrics port (9615) only exists if the node is explicitly started with `--prometheus-port`.

### What it does

1. **Scrapes** each service using its configured protocol:
   - `cardano-node` → `http://127.0.0.1:12798/metrics` (Prometheus)
   - `cardano-db-sync` → `http://127.0.0.1:8080/metrics` (Prometheus)
   - `midnight-node` → `http://127.0.0.1:9944` (Substrate JSON-RPC)
   - System resources via `node_exporter` → `http://127.0.0.1:9100/metrics` (Prometheus)

2. **Evaluates** health checks per service:

   | Service | Check | Condition |
   |---|---|---|
   | cardano-node | peers_sufficient | connected peers >= 3 |
   | cardano-node | block_height_nonzero | block height > 0 |
   | midnight-node | peers_sufficient | connected peers >= 2 |
   | midnight-node | best_block_nonzero | best block height > 0 |
   | midnight-node | finality_lag_ok | best - finalized <= 10 blocks |
   | all | service_up | endpoint reachable |

3. **Writes** a timestamped JSON report to the report directory

4. **Diffs** against the previous report and surfaces:
   - `health_degraded` — service was healthy last run, is unhealthy now
   - `metric_stalled` — block height unchanged between consecutive runs (stalled chain)

5. **Exits non-zero** if any service is unhealthy — cron/CI friendly

### Output format

```json
{
  "timestamp": "2026-05-02T14:32:00+00:00",
  "overall_healthy": true,
  "services": [
    {
      "service": "cardano-node",
      "healthy": true,
      "checks": [
        { "name": "service_up", "description": "Prometheus endpoint is reachable", "passed": true, "detail": "endpoint reachable" },
        { "name": "peers_sufficient", "description": "connected peers >= 3", "passed": true, "detail": "12.0 >= 3 → pass" },
        { "name": "block_height_nonzero", "description": "block height > 0 (node has synced at least one block)", "passed": true, "detail": "4671500.0 > 0 → pass" }
      ],
      "metric_snapshot": { "cardano_node_metrics_blockNum_int": 4671500.0 }
    }
  ],
  "regressions": []
}
```

### Regression diff example

```
REGRESSIONS since last run:
  [cardano-node] was healthy, now unhealthy
    failed checks: peers_sufficient
  [midnight-node] substrate_block_height{status="best"} unchanged between runs (value: 87654.0)
```

---

## Usage

```bash
# Single run
python3 scripts/node_health/node_health_check.py

# Custom report directory
python3 scripts/node_health/node_health_check.py --report-dir /var/log/fno-health

# Continuous mode — poll every 60 seconds
python3 scripts/node_health/node_health_check.py --interval 60

# Cron — silent when healthy, logs only on failure (every 5 minutes)
*/5 * * * * python3 /path/to/scripts/node_health/node_health_check.py \
    --report-dir /var/log/fno-health --quiet >> /var/log/fno-health/cron.log 2>&1
```

### Flags

| Flag | Default | Description |
|---|---|---|
| `--report-dir DIR` | `../reports/` | Where to write timestamped JSON reports |
| `--timeout SECS` | `5` | Per-scrape HTTP timeout |
| `--interval SECS` | single run | Poll continuously; Ctrl-C to stop |
| `--quiet` | off | Suppress output when all healthy (for cron) |

### Exit codes

| Code | Meaning |
|---|---|
| `0` | All services healthy |
| `1` | One or more services unhealthy or unreachable |
| `2` | Bad arguments |

