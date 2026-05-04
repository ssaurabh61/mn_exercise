# Midnight FNO Node — DevOps Assessment

## What I Built

A complete Midnight Founding Node Operator (FNO) setup running on Windows via WSL 2, targeting the Midnight preprod network.

### Section 1 — Node Setup (`docs/RUNBOOK.md`)
Full end-to-end runbook for onboarding as an FNO on preprod. Covers WSL 2 setup on D: drive, Cardano node, PostgreSQL 17, cardano-db-sync, and Midnight node installation and key generation. Written as a hand-off document for an engineer who knows DevOps but not Midnight specifically. Includes 15 documented gotchas hit during the actual live setup.

**Stack:** Ubuntu 24.04 (WSL 2) · Cardano node 10.6.2 · cardano-db-sync 13.6.0.7 · PostgreSQL 17 · Midnight node 0.22.2

**Status at submission:** Cardano node live-following chain tip (block ~4,671,500). db-sync fully synced (99.9999%, verified — see `docs/evidence/db-sync-verification.md`). Midnight node installed, all validator keys generated, keystore populated, `partner-chains-public-keys.json` produced. Node connects to preprod bootnodes (1–3 peers) and downloads chain data.

**Known issue:** Block import stalls at `best: #0`. The node connects to peers and db-sync is fully synced, so networking and chain data are healthy. The error `Validator inherent data must be provided` indicates the node cannot construct validator-specific inherent data during block verification — in Midnight preprod this depends on the node being registered in the on-chain `permissioned_candidates` set, which requires completing the FNO whitelisting process. This is the expected pre-whitelisting state. **Next step:** submit `partner-chains-public-keys.json` to Midnight Foundation. Documented as G14 in the runbook.

### Section 2 — Monitoring (`monitoring/`)
Prometheus + Grafana + Alertmanager stack deployed via Docker Compose. Six alerts across Cardano and Midnight node covering chain stall, low peer count, service crash, finality lag, and Midnight-specific signals. Alert routing config includes commented-out Slack and PagerDuty receiver examples.

**Alert design summary** — full rationale and operational responses in `monitoring/README.md`:

| Alert | Severity | Why it's actionable |
|---|---|---|
| `CardanoChainStall` | critical | Block progression is the single most important signal — if blocks stop, nothing else matters |
| `CardanoLowPeerCount` | warning | Leading indicator; fires before a stall, giving a window to fix connectivity |
| `ServiceDown` | critical | Covers all three services; `up == 0` regardless of crash reason; 2-min window avoids restart noise |
| `MidnightChainStall` | critical | Stalled Midnight height with Cardano healthy usually means a peer ban or bootstrap issue |
| `MidnightFinalityLag` | warning | Best block advancing but finality stalling = peers not reaching consensus; catch early |
| `MidnightLowPeerCount` | warning | Fewer than 2 peers usually means PeerID soft-banned — rotate network key if fires post-restart |

CPU intentionally not alerted on — Cardano's GC spikes regularly hit 80–100% during normal block validation; a fixed threshold would be constant noise. Chain stall is the real signal.

> **Production note:** In a Kubernetes environment this stack would be fronted by an OpenTelemetry Collector as the ingestion layer, normalising metrics, logs, and traces before forwarding to Prometheus and Grafana. The Docker Compose setup here is functionally equivalent for a single-node preprod deployment — the deliberate choice to keep it simple rather than a gap in the design.

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

### Section 4 — Security (`docs/SECURITY.md`)
Key storage (KMS + Vault), rotation procedure, and incident response answers.

---

## Assumptions

- **WSL 2 as the deployment target** — the homework doesn't specify an OS. Running on Linux bare-metal or in a VM would skip Phases 0–1 entirely and avoid the WSL-specific gotchas (NTFS socket limitation, virtual disk sizing).
- **Cardano node as relay, not block producer** — FNO validators don't need to mint Cardano blocks. A relay is simpler, safer (no signing keys on machine), and sufficient for db-sync to read from.
- **Pinned cardano-node 10.6.2** — not upgraded to 10.7.1 despite it being newer. cardano-db-sync 13.6.0.7 was built and tested against 10.6.x libraries; upgrading without a matching db-sync release risks data corruption.
- **db-sync synced from scratch** — IOG publishes pg_restore snapshots that would have cut the 6–8 hour sync to minutes, but the Midnight FNO docs don't reference them and preprod snapshot availability is inconsistent. Documented in the runbook as an optimisation for future runs.
- **Midnight node 0.22.2** — as specified in the official Midnight Foundation Notion doc. Tested v0.22.5 (latest stable on GitHub) as well — both versions hit the same bootstrap error, confirming it's not version-specific. Using 0.22.2 per docs.

---

## What I'd Do Differently With More Time

- **Complete FNO onboarding** — submit `partner-chains-public-keys.json`, confirm whitelisting in the `permissioned_candidates` set, and verify the node produces blocks. That's the natural conclusion of the whole exercise and the one remaining step.
- **Wire up Alertmanager receivers** — the config has Slack and PagerDuty stubs commented out. Connecting them to a real webhook would close the loop on Section 2 and make the alerts actually fire somewhere.
- **Use the IOG db-sync pg_restore snapshot** to skip the multi-hour cold sync. Syncing from epoch 0 is fine for a one-off exercise; in production this is always done from a snapshot.
- **Automate the setup with Ansible** — the runbook is fully reproducible but still manual. An idempotent playbook would make version upgrades and new node provisioning significantly faster.
- **Inject secrets at deploy time** — the runbook uses a placeholder password in pgpass. In production this would come from Vault agent or AWS SSM at startup, never stored in any file.
- **Add a liveness watchdog** — systemd `Restart=always` handles crashes but not a running-but-stuck node. A watchdog that checks block progression and restarts if stalled would catch the class of issue documented in G14.
