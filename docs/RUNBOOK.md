# RUNBOOK — Midnight FNO Node Setup (Windows / WSL)

Audience: engineer who knows DevOps but not Midnight specifically.  
OS: **Windows 11 with WSL 2**, Ubuntu 24.04 on D: drive.  
The Cardano chain block data lives on `/mnt/d` to avoid filling the WSL virtual disk. All other state (db-sync state, Midnight node data) stays on the WSL ext4 virtual disk.

> **Windows only.** The WSL setup in Phases 0–1 is Windows-specific — skip straight to Phase 2 if you're on macOS or Linux. From Phase 2 onwards the steps are the same on any OS, but paths and a few NTFS/ext4 quirks won't apply to you.

---

## Table of Contents

- [Variables](#variables)
- [Phase 0 — Install WSL 2 (Ubuntu 24.04) on D: Drive](#phase-0--install-wsl-2-ubuntu-2404-on-d-drive)
- [Phase 1 — Configure WSL Resources](#phase-1--configure-wsl-resources)
- [Phase 2 — Base Dependencies & Directory Structure](#phase-2--base-dependencies--directory-structure)
- [Phase 3 — Download Cardano DB Snapshot via Mithril](#phase-3--download-cardano-db-snapshot-via-mithril-)
- [Phase 4 — Install Cardano Node (v10.6.2)](#phase-4--install-cardano-node-v1062-)
- [Phase 5 — PostgreSQL 17](#phase-5--postgresql-17)
- [Phase 6 — Install cardano-db-sync (13.6.0.7)](#phase-6--install-cardano-db-sync-13607)
  - [Phase 6A — Install binaries, schema, config and service unit](#phase-6a--install-binaries-schema-config-and-service-unit)
  - [Phase 6B — Start db-sync (only after node socket is available)](#phase-6b--start-db-sync-only-after-node-socket-is-available)
- [Phase 7 — Verify DB Sync (wait for ~100%)](#phase-7--verify-db-sync-wait-for-100)
- [Phase 8 — Install Midnight Node & Generate Validator Keys](#phase-8--install-midnight-node--generate-validator-keys)
  - [Start the Midnight node](#start-the-midnight-node)
- [Steady-State Health Reference](#steady-state-health-reference)
- [Troubleshooting](#troubleshooting)
  - [Troubleshooting Decision Tree](#troubleshooting-decision-tree)
- [Cleanup (Reset to Clean State)](#cleanup-reset-to-clean-state)

---

## Variables

| Variable | Value | Notes |
|---|---|---|
| `YOUR_USER` | `knight` | Your Linux username inside WSL |
| `NETWORK` | `preprod` | Target Cardano network; drives paths and URLs throughout |
| `CARDANO_VERSION` | `10.6.2` | Pin this — must match db-sync |
| `DB_SYNC_VERSION` | `13.6.0.7` | Must match schema dir in tarball |
| `MIDNIGHT_RELEASE` | `node-0.22.2` | Pin to this — matches the official FNO Preprod Notion docs. See Issue 8 on why using the latest (0.22.5) is not recommended. |

---

## Phase 0 — Install WSL 2 (Ubuntu 24.04) on D: Drive

> **Why Ubuntu 24.04?** Midnight node requires GLIBC ≥ 2.39. Ubuntu 22.04 ships 2.35 and will fail at runtime.  
> **Why D: drive?** The Mithril snapshot alone is ~40 GB. WSL's default C: virtual disk fills up fast.

Run in **PowerShell (Admin)**:

```powershell
# Install Ubuntu 24.04 without launching (lands on C: temporarily)
wsl --install -d Ubuntu-24.04 --no-launch

# Verify it registered
wsl --list --verbose

# Create target directory on D:
mkdir D:\WSL

# Export to D: drive
wsl --export Ubuntu-24.04 D:\WSL\ubuntu-24.04.tar

# Unregister from C: (deletes the C: vhdx)
wsl --unregister Ubuntu-24.04

# Re-import onto D: drive
wsl --import Ubuntu-24.04 D:\WSL\Ubuntu-24.04 D:\WSL\ubuntu-24.04.tar --version 2

# Set as default distro
wsl --set-default Ubuntu-24.04

# Clean up the export tar
Remove-Item D:\WSL\ubuntu-24.04.tar

# Launch as root (import resets to root — user doesn't exist yet)
wsl -d Ubuntu-24.04
```

Inside WSL (running as root):

```bash
# Create your user and grant sudo
useradd -m -s /bin/bash -G sudo knight
passwd knight     # set a password when prompted

# Set as default login user
echo -e '[user]\ndefault=knight' > /etc/wsl.conf
exit
```

Back in PowerShell:

```powershell
# Restart WSL to apply wsl.conf
wsl --shutdown

# Launch — should now land as knight
wsl -d Ubuntu-24.04
```

---

## Phase 1 — Configure WSL Resources

Create `$env:USERPROFILE\.wslconfig` on Windows to cap RAM/CPU (prevents OOM during sync):

```powershell
@"
[wsl2]
memory=16GB
swap=8GB
processors=8
"@ | Set-Content "$env:USERPROFILE\.wslconfig"

wsl --shutdown
```

Then inside WSL, enable systemd (makes `systemctl` work natively):

```bash
sudo tee /etc/wsl.conf > /dev/null <<'EOF'
[boot]
systemd=true
[user]
default=knight
EOF
```

Back in PowerShell:

```powershell
wsl --shutdown
wsl -d Ubuntu-24.04
# Confirm systemd is running:
#   systemctl status
```

---

## Phase 2 — Base Dependencies & Directory Structure

Inside WSL:

```bash
sudo apt update
sudo apt install -y curl tar jq git rsync ca-certificates

# Binaries and configs in home dir (WSL ext4 — fast I/O)
mkdir -p ~/.local/bin ~/.local/share ~/cardano-data ~/res ~/tmp/mithril

# Only the Cardano chain block DB goes on D: — it's ~40 GB and sequential-read-heavy
# db-sync state and Midnight node data stay on ext4 (socket/lock file compatibility, see Issue 9)
mkdir -p /mnt/d/cardano/db          # Mithril snapshot and cardano-node DB land here

# Symlink so cardano-data/db points to D:
ln -s /mnt/d/cardano/db ~/cardano-data/db

# Add ~/.local/bin to PATH
echo 'export PATH=$HOME/.local/bin:$PATH' >> ~/.bashrc
source ~/.bashrc
```

---

## Phase 3 — Download Cardano DB Snapshot via Mithril ✅

> **Start this immediately and leave it running** — the snapshot is ~40 GB and takes hours to download and verify. Nothing else can proceed until db-sync is fully synced.
> **Completed:** snapshot downloaded to `/mnt/d/cardano/db` (~17 GB on preprod). Symlink `~/cardano-data/db -> /mnt/d/cardano/db` confirmed working.

```bash
cd ~/tmp/mithril

# Install mithril-client only — the three Mithril components have distinct roles:
#   mithril-client     → downloads snapshots to bootstrap a node DB  ← we need this
#   mithril-signer     → signs snapshots; only needed if you run an active Cardano SPO
#   mithril-aggregator → aggregates signatures into certificates; operated by IOG, not node operators
curl --proto '=https' --tlsv1.2 -sSf \
  https://raw.githubusercontent.com/input-output-hk/mithril/refs/heads/main/mithril-install.sh \
  | sh -s -- -c mithril-client -d unstable -p $(pwd)

# Set network environment
NETWORK="preprod"
export CARDANO_NETWORK=${NETWORK}
# Note: Mithril uses 'release-preprod' (not just 'preprod') as its environment name — those URLs are fixed
export AGGREGATOR_ENDPOINT=https://aggregator.release-preprod.api.mithril.network/aggregator
export GENESIS_VERIFICATION_KEY=$(wget -q -O - \
  https://raw.githubusercontent.com/input-output-hk/mithril/main/mithril-infra/configuration/release-preprod/genesis.vkey)
export ANCILLARY_VERIFICATION_KEY=$(wget -q -O - \
  https://raw.githubusercontent.com/input-output-hk/mithril/main/mithril-infra/configuration/release-preprod/ancillary.vkey)
export SNAPSHOT_DIGEST=latest

# Download directly to D: drive (avoids WSL ext4 space limit)
./mithril-client cardano-db download \
  --include-ancillary \
  --download-dir /mnt/d/cardano \
  $SNAPSHOT_DIGEST
# Creates /mnt/d/cardano/db/
```

---

## Phase 4 — Install Cardano Node (v10.6.2) ✅

> **Version rationale:** `10.6.2` is intentionally pinned rather than using the latest (`10.7.1` as of 2026-05-02). `cardano-db-sync 13.6.0.7` was built and tested against the `10.6.x` consensus/ledger libraries — upgrading the node binary without a matching db-sync build risks library version mismatches that can cause db-sync crashes or data corruption. Additionally, `10.7.1` introduces new OS-level dependencies (`liburing`, `protobuf-compiler`, `snappy-c`) which would need separate installation. Until Midnight FNO docs explicitly pair a newer db-sync release with a newer node version, `10.6.2` is the safe choice.

> **Gotcha hit:** the tarball nests `share/` inside `share/` — use `--strip-components=2` when extracting to `~/.local/share`, otherwise config files land at `~/.local/share/share/preprod/` and the node fails to start.  
> **Status:** service running, ledger replay in progress (~1.79% at start). Replay rebuilds ledger state from the Mithril snapshot blocks — takes ~1–2 hours, no network download needed. Phase 5 can start in parallel.

```bash
VERSION="10.6.2"
ARCH="linux-amd64"
BASE_URL="https://github.com/IntersectMBO/cardano-node/releases/download/${VERSION}"

# Download binaries and share files
curl -L "${BASE_URL}/cardano-node-${VERSION}-${ARCH}.tar.gz" \
  | tar -xz -C ~/.local/bin --strip-components=2 ./bin
# Note: tarball nests share/ inside share/ — strip 2 components to land at ~/.local/share/preprod/
curl -L "${BASE_URL}/cardano-node-${VERSION}-${ARCH}.tar.gz" \
  | tar -xz -C ~/.local/share --strip-components=2 ./share

chmod +x ~/.local/bin/cardano-*
cardano-node --version    # should print 10.6.2

# Deploy systemd service (replace knight if your username differs)
sudo tee /etc/systemd/system/cardano-node.service > /dev/null <<'EOF'
[Unit]
Description=Cardano Relay Node (Preprod)
Wants=network-online.target
After=network-online.target

[Service]
User=knight
Type=simple
WorkingDirectory=/home/knight/cardano-data
ExecStart=/home/knight/.local/bin/cardano-node run \
    --topology /home/knight/.local/share/preprod/topology.json \
    --database-path /home/knight/cardano-data/db \
    --socket-path /home/knight/cardano-data/node.socket \
    --host-addr 0.0.0.0 \
    --port 3001 \
    --config /home/knight/.local/share/preprod/config.json
KillSignal=SIGINT
Restart=always
RestartSec=5
LimitNOFILE=32768

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now cardano-node
sudo journalctl -u cardano-node -f
```

---

## Phase 5 — PostgreSQL 17

> Use the PGDG repo — the default Ubuntu apt repo gives PostgreSQL 14/16, which may have issues. Version 17 is required.

```bash
# Install PostgreSQL 17 from PGDG
sudo apt install -y curl ca-certificates
sudo install -d /usr/share/postgresql-common/pgdg
sudo curl -s -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc --fail \
  https://www.postgresql.org/media/keys/ACCC4CF8.asc
sudo sh -c 'echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
  https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > \
  /etc/apt/sources.list.d/pgdg.list'
sudo apt update && sudo apt install -y postgresql-17 postgresql-server-dev-17

sudo systemctl enable --now postgresql

# Create DB user and database (names must match docs: midnight / cexplorer)
sudo -u postgres psql <<'SQL'
CREATE USER midnight WITH PASSWORD 'your_secure_password';
ALTER ROLE midnight WITH SUPERUSER CREATEDB;
CREATE DATABASE cexplorer;
SQL

# Create pgpass (never put passwords in service unit files)
# Use 127.0.0.1 (TCP) not the Unix socket path — peer auth rejects non-matching OS usernames
# Use explicit path — ~ can resolve incorrectly depending on how the shell was invoked
echo "127.0.0.1:5432:cexplorer:midnight:your_secure_password" > /home/knight/.pgpass
chmod 0600 /home/knight/.pgpass

# Performance tuning for sync speed
sudo tee -a /etc/postgresql/17/main/postgresql.conf > /dev/null <<'EOF'
shared_buffers = 4GB
maintenance_work_mem = 1GB
max_parallel_maintenance_workers = 2
effective_cache_size = 12GB
join_collapse_limit = 1
EOF

sudo systemctl restart postgresql

# Verify — must use -h 127.0.0.1 to force TCP/password auth (peer auth will fail for non-postgres OS users)
psql -h 127.0.0.1 -U midnight -d cexplorer -c "SELECT 1;"
```

---

## Phase 6 — Install cardano-db-sync (13.6.0.7)

This phase is split into two parts:
- **Part A** can be done immediately — install binaries, schema and config, deploy the service unit.
- **Part B** must wait until the cardano-node ledger replay finishes and its socket is available.

> **Optional fast-track:** IOG publishes `pg_dump` snapshots of the fully-synced `cexplorer` DB at `https://update-cardano-mainnet.iohk.io/cardano-db-sync/index.html#13.6/` — restoring one with `pg_restore` skips the 6–8 hour sync entirely, same idea as Mithril for the chain data.  
>  
> We didn't use it because the FNO docs don't mention it and preprod snapshot availability is hit-or-miss, but if you're short on time it's worth checking. Just make sure the snapshot version matches `13.6.0.7` exactly — a mismatch gives a confusing restore error.

### Phase 6A — Install binaries, schema, config and service unit
> Do this now, in parallel with the node replay.

```bash
# Download and extract
cd ~/tmp
curl -L -O https://github.com/IntersectMBO/cardano-db-sync/releases/download/13.6.0.7/cardano-db-sync-13.6.0.7-linux.tar.gz
tar -xzf cardano-db-sync-13.6.0.7-linux.tar.gz

# Install binary
cp bin/* ~/.local/bin/

# Install schema (extracted read-only — use cp -r, not mv, then fix permissions)
mkdir -p ~/cardano-data/db-sync-state
cp -r ~/tmp/schema ~/cardano-data/
chmod -R u+w ~/cardano-data/schema

# Download and patch db-sync config to point at cardano-node's config
NETWORK="preprod"
cd ~/cardano-data
curl -O "https://book.world.dev.cardano.org/environments/${NETWORK}/db-sync-config.json"
sed -i "s|\"NodeConfigFile\": \"config.json\"|\"NodeConfigFile\": \"/home/knight/.local/share/${NETWORK}/config.json\"|" \
  ~/cardano-data/db-sync-config.json

# Verify patch
grep NodeConfigFile ~/cardano-data/db-sync-config.json

# Deploy the systemd service unit
sudo tee /etc/systemd/system/cardano-db-sync.service > /dev/null <<'EOF'
[Unit]
Description=Cardano DB Sync (Preprod)
After=cardano-node.service
Requires=cardano-node.service

[Service]
User=knight
Type=simple
Environment="PGPASSFILE=/home/knight/.pgpass"
WorkingDirectory=/home/knight/cardano-data
ExecStart=/home/knight/.local/bin/cardano-db-sync \
    --config /home/knight/cardano-data/db-sync-config.json \
    --socket-path /home/knight/cardano-data/node.socket \
    --schema-dir /home/knight/cardano-data/schema \
    --state-dir /home/knight/cardano-data/db-sync-state
KillSignal=SIGINT
Restart=always
RestartSec=10
LimitNOFILE=32768

[Install]
WantedBy=multi-user.target
EOF

# Enable but do NOT start yet — the node socket doesn't exist until replay finishes
sudo systemctl daemon-reload
sudo systemctl enable cardano-db-sync
```

### Phase 6B — Start db-sync (only after node socket is available)
> The cardano-node must finish its ledger replay before db-sync can connect.  
> Monitor replay progress in a separate terminal:
> ```bash
> sudo journalctl -u cardano-node -f | grep -E "Progress|AddedToCurrentChain"
> ```
> Wait until you see `Progress: 100%` or chain-following messages, then confirm the socket:

```bash
# Confirm the socket exists before starting
ls -lh ~/cardano-data/node.socket

# Start db-sync and follow logs
sudo systemctl start cardano-db-sync
sudo journalctl -u cardano-db-sync -f
```

Look for lines like `Subscription started` or `block ... after` in the logs to confirm db-sync is connected and processing.

---

## Phase 7 — Verify DB Sync (wait for ~100%)

```bash
# Latest synced block  (use -h 127.0.0.1 to force TCP auth — see Issue 3)
psql -h 127.0.0.1 -U midnight -d cexplorer -c "SELECT block_no, slot_no, time FROM block ORDER BY id DESC LIMIT 1;"

# Sync percentage
psql -h 127.0.0.1 -U midnight -d cexplorer -c "
SELECT
  100 * (EXTRACT(epoch FROM (MAX(time) AT TIME ZONE 'UTC')) -
         EXTRACT(epoch FROM (MIN(time) AT TIME ZONE 'UTC')))
  / (EXTRACT(epoch FROM (NOW() AT TIME ZONE 'UTC')) -
     EXTRACT(epoch FROM (MIN(time) AT TIME ZONE 'UTC')))
AS sync_percent
FROM block;"
```

Do not proceed to Phase 8 until `sync_percent` reaches ~100.

> **Evidence:** Sync verified on 2026-05-04 at 99.9999% (block 4,671,291, slot 122,189,878) before proceeding to Phase 8. Full query output: [`docs/evidence/db-sync-verification.md`](evidence/db-sync-verification.md).

---

## Phase 8 — Install Midnight Node & Generate Validator Keys

```bash
# Download and install — use 0.22.2 per official FNO docs (see Issue 8 before upgrading)
MIDNIGHT_RELEASE="node-0.22.2"
cd ~/tmp
curl -L -O "https://github.com/midnightntwrk/midnight-node/releases/download/${MIDNIGHT_RELEASE}/midnight-node-0.22.2-linux-amd64.tar.gz"
tar -xvzf midnight-node-0.22.2-linux-amd64.tar.gz

mv ~/tmp/midnight-node ~/.local/bin/
mv ~/tmp/res ~/res
# The tarball nests contents under an extra res/ — flatten it (see Issue 10)
mv ~/res/res/* ~/res/ && rmdir ~/res/res
chmod +x ~/.local/bin/midnight-node
source ~/.bashrc
midnight-node --version

# Generate session keys — back these up immediately to an offline, secure location
cd ~
midnight-node key generate --scheme sr25519 --output-type json > aura.json
midnight-node key generate --scheme ed25519 --output-type json > grandpa.json
midnight-node key generate --scheme ecdsa   --output-type json > cross_chain.json
chmod 600 aura.json grandpa.json cross_chain.json

# > **Production note:** The commands above store raw key material as plaintext JSON files.
# > For a production validator, signing keys (aura, grandpa, cross_chain) should be backed
# > by a remote signing service (Cloud KMS, HSM, or a dedicated signing sidecar) so that
# > private key material never touches the node host's disk. The JSON files are acceptable
# > for preprod onboarding; see [`notes/SECURITY.md`](SECURITY.md) for the recommended
# > production architecture (KMS-backed signing, Vault for auxiliary secrets, systemd
# > LoadCredential= injection).

# Generate network identity key (P2P PeerID)
NETWORK="preprod"
NETWORK_DIR="$HOME/data/chains/midnight_${NETWORK}/network"
mkdir -p "$NETWORK_DIR" && chmod 700 "$NETWORK_DIR"
# --chain is required; without it the command errors (see Issue 11)
midnight-node key generate-node-key --file "$NETWORK_DIR/secret_ed25519" --chain ~/res/preprod/chain-spec-raw.json
midnight-node key inspect-node-key --file "$NETWORK_DIR/secret_ed25519"   # prints PeerID

# Insert keys into the keystore
KEYSTORE_PATH="$HOME/data/chains/midnight_preprod/keystore"
mkdir -p "$KEYSTORE_PATH"

# --chain is required for all key insert calls too (see Issue 11)
midnight-node key insert --keystore-path "$KEYSTORE_PATH" --scheme sr25519 --key-type aura \
  --chain ~/res/preprod/chain-spec-raw.json \
  --suri "$(jq -r .secretPhrase aura.json)"
midnight-node key insert --keystore-path "$KEYSTORE_PATH" --scheme ed25519 --key-type gran \
  --chain ~/res/preprod/chain-spec-raw.json \
  --suri "$(jq -r .secretPhrase grandpa.json)"
midnight-node key insert --keystore-path "$KEYSTORE_PATH" --scheme ecdsa  --key-type beef \
  --chain ~/res/preprod/chain-spec-raw.json \
  --suri "$(jq -r .secretPhrase cross_chain.json)"

# Build validator registration JSON (send this to Midnight Foundation)
OUTPUT_FILE="$HOME/partner-chains-public-keys.json"
cat <<EOF > "$OUTPUT_FILE"
{
  "partner_chains_key": "$(jq -r .publicKey cross_chain.json)",
  "keys": {
    "aura": "$(jq -r .publicKey aura.json)",
    "crch": "$(jq -r .publicKey cross_chain.json)",
    "gran": "$(jq -r .publicKey grandpa.json)"
  }
}
EOF
cat "$OUTPUT_FILE"
```

### Start the Midnight node

Create a `.env` file to hold all required environment variables:

```bash
cat > ~/.env << 'EOF'
# DB connection — must be a URL, not key=value format (see Issue 12)
# Replace 'midnight' (password) with the password you set in Phase 5
DB_SYNC_POSTGRES_CONNECTION_STRING=postgresql://midnight:your_secure_password@localhost:5432/cexplorer

# Cardano preprod parameters — use these values, not anything from chain spec files
CARDANO_SECURITY_PARAMETER=432
BLOCK_STABILITY_MARGIN=30

# Node identity
CFG_PRESET=preprod
NODE_NAME=my-preprod-fno
EOF

source ~/.env
```

Then start the node:

```bash
midnight-node \
  --chain ~/res/preprod/chain-spec-raw.json \
  --base-path ~/data \
  --sync full \
  --pool-limit 35 \
  --name "${NODE_NAME}" \
  --rpc-port 9933 \
  2>&1 | tee ~/midnight-node.log
```

> **First run:** the node spends 5–10 minutes building PostgreSQL indexes on the cexplorer DB before it does anything else. You'll see slow statement warnings in the logs — that's fine, just let it finish.
>
> **"unknown parent" errors** at the start are normal — the node gets blocks slightly out of order until it finds its footing. They go away on their own.
>
> **Bootnodes** are baked into the chain spec, so no `--bootnodes` flag needed. `--reserved-only` and WireGuard aren't used on preprod either.

---

## Steady-State Health Reference

This is what a correctly operating FNO stack looks like. Use this as the baseline before declaring a system healthy and before starting incident investigation. Check layers top-to-bottom — a failure in a lower layer will cascade upward.

---

### Layer 1 — cardano-node

**What healthy looks like:**
- Service is active (running), not crash-looping
- Socket exists at `~/cardano-data/node.socket`
- Node is at or within 1–2 slots of chain tip
- No repeated `ExceededTimeLimit` or `PeerDisconnected` floods in logs

```bash
# Service status
systemctl is-active cardano-node

# Socket present
ls -lh ~/cardano-data/node.socket

# Chain tip — era should be "Conway", syncProgress should be "1.00"
cardano-cli query tip --testnet-magic 1 --socket-path ~/cardano-data/node.socket
```

**Expected output:**
```json
{
  "era": "Conway",
  "syncProgress": "1.00",
  "slot": 122189878,
  "block": 4671291
}
```

**Not healthy if:** `syncProgress` < `"1.00"`, era is `"Byron"` (still replaying), or socket is absent.

---

### Layer 2 — PostgreSQL + cardano-db-sync

**What healthy looks like:**
- PostgreSQL service active
- db-sync service active, not crash-looping
- `sync_percent` > 99.99
- Latest indexed block within 2–3 blocks of cardano-node tip
- No `db-sync` log lines containing `rollback` loops or `MigrationError`

```bash
# Service status
systemctl is-active postgresql cardano-db-sync

# Sync percentage — should be >= 99.99
psql -h 127.0.0.1 -U midnight -d cexplorer -c "
SELECT ROUND(
  100 * (EXTRACT(epoch FROM (MAX(time) AT TIME ZONE 'UTC')) -
         EXTRACT(epoch FROM (MIN(time) AT TIME ZONE 'UTC')))
  / (EXTRACT(epoch FROM (NOW() AT TIME ZONE 'UTC')) -
     EXTRACT(epoch FROM (MIN(time) AT TIME ZONE 'UTC'))), 6) AS sync_percent
FROM block;"

# Latest indexed block — compare block_no to cardano-cli query tip output above
psql -h 127.0.0.1 -U midnight -d cexplorer \
  -c "SELECT block_no, slot_no, time FROM block ORDER BY id DESC LIMIT 1;"

# db-sync lag — rows in this table indicate blocks received but not yet indexed
psql -h 127.0.0.1 -U midnight -d cexplorer \
  -c "SELECT COUNT(*) AS pending_blocks FROM block WHERE epoch_no IS NULL;"
```

**Expected output:** `sync_percent` = 99.999x, `pending_blocks` = 0, latest `block_no` matches cardano-node tip.

**Not healthy if:** sync_percent stalled below 99%, `pending_blocks` > 0 and growing, or db-sync is crash-looping (`systemctl status cardano-db-sync`).

---

### Layer 3 — midnight-node (pre-whitelisting)

Pre-whitelisting is a defined operational state, not a failure. The healthy signals here are about infrastructure readiness, not block production.

**What healthy looks like:**
- Process running, connected to 3+ peers
- Keystore populated with all three key types: `aura`, `gran`, `cross_chain`
- `partner-chains-public-keys.json` generated and ready for submission
- Logs show `Validator inherent data must be provided` (expected — confirms pre-whitelisting state, not a misconfiguration)
- No `PeerBanned`, socket errors, or db-sync connection failures in logs

```bash
# Peer count — look for "peers=N" in recent log lines (expect 3–7 on preprod)
journalctl -u midnight-node -n 50 --no-pager | grep -E "peers=[0-9]+|best:"

# Keystore populated — expect aura (sr25519), gran (ed25519), cross_chain (ecdsa)
ls ~/data/chains/midnight_preprod/keystore/

# Public key file present
test -f ~/partner-chains-public-keys.json && cat ~/partner-chains-public-keys.json

# RPC alive — expects a JSON result, not a connection error
curl -s -X POST http://localhost:9933 \
  -H 'Content-Type: application/json' \
  -d '{"id":1,"jsonrpc":"2.0","method":"system_health","params":[]}'
```

**Expected RPC output:**
```json
{"jsonrpc":"2.0","result":{"isSyncing":false,"peers":5,"shouldHavePeers":true},"id":1}
```

**Not healthy if:** `peers` = 0 and not recovering (→ Issue 13), RPC returns connection refused, keystore directory is empty, or db-sync connection errors appear in midnight-node logs.

---

### Full-stack quick check

Run this after any restart or change to get a one-shot view of all four services:

```bash
echo "=== Service Status ==="
for svc in cardano-node cardano-db-sync postgresql midnight-node; do
  printf "%-22s %s\n" "$svc" "$(systemctl is-active $svc 2>/dev/null || echo not-installed)"
done

echo ""
echo "=== cardano-node tip ==="
cardano-cli query tip --testnet-magic 1 --socket-path ~/cardano-data/node.socket 2>/dev/null \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"era={d['era']} sync={d['syncProgress']} block={d['block']}\")"

echo ""
echo "=== db-sync sync ==="
psql -h 127.0.0.1 -U midnight -d cexplorer -t -c \
  "SELECT 'sync_percent=' || ROUND(100*(EXTRACT(epoch FROM (MAX(time) AT TIME ZONE 'UTC'))-EXTRACT(epoch FROM (MIN(time) AT TIME ZONE 'UTC')))/(EXTRACT(epoch FROM (NOW() AT TIME ZONE 'UTC'))-EXTRACT(epoch FROM (MIN(time) AT TIME ZONE 'UTC'))),4) FROM block;" 2>/dev/null

echo ""
echo "=== midnight-node peers ==="
curl -s -X POST http://localhost:9933 \
  -H 'Content-Type: application/json' \
  -d '{"id":1,"jsonrpc":"2.0","method":"system_health","params":[]}' 2>/dev/null \
  | python3 -c "import sys,json; d=json.load(sys.stdin)['result']; print(f\"peers={d['peers']} syncing={d['isSyncing']}\")"
```

**Healthy output looks like:**
```
=== Service Status ===
cardano-node           active
cardano-db-sync        active
postgresql             active
midnight-node          active

=== cardano-node tip ===
era=Conway sync=1.00 block=4671291

=== db-sync sync ===
 sync_percent=99.9999

=== midnight-node peers ===
peers=5 syncing=false
```

---

## Troubleshooting

Things that actually went wrong during this setup. Saving the next person the same headaches.

---

## Troubleshooting Decision Tree

Use this to identify which gotcha applies before reading the full entries below.

### Midnight node stuck at `best: #0`

```
Is the peer count 0?
├── YES → Has the node connected to peers on a previous run?
│          ├── YES → Peers soft-banned the old PeerID → Issue 13 (regenerate network key)
│          └── NO  → Check socket path is on ext4, not NTFS → Issue 9
└── NO  → Peers connected but block not advancing
           └── "Validator inherent data must be provided" in logs?
                ├── YES → Node not whitelisted in permissioned_candidates yet → Issue 14
                └── NO  → Wait — initial PostgreSQL index build takes 5–10 min on first run
```

### cardano-node crashes or won't start

```
When does it crash?
├── Immediately, config error → Wrong extract path for share/ tarball → Issue 2
├── After full ledger replay, at StartedInitChainSelection → Unix socket on NTFS → Issue 9
└── Random crash-loops → db-sync trying to connect before socket is ready → Issue 6
```

### cardano-db-sync won't start / crash-loops

```
Error message?
├── Socket not found / ENOENT → Start db-sync only after node socket exists → Issue 6
├── Permission denied on schema/ → Read-only schema directory after extract → Issue 7
└── "relative URL without a base" → Connection string is key=value, not a URL → Issue 12
```

### Postgres / psql access denied

```
├── "Peer authentication failed" → Use -h 127.0.0.1, not socket path → Issue 3
├── pgpass not used / credentials not found → Wrong home directory in pgpass path → Issue 4
└── DB sync still at 0% after hours → Check sync_percent: SELECT sync_percent FROM chain_sync_progress;
```

### midnight-node binary panics immediately

```
├── "failed reading default.toml" → Double-nested res/ directory after extract → Issue 10
└── "chainspec_genesis_block not configured" → Missing --chain flag on key subcommands → Issue 11
```

### Download or extract fails

```
├── gzip: stdin: not in gzip format (9-byte download) → Wrong tag in db-sync URL → Issue 5
└── Configs land at wrong path after extract → Wrong --strip-components value → Issue 2
```

---

> **Windows/WSL only — skip if you're on macOS or Linux**

### Issue 1 — WSL setup pitfalls (Phases 0–1)

Four things tripped us up during WSL install and configuration. None of this applies if you're on macOS or native Linux.

**Issue 1a — Export before unregister.** Ran `wsl --unregister` before the export finished, which permanently deleted the distro from C: with no recovery. Always confirm the `.tar` exists and is non-zero size first. Order: export → verify → unregister → import.

**Issue 1b — `wsl --import` with a downloaded rootfs fails.** Trying to `curl` the rootfs tarball from `cloud-images.ubuntu.com` gets a 286-byte HTML redirect, not the actual file. `wsl --import` then errors with "Unrecognized archive format". Use `wsl --install -d Ubuntu-24.04 --no-launch` instead.

**Issue 1c — `%USERPROFILE%` is CMD syntax, not PowerShell.** Use `$env:USERPROFILE`. E.g. `Set-Content "$env:USERPROFILE\.wslconfig"`.

**Issue 1d — `getpwnam(knight) failed` after import.** `wsl --import` resets to root with no users, and `wsl.conf` `[user] default=` is ignored until the user actually exists. On first launch you're root — create the user first (`useradd -m -s /bin/bash -G sudo knight && passwd knight`), set `wsl.conf`, then `wsl --shutdown` and relaunch.

---

### Issue 2 — Cardano node `share/` tarball has double-nested path (Phase 4)

**What happened:** Extracted the cardano-node tarball with `--strip-components=1 ./share` expecting configs to land at `~/.local/share/preprod/`. Instead they landed at `~/.local/share/share/preprod/`. The node immediately crash-looped with `Yaml file not found: /home/knight/.local/share/preprod/config.json`.

**Fix:** Use `--strip-components=2` when extracting `./share` from the tarball. The archive structure is `./share/share/preprod/...`, not `./share/preprod/...`.

**Corrected command:**
```bash
curl -L "${BASE_URL}/cardano-node-${VERSION}-${ARCH}.tar.gz" \
  | tar -xz -C ~/.local/share --strip-components=2 ./share
```

---

### Issue 3 — PostgreSQL peer authentication rejects non-matching OS username (Phase 5)

**What happened:** Running `psql -U midnight -d cexplorer` failed with `FATAL: Peer authentication failed for user "midnight"`. The OS user is `knight` but the Postgres role is `midnight` — peer auth requires them to match.

**Fix:** Connect over TCP by passing `-h 127.0.0.1`. This uses password authentication instead of peer auth:
```bash
psql -h 127.0.0.1 -U midnight -d cexplorer -c "SELECT 1;"
```
Also set pgpass with `127.0.0.1` as the host, not the socket path.

---

### Issue 4 — `~/.pgpass` resolves to wrong home directory (Phase 5)

**What happened:** Running `echo "..." > ~/.pgpass` updated a pgpass file that wasn't at `/home/knight/.pgpass`. The `~` resolved to a different home (likely `/root/` depending on how the shell was invoked), so db-sync couldn't find credentials.

**Fix:** Use the full path `/home/knight/.pgpass` instead of `~/.pgpass` — tilde expansion depends on how the shell was invoked, and it bit us here.

---

### Issue 5 — cardano-db-sync release URL in official docs has wrong tag (Phase 6)

**What happened:** The Midnight FNO docs reference this URL for db-sync:
```
https://github.com/IntersectMBO/cardano-db-sync/releases/download/13.6.0.5/cardano-db-sync-13.6.0.7-linux.tar.gz
```
The tag in the URL (`13.6.0.5`) does not match the filename (`13.6.0.7`). The download silently returned 9 bytes (a redirect/error response), causing `tar` to fail with `gzip: stdin: not in gzip format`.

**Fix:** The correct tag is `13.6.0.7`:
```bash
curl -L -O https://github.com/IntersectMBO/cardano-db-sync/releases/download/13.6.0.7/cardano-db-sync-13.6.0.7-linux.tar.gz
```
Always verify download size before extracting: `ls -lh <file>` — a valid tarball should be tens of MB, not single digits.

---

### Issue 6 — cardano-db-sync cannot start until node socket exists (Phase 6)

**What happened:** After installing cardano-db-sync, attempting to start it before the cardano-node finished its ledger replay results in crash-loops — the socket at `~/cardano-data/node.socket` doesn't exist until the replay completes.

**Fix:** Install the service with `systemctl enable` but do NOT `--now`. Wait for the node to finish replay (monitor with `journalctl -u cardano-node -f | grep Progress`), confirm the socket exists (`ls ~/cardano-data/node.socket`), then `systemctl start cardano-db-sync`.

---

### Issue 7 — cardano-db-sync `schema/` directory extracted read-only (Phase 6)

**What happened:** The tarball extracts the `schema/` directory with permissions `dr-xr-xr-x` (no write bit for owner). Running `mv ~/tmp/schema ~/cardano-data/` fails with `Permission denied` because mv needs write permission on the source directory to remove it.

**Fix:** Use `cp -r` instead of `mv`, then explicitly set write permissions:
```bash
cp -r ~/tmp/schema ~/cardano-data/
chmod -R u+w ~/cardano-data/schema
```

---

### Issue 8 — Tried `node-0.22.5` (latest) instead of `node-0.22.2` (docs) — both hit the same bootstrap issue (Phase 8)

**What happened:** The official Midnight FNO Preprod Notion docs pin the install to `node-0.22.2`. GitHub shows `node-0.22.5` as the latest `0.x.x` stable release. We initially installed `0.22.5` to pick up any fixes in the newer release.

**Result:** Both `0.22.2` and `0.22.5` produce the identical bootstrap error (see Issue 14 — `Main chain state ... not found`). The error is the same block hash, same error message, same behaviour on both versions. This confirms the issue is **not version-specific** — it is a fundamental bootstrap sequencing requirement affecting all fresh nodes against the current live preprod network.

**Fix:** Use `node-0.22.2` as specified in the official docs. Do not chase the latest release expecting it to fix this error — it won't. The underlying cause is the FNO whitelisting state (see Issue 14), not the binary version.

> Note: The `1.x.x` releases are all pre-release RCs (`node-1.0.0-toolkit-1.0.0-rc.x`). Do **not** use these for FNO preprod — use `0.22.2` as documented.

---

### Issue 9 — cardano-node crashes at `StartedInitChainSelection` on WSL — Unix socket on NTFS (Phases 4 & 6)

**Symptom:** cardano-node completes full ledger replay (~90% progress), then dies cleanly at exactly the `StartedInitChainSelection` log line with no OOM or signal.

**What happened:** cardano-node ran the full ledger replay (~90%), then died every single time right after the `StartedInitChainSelection` log line — clean exit, no OOM, no signal. Took a while to figure out. The `--socket-path` was pointing inside `~/cardano-data/db/`, which is a symlink to `/mnt/d/cardano/db` on NTFS. Turns out **WSL DrvFS doesn't support Unix domain sockets** — the node gets all the way through replay, then fails the moment it tries to create the socket on NTFS.

**Fix:** Set `--socket-path` to a path that resolves to the WSL ext4 filesystem — one level above the symlink:
```
--socket-path /home/knight/cardano-data/node.socket
```
`/home/knight/cardano-data/` itself is on ext4 (only the `db/` subdirectory is symlinked to NTFS). Phase 4 and Phase 6A in this runbook already use the correct path.

> **Rule of thumb for WSL:** keep chain block data (`db/`) on NTFS for space, but keep all socket files, lock files, and state files on the ext4 virtual disk.

---

### Issue 10 — `midnight-node key generate` panics: `failed reading default.toml` (Phase 8)

**Symptom:** Every `midnight-node key generate` command immediately panics with `failed reading default.toml at path /home/knight/res/cfg/default.toml: No such file or directory`, even though you extracted the tarball.

**What happened:** All three `midnight-node key generate` commands immediately panicked with `failed reading default.toml at path /home/knight/res/cfg/default.toml: No such file or directory`. The binary looks for its config at `~/res/cfg/default.toml` relative to the working directory.

**Root cause:** The tarball extracts into a double-nested structure — `res/res/` instead of `res/`. After `mv ~/tmp/res ~/res`, the actual contents land at `~/res/res/cfg/` rather than `~/res/cfg/`.

**Fix:** Flatten the directory after moving it:
```bash
mv ~/res/res/* ~/res/
rmdir ~/res/res
```

---

### Issue 11 — `midnight-node key generate-node-key` requires `--chain` flag (Phase 8)

**Symptom:** `midnight-node key generate-node-key` or `midnight-node key insert` exits immediately with `Input("chainspec_genesis_block not configured")` or a `NotFound` IO error.

**What happened:** Running `midnight-node key generate-node-key` or `midnight-node key insert` without a chain spec produced: `Input("chainspec_genesis_block not configured")` and a `NotFound` IO error.

**Fix:** Pass the preprod chain spec explicitly to every `midnight-node key` subcommand:
```bash
midnight-node key generate-node-key \
  --file "$NETWORK_DIR/secret_ed25519" \
  --chain ~/res/preprod/chain-spec-raw.json

midnight-node key insert ... \
  --chain ~/res/preprod/chain-spec-raw.json \
  --suri "..."
```

---

### Issue 12 — `DB_SYNC_POSTGRES_CONNECTION_STRING` must be a URL, not key=value format (Phase 8)

**Symptom:** midnight-node starts then immediately errors: `Failed to create db-sync main chain follower: error with configuration: relative URL without a base`.

**What happened:** Setting the env var as `"host=127.0.0.1 port=5432 dbname=cexplorer user=midnight password=midnight"` (libpq key=value format) caused the node to error: `Failed to create db-sync main chain follower: error with configuration: relative URL without a base`.

**Fix:** Use a PostgreSQL connection URL:
```bash
export DB_SYNC_POSTGRES_CONNECTION_STRING="postgresql://midnight:your_secure_password@127.0.0.1:5432/cexplorer"
```

---

### Issue 13 — Node stuck at `best: #0` with 0 peers after repeated restarts (Phase 8)

**Symptom:** `best: #0` with `0 peers` persists indefinitely after a restart. The node connected to peers on a previous run but never reconnects now — peers are immediately dropped.

**What happened:** The node showed `0 peers` indefinitely after a restart and never reconnected, even though it had connected to peers on a previous run.

**Root cause:** Preprod peers soft-ban PeerIDs that repeatedly fail block verification. While the node is not yet whitelisted (see Issue 14), the inherent data provider fails on every peer connection, ending each attempt with a protocol error. After a few cycles the PeerID is soft-banned by all live preprod peers and subsequent restarts with the same PeerID are immediately dropped.

**Fix:** Generate a fresh network identity key so the node presents a new PeerID to peers:
```bash
NETWORK_DIR="$HOME/data/chains/midnight_preprod/network"
rm "$NETWORK_DIR/secret_ed25519"
midnight-node key generate-node-key --file "$NETWORK_DIR/secret_ed25519" \
  --chain ~/res/preprod/chain-spec-raw.json
midnight-node key inspect-node-key --file "$NETWORK_DIR/secret_ed25519"   # confirm new PeerID
```

> **Bootnodes** do **not** need to be passed as CLI flags — they are embedded in the preprod chain spec (`chain-spec-raw.json`). Do not add `--bootnodes` flags; doing so can cause duplicate connection attempts and confuse the peer discovery process.

---

### Issue 14 — Block import stalled at `best: #0` on a fresh node (Phase 8)

**Symptom:** Node has peers (`5–7 peers` in logs), db-sync is fully synced, but `best: #0` never advances. Logs repeat `Validator inherent data must be provided` on every block announcement.

**What happened:** After connecting to peers, the node repeatedly logs `Validator inherent data must be provided` when verifying block announcements. Occurs with both v0.22.2 and v0.22.5.

The node successfully connects to peers and db-sync is fully synced (99.9999%, block 4,671,291 — confirmed at [`docs/evidence/db-sync-verification.md`](evidence/db-sync-verification.md)), so basic networking and chain data are healthy.

The error indicates the node cannot construct validator-specific inherent data during block verification. In a permissioned setup like Midnight preprod, that data depends on the node being registered in the on-chain `permissioned_candidates` set. This can be verified by querying the `permissioned_candidates` set on-chain, where the node's public key is not yet present. This behavior is consistent with the node not being whitelisted yet in the on-chain `permissioned_candidates` set, so block import stalls at genesis.

This aligns with the documented behavior — once the public keys are submitted and added on-chain, block processing should proceed normally.

**Fix:** Complete FNO onboarding by submitting `partner-chains-public-keys.json` for inclusion in the on-chain `permissioned_candidates` set. Once included, validator inherent data resolution succeeds and block import proceeds.

**Evidence captured:** Up to 7 peers connected, db-sync returning real committee data via SQL, all keys generated and keystore populated, `partner-chains-public-keys.json` produced. This represents the expected pre-whitelisting state in the FNO onboarding flow. Log: [`docs/evidence/midnight-node-log-evidence.txt`](evidence/midnight-node-log-evidence.txt).

---


## Cleanup (Reset to Clean State)

Only run if you need to start over. Destructive.

```bash
# Stop and remove systemd services
sudo systemctl stop cardano-db-sync cardano-node || true
sudo systemctl disable cardano-db-sync cardano-node || true
sudo rm -f /etc/systemd/system/cardano-db-sync.service \
           /etc/systemd/system/cardano-node.service
sudo systemctl daemon-reload

# Remove binaries
rm -f ~/.local/bin/cardano-db-sync ~/.local/bin/cardano-node ~/.local/bin/cardano-cli \
      ~/.local/bin/midnight-node

# Remove Cardano configs extracted to ~/.local/share
rm -rf ~/.local/share/preprod

# Remove all node data directories
rm -rf ~/cardano-data ~/res ~/data

# Remove scratch/temp directories (includes mithril-client binary)
rm -rf ~/tmp

# Remove midnight node log
rm -f ~/midnight-node.log

# Remove key files — only do this if you intend to re-generate keys
# If you've already submitted partner-chains-public-keys.json to Midnight Foundation, keep these
rm -f ~/aura.json ~/grandpa.json ~/cross_chain.json ~/partner-chains-public-keys.json

# Remove env and credentials files
rm -f ~/.env ~/.pgpass

# Remove D: drive chain data
rm -rf /mnt/d/cardano /mnt/d/mithril

# Revert PostgreSQL performance tuning (remove the 5 lines we appended in Phase 5)
sudo sed -i '/^shared_buffers = 4GB/d;/^maintenance_work_mem = 1GB/d;/^max_parallel_maintenance_workers = 2/d;/^effective_cache_size = 12GB/d;/^join_collapse_limit = 1/d' \
  /etc/postgresql/17/main/postgresql.conf
sudo systemctl restart postgresql

# Drop Postgres DB and role (optional — skip if you want to keep the data)
sudo -u postgres psql -c "DROP DATABASE IF EXISTS cexplorer;"
sudo -u postgres psql -c "DROP ROLE IF EXISTS midnight;"
```
