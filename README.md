# Midnight FNO Node — DevOps Assessment

## What I Built

A complete Midnight Founding Node Operator (FNO) setup running on Windows via WSL 2, targeting the Midnight preprod network.

### Section 1 — Node Setup (`notes/RUNBOOK.md`)
Full end-to-end runbook for onboarding as an FNO on preprod. Covers WSL 2 setup on D: drive, Cardano node, PostgreSQL 17, cardano-db-sync, and Midnight node installation and key generation. Written as a hand-off document for an engineer who knows DevOps but not Midnight specifically. Includes 12 documented gotchas hit during the actual live setup.

**Stack:** Ubuntu 24.04 (WSL 2) · Cardano node 10.6.2 · cardano-db-sync 13.6.0.7 · PostgreSQL 17 · Midnight node 0.22.2

**Status at submission:** Cardano node live-following chain tip (block ~4,671,500). db-sync fully synced (99.9999%, verified — see `notes/db-sync-verification.md`). Midnight node installed, all validator keys generated, keystore populated, `partner-chains-public-keys.json` produced. Node connects to preprod bootnodes (1–3 peers) and downloads chain data.

**Known issue:** Block import stalls at `best: #0` due to a bootstrap sequencing requirement. midnight-node maintains its own internal Cardano state cache built from processed Midnight blocks — since a fresh node has no Midnight blocks yet, it cannot verify peer block announcements that reference recent Cardano state. The referenced Cardano block (`aee88622...`, block 4,526,090) is confirmed present in db-sync; the issue is midnight-node's own empty internal cache. **Fix:** A chain snapshot from Midnight Foundation, provided as part of the official FNO whitelisting process after `partner-chains-public-keys.json` is submitted. All infrastructure is correctly configured. Documented as G17 in the runbook.

### Section 2 — Monitoring (`monitoring/`)
Prometheus + Grafana + Alertmanager stack deployed via Docker Compose. Three alerts chosen for signal-over-noise: chain stall, low peer count, and service crash. Full config files included.

### Section 3 — Automation (`scripts/`)
Two scripts — one per assessment option:

**Option A — `scripts/key_collection/fno_key_collection.py`** — Given a list of FNO operator identifiers (`operators.json`), fetches each operator's public key from their configured endpoint, writes a timestamped JSON + CSV report, and persists state so re-runs skip operators who already responded. Idempotent by design; `--retry` forces a full re-collection.

```
python3 scripts/key_collection/fno_key_collection.py
python3 scripts/key_collection/fno_key_collection.py --retry           # re-request all operators
python3 scripts/key_collection/fno_key_collection.py --timeout 30      # slower network
```

**Option C — `scripts/node_health/node_health_check.py`** — Polls Prometheus endpoints for all three FNO services, evaluates health conditions, writes timestamped JSON reports, and diffs regressions against the previous run. Exit codes make it cron/CI-friendly.

```
python3 scripts/node_health/node_health_check.py
python3 scripts/node_health/node_health_check.py --report-dir /var/log/fno-health
```

Both scripts use Python 3 stdlib only — no pip dependencies.

### Section 4 — Security (`notes/SECURITY.md`)
Key storage (KMS + Vault), rotation procedure, and incident response answers.

---

## Assumptions

- **WSL 2 as the deployment target** — the homework doesn't specify an OS. Running on Linux bare-metal or in a VM would skip Phases 0–1 entirely and avoid the WSL-specific gotchas (NTFS socket limitation, virtual disk sizing).
- **Cardano node as relay, not block producer** — FNO validators don't need to mint Cardano blocks. A relay is simpler, safer (no signing keys on machine), and sufficient for db-sync to read from.
- **Pinned cardano-node 10.6.2** — not upgraded to 10.7.1 despite it being newer. cardano-db-sync 13.6.0.7 was built and tested against 10.6.x libraries; upgrading without a matching db-sync release risks data corruption.
- **db-sync synced from scratch** — IOG publishes pg_restore snapshots that would have cut the 6–8 hour sync to minutes, but the Midnight FNO docs don't reference them and preprod snapshot availability is inconsistent. Documented in the runbook as an optimisation for future runs.
- **Midnight node 0.22.2** — as specified in the official Midnight Foundation Notion doc. An initial attempt with v0.22.5 caused version mismatch errors; reverted to 0.22.2 as directed.

---

## What I'd Do Differently With More Time

- **Use the IOG db-sync pg_restore snapshot** to skip the multi-hour cold sync. This is standard SRE practice for provisioning new nodes — syncing from epoch 0 is for development/testing only.
- **Run on Linux natively or in a proper VM** rather than WSL. WSL is convenient for development but has real operational constraints (NTFS socket limitation, WSL shutdown killing services, virtual disk management). A production FNO would run on a dedicated Linux host or cloud VM.
- **Implement the full monitoring stack end-to-end** including a live Grafana dashboard with real block progression panels, not just config files. With a fully synced node the dashboard panels would show real data.
- **Automate the full setup with Ansible or a shell installer** — the runbook is reproducible but still manual. An idempotent Ansible playbook would make repeatable deployments and version upgrades significantly faster.
- **Set up proper secret management** — the runbook uses a placeholder password in pgpass. In production this would be injected at deploy time via Vault agent or AWS SSM Parameter Store, never stored in any file.
- **Add a process supervisor health check** — systemd `Restart=always` handles crashes but doesn't detect a running-but-stuck process (e.g. node connected but not advancing). A watchdog script or liveness probe would catch this.
