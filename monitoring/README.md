# Monitoring — Midnight FNO Preprod

Prometheus + Grafana + Alertmanager, deployed via Docker Compose.

Cardano node and Midnight node both expose native Prometheus endpoints so no custom exporters are needed for the core blockchain signals. Grafana gives you a health overview without SSH-ing in on every check. Alertmanager handles deduplication and routing so you don't get flooded during a restart.

Looked at Datadog briefly — good if you're already running agents across a fleet, but adds cost and an external dependency. A self-hosted stack is simpler to audit for a single-node preprod setup.

---

## What's being scraped

| Service | Endpoint | What it gives you |
|---|---|---|
| `cardano-node` | `:12798/metrics` | Block height, peer count, mempool, memory/GC |
| `cardano-db-sync` | `:8080/metrics` | Sync progress, DB insert rate, lag behind tip |
| `midnight-node` | `:9615/metrics` | Substrate block height, peer count, finality |
| `node_exporter` | `:9100/metrics` | CPU, memory, disk |

> Cardano Prometheus must be enabled in `config.json`: `"hasPrometheus": ["127.0.0.1", 12798]`. Already set in the preprod config from IOG.

---

## Implemented alerts

Six alerts across Cardano and Midnight. Three were originally required; the extra three cover Midnight-specific signals that matter just as much for an FNO.

### `CardanoChainStall` · critical

```yaml
expr: increase(cardano_node_metrics_blockNum_int[5m]) < 1
for: 5m
```

Block progression is the single most important signal. If blocks stopped advancing, nothing else being healthy matters. The 5-minute expression window combined with `for: 5m` gives a worst-case detection time of ~10 minutes — fast enough to matter in production without false-alerting on brief hiccups or planned restarts (which typically complete within 2-3 minutes).

**Response:** check peer count (usually the cause) → journalctl → restart if no obvious reason.

---

### `CardanoLowPeerCount` · warning

```yaml
expr: cardano_node_metrics_connectedPeers_int < 3
for: 5m
```

Leading indicator — fires before a chain stall happens, giving a window to fix connectivity. Below 3 peers the node is technically still following the chain but fragile.

**Response:** check port 3001 → review topology config → add more reliable relays.

---

### `ServiceDown` · critical

```yaml
expr: up{job=~"cardano-node|cardano-db-sync|midnight-node"} == 0
for: 2m
```

Covers all three services with one rule. `up == 0` fires regardless of why the process exited — OOM, segfault, config error, or manual stop. 2-minute window avoids noise from planned restarts.

**Response:** systemctl status → journalctl → restart if transient. If OOM, investigate before restarting.

---

### `MidnightChainStall` · critical

```yaml
expr: increase(substrate_block_height{status="best",job="midnight-node"}[5m]) < 1
for: 5m
```

Same logic as CardanoChainStall but for the FNO's own chain. Stalled Midnight block height with Cardano healthy usually means a peer ban or bootstrap issue (see Issue 13/14 in the runbook).

> **Metric name assumption:** `substrate_block_height` is the standard metric exported by Substrate-based nodes at the `:9615/metrics` endpoint. This is the expected format based on the Substrate framework that Midnight is built on. The midnight-node was confirmed running and exporting metrics at port 9615, but the node did not advance past `best: #0` during setup (pre-whitelisting state), so the exact metric names from a live Midnight node were not verified against a fully synced instance. If the metric name differs in a future release, update the `expr` accordingly.

---

### `MidnightFinalityLag` · warning

```yaml
expr: substrate_block_height{status="best"} - substrate_block_height{status="finalized"} > 10
for: 5m
```

Best block advancing but finalized stalling = peers aren't reaching consensus. Catching this early prevents it from becoming a full stall.

---

### `MidnightLowPeerCount` · warning

```yaml
expr: substrate_sub_libp2p_peers_count{job="midnight-node"} < 2
for: 5m
```

