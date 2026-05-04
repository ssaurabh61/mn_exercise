# Security & Key Management — Midnight FNO

> Section 4 of the Midnight DevOps assessment.  
> FNO nodes have three registered key pairs: `aura` (sr25519, block authoring), `grandpa` (ed25519, finality), `cross_chain` (ecdsa, Cardano bridge). Compromise of any of them can disrupt consensus participation or expose signed cross-chain transactions.

---

## 1. Key Storage

The core principle is that private key material should never touch the node host in plaintext. Signing happens in a remote service; the node receives only the signature back.

### What to use and why

| Key | Curve | Recommended storage |
|---|---|---|
| `aura` | sr25519 (Schnorrkel/Ristretto) | **Vault Transit Engine** or a custom signing service in an **AWS Nitro Enclave** — cloud KMS has no native support for this curve |
| `grandpa` | ed25519 | **AWS KMS / GCP Cloud KMS** — both support ed25519 natively |
| `cross_chain` | ecdsa (secp256k1) | **AWS KMS / GCP Cloud KMS** — both support secp256k1 natively |

**Cloud KMS** (AWS KMS, GCP Cloud KMS) is the right default for `grandpa` and `cross_chain`. The private key is born inside an HSM, never exported, and every signing call is logged in CloudTrail / Cloud Audit Logs. The ~10–50 ms per-call latency is fine for Midnight's slot timescales. Use **Multi-Region keys** (AWS MRK or GCP replication) so a regional outage doesn't block signing — the replica in a secondary region (e.g. London → Frankfurt) is immediately usable.

**Vault Transit Engine** fills the gap for `aura` where KMS has no curve support. Vault generates and holds the sr25519 key internally and returns only the signature via its API. The node authenticates with a short-lived AppRole credential. Vault also handles auxiliary secrets (DB passwords, API tokens) well — dynamic secrets, agent injection at startup, no secrets on disk.

**AWS Nitro Enclaves** are the stronger alternative for `aura` if you want hardware-attested isolation rather than relying on Vault's software boundary. An enclave is a memory-encrypted VM inside your EC2 instance with no persistent storage, no SSH access, and only a local vsock channel. You run your sr25519 signing service inside it; the key seed is decrypted only in enclave memory. KMS can be configured to refuse decryption unless the enclave's PCR measurements match your known-good signing binary — so even if an attacker compromises the host, they can't swap in a different binary and get the key decrypted.

**CloudHSM** is available if compliance requires FIPS 140-2 Level 3 single-tenant hardware. Expensive and operationally heavy — warranted for financial services / government contexts, overkill for most FNO operators.

### What not to use

- **Plaintext keystore files on disk** — fine for preprod, not for mainnet
- **Environment variables in systemd unit files** — visible to co-tenant processes; use `LoadCredential=` or Vault agent injection instead
- **Generating keys on the node** — the private key transiently exists in process memory, shell history, and potentially swap, all of which survive process termination and can be recovered forensically

---

## 2. Key Rotation

Session keys are registered on-chain. Three things matter: don't generate on the node, don't run two nodes with the same key simultaneously, and don't revoke the old key before the session boundary.

### Procedure

1. **Generate the new key inside KMS or Vault** — not on the node. For `grandpa`/`cross_chain` use `aws kms create-key`; for `aura` use Vault's Transit API or generate inside a Nitro Enclave. The public key is the only thing that leaves this boundary.

2. **Register the new signing backend with the running node.** In a remote-signer setup the node never holds the private key at all — you update the Vault/KMS/enclave endpoint configuration and the node routes signing calls there automatically. The `author_insertKey` RPC (which passes a mnemonic to the node's local keystore) is a preprod/dev pattern; in production with a remote signer it is not used, because there is no local key to inject.

3. **Submit `setKeys` on-chain** and wait for finalization — not just inclusion.

4. **Confirm on-chain** that the new keys are associated with your stash account before proceeding.

5. **Wait for the session boundary.** `setKeys` takes effect at the start of the *next* session/epoch. Keep the old key active in KMS/Vault until the chain has transitioned — revoking early causes missed blocks.

Prefer in-place rotation (same node, new key) over migrating hosts at the same time. Two nodes running simultaneously with the same registered key is a slashing condition.

> **Migrating hosts:** Rotate keys first on the old node, confirm on-chain, then bring up the new host already using the registered keys. Treat key rotation and host migration as separate operations.

### What can go wrong

| Risk | Mitigation |
|---|---|
| Old node stopped before `setKeys` is finalized | Wait for explicit on-chain confirmation first |
| Old key revoked before session boundary — missed blocks | Keep it active in KMS/Vault until session transition is confirmed |
| Two nodes running with the same key — equivocation (slashable) | In-place rotation only; never spin up a second node with the active key |
| Key generated on the node — recoverable from memory/swap/history | Generate only inside KMS, Vault, or Nitro Enclave |

---

## 3. Incident Response — Suspected Key Compromise

**Rotate first, investigate after.** The cost of rotating an unexposed key is a few minutes of partial downtime. The cost of leaving a compromised key active is unbounded.

### Action 1 — Rotate immediately

Generate a new key in KMS/Vault and submit `setKeys`. Treat the key as compromised the moment there is reasonable suspicion, not after confirmation.

One caveat for `cross_chain`: that key signs messages anchored to Cardano, so rotation requires updating the partner chain committee registration on the Cardano side as well — not just a `setKeys` on the Midnight side. Coordinate with Midnight Foundation to ensure both sides reflect the new key before revoking the old one.

### Action 2 — Audit the blast radius

Once the new key is live and the old one is inactive:

- **KMS/CloudTrail logs:** look for signing calls not initiated by your node process. Unexpected events are evidence of active exploitation.
- **Cross-chain transactions:** check for transactions signed with the old key that you didn't authorise.
- **System access:** review SSH sessions, IAM logs, `/var/log/auth.log` for the suspected exposure window to understand how the key may have been accessed.
- **ZK-proof anomalies:** a compromised signing key doesn't let an attacker forge ZK proofs (validity is enforced by the verifier circuit), but node-level access could be used to relay malformed proofs. Check verification outcomes for the exposure period for anomalous rejection rates.

### Action 3 — Notify the network coordinators

Contact the Midnight Foundation / FNO coordination channel immediately, even if the investigation is still inconclusive. Share the old public key (so others can audit transactions signed by it), the suspected exposure window, and the rotation status. Delayed disclosure compounds the damage.
