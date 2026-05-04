# Security & Key Management — Midnight FNO

> Section 4 of the Midnight DevOps assessment.  
> FNO nodes have three registered key pairs: `aura` (sr25519, block authoring), `grandpa` (ed25519, finality), `cross_chain` (ecdsa, Cardano bridge). Compromise of any of them can disrupt consensus participation or expose signed cross-chain transactions.

---

## 1. Key Storage

The design goal is defence-in-depth: private key material never exists on the node host — it remains confined to KMS/Vault/Enclave boundaries and is never exposed to the node process. All signing operations are auditable, and keys are non-exportable wherever the provider supports it.

### Tooling by key type

| Key | Curve | Recommended storage |
|---|---|---|
| `aura` | sr25519 (Schnorrkel/Ristretto) | **Vault Transit Engine** or signing service in an **AWS Nitro Enclave** — cloud KMS has no native support for this curve |
| `grandpa` | ed25519 | **AWS KMS / GCP Cloud KMS** — native support |
| `cross_chain` | ecdsa (secp256k1) | **AWS KMS / GCP Cloud KMS** — native support |

**Cloud KMS** is the default for `grandpa` and `cross_chain`. Keys are born inside an HSM boundary and never exported; every signing call is logged in CloudTrail / Cloud Audit Logs. Access is locked to least-privilege IAM roles scoped to the node's instance identity, with KMS endpoints on private subnets only — no public exposure. Use **Multi-Region keys** (AWS MRK or GCP replication) so a regional outage doesn't block signing. KMS availability and latency must be monitored, as signing delays directly affect block production. One practical note: Substrate-based nodes don't natively call cloud KMS APIs — a thin signing adapter or sidecar is required to bridge the node to KMS/Vault. The adapter should run with least privilege, isolated from the node process (separate user or container), and expose only a minimal local interface.

**Vault Transit Engine** handles `aura` signing (sr25519, unsupported by cloud KMS) and also serves as the auxiliary secrets store for DB credentials and API tokens via dynamic secrets and agent injection. Vault must be HA and low-latency to the node — unavailability or high latency during block production directly impacts validator performance.

**AWS Nitro Enclaves** are the stronger option for `aura` when hardware attestation is required. The signing service runs in a memory-encrypted VM with no persistent storage and no SSH access; the key seed is decrypted only in enclave memory. KMS key policies can enforce that decryption is refused unless the enclave's PCR measurements match the known-good binary — host-level compromise can't subvert it. Like Vault, the enclave signer must be highly available; failure during block production causes missed slots.

**CloudHSM** is warranted for FIPS 140-2 Level 3 compliance in regulated environments. Overkill for most FNO operators.

### What not to use

- **Plaintext keystore files** — acceptable for preprod, not for mainnet
- **Environment variables in systemd units** — use `LoadCredential=` or Vault agent injection
- **Generating keys on the node** — seed material transiently exists in process memory, swap, and shell history

### Production layout

```
grandpa / cross_chain  → AWS KMS (non-exportable); node uses signing adapter
aura                   → Vault Transit or Nitro Enclave signing service (HA required)
Auxiliary secrets      → Vault dynamic secrets, agent-injected at startup
Backups                → KMS-managed keys: provider-handled, no manual export needed
                         Only externally generated keys (e.g., sr25519 seeds): GPG-encrypted, S3/Glacier, MFA + break-glass, with access logged and time-bound
```

---

## 2. Key Rotation

### Signing model — pick one and be explicit about it

**External signer (production):** The node holds no private key material. Signing is delegated to KMS / Vault Transit / Nitro Enclave via a signer service. `author_insertKey` is not used. The signer must be HA and low-latency — unavailability or latency spikes during block production directly impact validator performance.

**Local keystore (preprod / fallback):** Key material is injected into the node via `author_insertKey`, called over the loopback RPC only. The key exists in node memory for the duration of the process. The RPC port must be bound strictly to localhost with OS-level access controls — loopback trust assumes host integrity. This model is not recommended for mainnet.

### Procedure

1. **Generate inside KMS or Vault** — never on the node. For `grandpa`/`cross_chain` use `aws kms create-key`; for `aura` use Vault Transit or a Nitro Enclave. Only the public key leaves this boundary.

2. **Validate before touching chain state.** Perform a test signature and confirm it verifies against the expected public key. For an external signer, verify API reachability and that signing latency is within block production thresholds. Registering a broken key risks missed blocks with no safe rollback.

3. **Register the new key with the node.** In the external signer model, update the signer endpoint config. In the local keystore model, call `author_insertKey` over loopback. No restart required in either case. Ensure only one active signing key is in use at any point — having two registered simultaneously is a slashing condition.

4. **Submit `setKeys` on-chain and wait for finalization** — not just inclusion. Query the session index before and after; confirm the new keys are reflected on-chain for your stash account.

5. **Wait for the session boundary.** New keys take effect at the start of the next epoch, not immediately. Keep the old key active in KMS/Vault until the transition is confirmed on-chain, then revoke it. If the rotation fails at any point before session transition, the old key is still valid and the node continues without downtime — safe rollback is automatic as long as the old key hasn't been prematurely revoked.

6. **Verify post-rotation health.** Confirm the validator is producing blocks, check for missed slots or equivocation events, and (for external signers) monitor signer latency.

> **Node migration:** Key rotation and host migration must be treated as independent operations. Rotate on the old node first, confirm on-chain, then bring up the new host with the already-registered keys. Running two nodes simultaneously with the same active key is a slashing condition.

This procedure delivers zero-downtime rotation while preventing equivocation and maintaining strict key isolation throughout.

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
