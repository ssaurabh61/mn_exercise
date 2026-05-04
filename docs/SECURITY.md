# Security & Key Management — Midnight FNO Preprod

> This document addresses Section 4 of the Midnight DevOps assessment.  
> Context: Midnight FNO nodes use three registered cryptographic key pairs — aura (sr25519, block authoring), grandpa (ed25519, finality), and cross_chain (ecdsa, Cardano bridge). Loss or compromise of any of these could disrupt the operator's participation in consensus or expose signed cross-chain transactions.

---

## 1. Key Storage in Production Cloud

### Recommended approach: Cloud KMS for signing, Vault for secret distribution

**Cloud KMS (AWS KMS / GCP Cloud KMS / Azure Key Vault)**
- The private key material never leaves the KMS boundary — signing operations are submitted to the KMS API and only the signature is returned
- Keys are backed by FIPS 140-2 Level 2 HSMs (Level 3 with Dedicated HSM tiers)
- Full audit trail via CloudTrail / Cloud Audit Logs — every signing operation is logged
- IAM policies restrict which service accounts / EC2 roles can invoke the sign operation
- Tradeoff: adds ~10–50ms latency per signing call; acceptable for block authoring timescales (slots are 1s on Cardano)
- **Multi-Region keys** (AWS KMS MRK / GCP Cloud KMS replication) replicate key material to a secondary region (e.g. London → Frankfurt). A total regional outage does not prevent signing or rotation once the node is recovered — the replica in the secondary region is immediately usable without any re-import or break-glass procedure

**AWS CloudHSM / Azure Dedicated HSM (if FIPS 140-2 Level 3 is required)**
- Key material is truly single-tenant and never shared with the cloud provider
- Required for regulatory compliance (financial services, government)
- Significantly more expensive (~$1.5k/month) and operationally complex
- Tradeoff: overkill for most FNO operators; adds operational burden without meaningful security improvement if the threat model is external compromise (not insider/cloud-provider threat)

**AWS Nitro Enclaves (for isolated signing services)**
- Nitro Enclaves carve out an isolated, memory-encrypted VM within an EC2 instance with no persistent storage, no interactive access, and no network socket — only a local vsock channel to the parent instance
- Cryptographic attestation (PCR measurements) lets a KMS key policy enforce that the signing service binary hasn't been tampered with before it is permitted to decrypt key material
- Primary use case here: hosting a custom sr25519 signing service (e.g., a Vault Transit sidecar or a thin Rust service) inside the enclave where the key seed is decrypted only in enclave memory and never exposed to the host OS
- This closes the gap left by cloud KMS not supporting the Schnorrkel/Ristretto curve natively
- Tradeoff: requires Nitro Enclaves-enabled instance types (m5n, c5n, r5n, etc.) and building/maintaining the enclave image; attestation document must be validated in the KMS key policy

**HashiCorp Vault (for auxiliary secrets)**
- Used for distributing secrets to the node process at startup (DB passwords, pgpass, API keys) — not for the cryptographic keys themselves
- Dynamic secrets (short-lived credentials generated on demand) reduce blast radius if a secret is leaked
- Vault agent can inject secrets as environment variables or files without the secret ever touching disk unencrypted
- Tradeoff: requires running and unsealing a Vault cluster — adds another service to operate and secure

**What I would not use:**
- **Plaintext files on disk** (`~/.local/share/midnight/keystore/*` as static files without encryption at rest) — acceptable for preprod; not acceptable for mainnet
- **Environment variables in systemd unit files** — visible to any process running as the same user; use `LoadCredential=` or Vault agent injection instead
- **AWS Secrets Manager for signing keys** — fine for passwords, not designed for asymmetric key material that needs to sign without exporting

### Recommended production layout

```
Signing keys (aura, grandpa, cross_chain)
  → Stored in AWS KMS / Cloud HSM
  → Node calls KMS API to sign; key never exported

Auxiliary secrets (DB password, API tokens)
  → HashiCorp Vault with dynamic secrets
  → Vault agent injects at startup via systemd LoadCredential

Key backups (disaster recovery only)
  → Encrypted with GPG, stored in S3 with Glacier lifecycle policy
  → Access requires MFA + break-glass procedure with audit log
```

### Tooling by key type

| Key | Curve | Recommended storage |
|---|---|---|
| `aura` | sr25519 (Schnorrkel/Ristretto) | HashiCorp Vault Transit Engine — cloud KMS does not support this curve; or a custom signing service running inside **AWS Nitro Enclave** for hardware-attested isolation |
| `grandpa` | ed25519 | AWS KMS / GCP Cloud KMS (native support) |
| `cross_chain` | ecdsa (secp256k1) | AWS KMS / GCP Cloud KMS (native support) |

---

## 2. Key Rotation

Midnight session keys are registered on-chain. Rotation must be done carefully to avoid a gap in consensus participation or a double-signing window.

### Procedure

