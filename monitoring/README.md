# Monitoring & Alerting — Midnight FNO Preprod Node

## Stack Choice

**Prometheus + Grafana + Alertmanager**, deployed via Docker Compose.

Rationale:
- Industry standard for blockchain node monitoring — Cardano node and Midnight node both expose native Prometheus endpoints, so no custom exporters are needed for the core signals
- Grafana gives visual confirmation of chain health without SSH access
- Alertmanager handles deduplication, grouping, and routing — avoids alert storms during restarts

Alternative considered: **Datadog** — excellent for multi-service environments with existing agents, but adds cost and an external dependency. For a self-hosted preprod validator, a local stack is more appropriate and easier to audit.

---

## Components Monitored

| Service | Metrics endpoint | What it exposes |
|---|---|---|
| `cardano-node` | `:12798/metrics` | Block height, peer count, mempool, memory/GC |
| `cardano-db-sync` | `:8080/metrics` | Sync progress, DB insert rate, lag behind tip |
| `midnight-node` | `:9615/metrics` | Substrate block height, peer count, finality |
| `node_exporter` | `:9100/metrics` | System CPU, memory, disk usage |

> **Note:** `cardano-node` Prometheus metrics must be enabled in `config.json` by setting `"hasPrometheus": ["127.0.0.1", 12798]`. This is already set in the preprod config from IOG.

---

## Alert Design

### Alert 1 — Chain Stall (`CardanoChainStall`)

```yaml
alert: CardanoChainStall
expr: increase(cardano_node_metrics_blockNum_int[10m]) == 0
for: 10m
labels:
  severity: critical
annotations:
  summary: "Cardano node has not added a block in 10 minutes"
  description: "Block height has been static for 10+ minutes. Node is not following the chain."
```

**Why:** Block progression is the single most critical health signal for a validator. A stalled node means it is neither observing the chain nor contributing to it. Any other metric being healthy while blocks are stalled is a false positive on node health.

**Operational response:**
1. Check peer count alert (below) — isolation is the most common cause
2. `sudo journalctl -u cardano-node -n 100` — look for consensus errors
3. If no obvious cause, restart: `sudo systemctl restart cardano-node`

---

### Alert 2 — Low Peer Count (`CardanoLowPeerCount`)

```yaml
alert: CardanoLowPeerCount
expr: cardano_node_metrics_connectedPeers_int < 3
for: 5m
labels:
  severity: warning
annotations:
  summary: "Cardano node has fewer than 3 connected peers"
  description: "Peer count is {{ $value }}. Node may be approaching network isolation."
```

**Why:** Cardano uses Ouroboros which requires network consensus. With fewer than 3 peers the node can still follow the chain but is at risk of diverging if those peers are unreliable. This is a leading indicator — it fires before a chain stall occurs, giving time to act.

**Operational response:**
1. Check ISP/VPN connectivity
2. Verify port 3001 is reachable: `curl -v telnet://$(hostname -I | awk '{print $1}'):3001`
3. Review topology config and add more reliable relays if persistently low

---

### Alert 3 — Service Down (`ServiceDown`)

```yaml
alert: ServiceDown
expr: up{job=~"cardano-node|cardano-db-sync|midnight-node"} == 0
for: 2m
labels:
  severity: critical
annotations:
  summary: "{{ $labels.job }} is not responding to Prometheus scrapes"
  description: "Service {{ $labels.job }} has been unreachable for 2+ minutes — process likely crashed."
```

**Why:** Prometheus `up == 0` is the most reliable crash signal — it fires regardless of *why* the process exited (OOM, segfault, config error, manual stop). Covering all three services with a single rule means no crash goes unnoticed. The 2-minute `for` window avoids false positives during planned restarts.

**Operational response:**
1. `sudo systemctl status <service>` — check exit code and last log lines
2. `sudo journalctl -u <service> -n 50` — identify crash reason
3. `sudo systemctl restart <service>` if transient; investigate before restarting if it was an OOM kill

---

## Alert Philosophy

These three alerts cover the three independent failure modes a validator can experience:

| Failure mode | Alert | Is it actionable? |
|---|---|---|
| Node not following chain | CardanoChainStall | Yes — restart / check consensus |
| Network isolation risk | CardanoLowPeerCount | Yes — fix connectivity / topology |
| Process crash | ServiceDown | Yes — restart / fix config |

Intentionally excluded (would be noisy/low-signal for preprod FNO):
- **CPU/memory thresholds** — Cardano node GC spikes are expected; threshold tuning is environment-specific and generates false positives. OOM will surface via ServiceDown.
- **DB sync lag** — lag is expected during initial sync and after restarts; alerting before 100% sync would be constant noise.
- **Disk usage** — valid concern for production, deferred here since chain data volume is known and stable on preprod.

---

## Directory Structure

```
monitoring/
├── README.md                        ← this file
├── docker-compose.yml               ← Prometheus + Grafana + Alertmanager stack
├── prometheus/
│   ├── prometheus.yml               ← scrape config
│   └── alerts.yml                   ← alert rules (the three above)
├── alertmanager/
│   └── alertmanager.yml             ← routing (logs to stdout for preprod)
└── grafana/
    ├── provisioning/
    │   ├── datasources/
    │   │   └── prometheus.yml       ← auto-wire Prometheus datasource
    │   └── dashboards/
    │       └── dashboard.yml        ← auto-load dashboard on startup
    └── dashboards/
        └── midnight-fno.json        ← dashboard definition
```

---

## Running the Stack

Prerequisites: Docker Desktop running (WSL2 integration enabled).

```bash
cd monitoring
docker compose up -d

# Grafana:    http://localhost:3000  (admin / admin)
# Prometheus: http://localhost:9090
# Alertmanager: http://localhost:9093
```

> **Note:** Prometheus scrapes `host.docker.internal` to reach services running in WSL. On Linux (non-WSL) replace with `172.17.0.1` or the host IP.

---

## TODO (Implementation)

- [ ] Enable prometheus endpoint in cardano-node `config.json` (`hasPrometheus`)
- [ ] Write `docker-compose.yml`
- [ ] Write `prometheus/prometheus.yml` with scrape targets
- [ ] Write `prometheus/alerts.yml` with the three rules above
- [ ] Write `alertmanager/alertmanager.yml` (log-to-stdout for preprod)
- [ ] Import/export Grafana dashboard JSON for cardano-node panel
- [ ] Verify midnight-node exposes `:9615` (Substrate default) or adjust scrape target