Fewer than 2 peers on midnight-node usually means the PeerID got soft-banned. If this fires after a restart, rotate the network key (see Issue 13 in runbook).

---

## Alert routing

Alertmanager currently routes everything to a no-op receiver — alerts are evaluated and deduplicated but not forwarded anywhere. That's fine for preprod where you're watching the dashboard. For production, route by severity:

| Severity | Channel | Why |
|---|---|---|
| `critical` | PagerDuty | Chain stall or service crash needs a human now, any time of day |
| `warning` | Slack `#fno-alerts` | Needs attention but can wait for business hours |
| `info` | Grafana annotation only | Visible on the dashboard, no interrupt |

The alertmanager config has commented-out Slack and PagerDuty receiver examples — swap in your webhook URLs and update the routes.

`inhibit_rules` suppress the chain-stall alert when `ServiceDown` is already firing for the same service — they're the same incident, no need to page twice.

---

## What else to add in production

The alerts above cover "is it working right now." For a production FNO you'd also want:

**Midnight — once whitelisted:**
- **Missed block production** — if your node was scheduled to author a block in its slot and didn't, that's an SLA breach. Requires comparing on-chain block authors to your node's AURA key.
- **Epoch transition** — alert when a new epoch starts so you can confirm your node is still in the validator set.

**Infrastructure:**
- **Disk space < 20% free** — especially on the D: partition where the chain DB lives. A full disk kills the node without warning.
- **Memory > 90% for > 10 minutes** — Cardano + db-sync + PostgreSQL under catch-up load can push past 12 GB. Threshold left out of the current rules because it's host-specific; calibrate against a week of baseline data before setting it.
- **CPU — intentionally not alerted on.** Cardano node has aggressive GC cycles that regularly spike CPU to 80-100% during block validation and chain catch-up. A fixed CPU threshold would page constantly for completely normal behaviour. If the node is struggling, it shows up in the chain stall alert first — that's the signal worth acting on.

**db-sync:**
- **Lag > 10 blocks behind Cardano tip** (post-initial-sync) — db-sync falling behind means midnight-node is querying stale chain data. Use `cardano_db_sync_node_blocks` vs `cardano_node_metrics_blockNum_int`.
- **PostgreSQL unreachable** (`pg_up == 0`) — midnight-node silently degrades if postgres goes away; this catches it before it surfaces as a chain stall.

**Latency / P95–P99:**
- **RPC response time p95 > 500ms** — `substrate_rpc_calls_time_bucket` in midnight-node's metrics. High RPC latency is usually the first sign of memory pressure or a blocked thread, well before it causes a visible problem.
- **DB query latency p99 > 2s** — via PostgreSQL `pg_stat_statements`. db-sync query patterns degrade as the DB grows; catching it early prevents cascade failures.
- **Block validation time spikes** — Substrate exposes block processing time histograms. p99 spikes above baseline are an early warning of runtime issues before they become stalls.

---

## Running the stack

Prerequisites: Docker Desktop with WSL2 integration enabled.

```bash
cd monitoring
docker compose up -d

# Grafana:      http://localhost:3000  (admin / admin — change on first login)
# Prometheus:   http://localhost:9090
# Alertmanager: http://localhost:9093
```

> **WSL note:** Prometheus scrapes `host.docker.internal` to reach services running in WSL. On native Linux replace with `172.17.0.1` or the actual host IP.

---

## Directory structure

```
monitoring/
├── README.md
├── docker-compose.yml               ← Prometheus + Grafana + Alertmanager + node-exporter
├── prometheus/
│   ├── prometheus.yml               ← scrape config (4 targets)
│   └── alerts.yml                   ← 6 alert rules
├── alertmanager/
│   └── alertmanager.yml             ← routing + inhibit rules (no-op receiver for preprod)
└── grafana/
    ├── provisioning/
    │   ├── datasources/prometheus.yml
    │   └── dashboards/dashboard.yml
    └── dashboards/midnight-fno.json
```