1. **Generate the new key pair inside the KMS** — not on the node.
   Key material must be born inside the KMS hardware boundary and never exported in plaintext. Generating on the node (e.g. `midnight-node key generate`) means the private key transiently exists in process memory, shell history, and potentially swap — all of which can survive process termination and be recovered forensically.

   With AWS KMS:
   ```bash
   # Create an asymmetric signing key — material never leaves KMS
   aws kms create-key --key-spec ECC_SECG_P256K1 --key-usage SIGN_VERIFY
   ```
   The node calls the KMS sign API at runtime; only the signature is returned, never the key.

   For sr25519 (the Schnorrkel/Ristretto curve used by Aura), AWS KMS and GCP Cloud KMS have no native support. The recommended alternative is the **HashiCorp Vault Transit Engine**: Vault generates and stores the key internally, exposes a sign API, and returns only the signature — keeping the private key out of node process memory entirely. The node authenticates to Vault via a short-lived token (e.g. a Vault-injected AppRole credential) and calls the Transit sign endpoint at block authoring time. If Vault Transit is not available, the fallback is a one-time generation in a hardened ephemeral environment (air-gapped machine, dedicated secrets workstation), immediately sealing the result into Vault's KV store with no plaintext written to disk and shell history suppressed.

2. **Inject the new key into the running node's keystore** via the loopback-only RPC. No node restart is required, and no new node is needed — key rotation is an in-place operation on the same running node. In-place rotation is also preferred specifically to eliminate equivocation risk: if two nodes were running simultaneously with the same registered key during a migration window, both could attempt to author or finalize the same slot, which is a primary slashing condition in the Midnight/Cardano ecosystem. In-place rotation removes this possibility entirely. The mnemonic or private key bytes are passed only over the local loopback interface and held only in node memory:
   ```bash
   curl -s -X POST http://localhost:9944 \
     -H "Content-Type: application/json" \
     -d '{"id":1,"jsonrpc":"2.0","method":"author_insertKey","params":["aura","<mnemonic>","<public_key>"]}'
   ```

3. **Submit `setKeys` extrinsic** to register the new keys on-chain. Wait for the transaction to be finalized (included in a finalized block, not just pending).

4. **Confirm on-chain registration** — query the session keys associated with your stash account before proceeding.

5. **Keep the old key material active until the session boundary.** In Substrate-based chains, `setKeys` does not take effect immediately — the new keys become active at the start of the *next session* (epoch). The old key must remain valid in KMS / Vault and must continue signing until the chain has transitioned to the new session. Revoking it prematurely will cause missed blocks during the transition window.

   Monitor the current session index on-chain to determine when the transition has occurred, then revoke the old key and archive its public key for audit purposes.

> **Node migration:** If you are also replacing the node host (e.g. migrating to new hardware), rotate the keys first on the old node, confirm on-chain registration, then bring up the new node using the already-registered keys. Never generate a fresh key pair on the new host as part of a migration — treat key rotation and node replacement as two independent procedures.

### Risks and mitigations

| Risk | Mitigation |
|---|---|
| Key generated on the node — recoverable via memory forensics, swap, or shell history | Generate inside KMS or a hardened ephemeral environment; never run `key generate` on the production node |
| New keys registered but old node stopped before finalization | Wait for explicit on-chain confirmation before stopping old node |
| Rotation during high network load delays finalization | Schedule rotation during low-activity periods |
| Old key revoked before session boundary — missed blocks during transition | Keep old key active in KMS until on-chain session transition is confirmed |
| Conflating key rotation with node migration — new keys generated on the new host | Rotate keys first on the running node; treat migration as a separate step |

---

## 3. Incident Response — Suspected Key Exposure

**Scenario:** An operator reports they believe their signing key may have been exposed.

### First three actions (in order)

**Action 1 — Contain first, then replace**

**Revoke the compromised key immediately** — disable it in KMS (`aws kms disable-key`) or revoke the Vault token / Transit key. Do this before anything else. Once disabled, no further signing operations can use it, regardless of whether an attacker still has access to the credential. This cuts off the threat at the source.

Then, without delay, generate a new key and register it with the node so consensus participation resumes. Treat the key as compromised the moment there is *reasonable suspicion* — do not wait for confirmation. The cost of disabling and replacing an unexposed key is a brief gap in block authoring. The cost of leaving a compromised key active is unbounded.

**If the exposed key is `cross_chain` (ECDSA):** disabling it in KMS stops further signing on the Midnight side, but the key is also registered on Cardano for the partner chain committee. A Cardano-side transaction is needed to update that registration — coordinate with Midnight Foundation so both sides are updated before the exposure window closes.

**Action 2 — Audit the blast radius**

Once the new key is registered and the old key is inactive:
- Review CloudTrail / KMS audit logs for any signing operations that were not initiated by the node process — unexpected signing calls are evidence of active exploitation
- Check cross-chain transaction logs for any transactions signed with the old key that you did not authorize
- Review system access logs (`/var/log/auth.log`, SSH sessions, cloud IAM access) for the time window of suspected exposure to determine how the key may have been accessed
- **Check ZK-proof integrity:** Midnight's privacy model depends on zero-knowledge proofs submitted alongside transactions. A compromised signing key does not directly forge proofs (proof validity is enforced by the verifier circuit), but an attacker with node access may have attempted to submit or relay malformed proofs during the exposure window. Review transaction receipts and proof verification outcomes for the exposure period for any anomalous rejection rates or unexpected proof submissions attributed to your node.

**Action 3 — Notify the network coordinators**

Contact the Midnight Foundation / FNO coordination channel immediately, even if the investigation is inconclusive. This is not optional. If the key was used to sign malicious cross-chain transactions, other participants and validators need to know so they can assess whether those transactions should be challenged. Delayed disclosure compounds the damage. Provide:
- The old public key (so others can audit transactions signed by it)
- The approximate time window of suspected exposure
- The status of the rotation (completed / in progress)
