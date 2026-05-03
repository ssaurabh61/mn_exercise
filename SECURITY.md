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

**AWS CloudHSM / Azure Dedicated HSM (if FIPS 140-2 Level 3 is required)**
- Key material is truly single-tenant and never shared with the cloud provider
- Required for regulatory compliance (financial services, government)
- Significantly more expensive (~$1.5k/month) and operationally complex
- Tradeoff: overkill for most FNO operators; adds operational burden without meaningful security improvement if the threat model is external compromise (not insider/cloud-provider threat)

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
  → Vault agent injects at startup via systemd LoadCredential=

Key backups (disaster recovery only)
  → Encrypted with GPG, stored in S3 with Glacier lifecycle policy
  → Access requires MFA + break-glass procedure with audit log
```

---

## 2. Key Rotation

Midnight session keys are registered on-chain. Rotation must be done carefully to avoid a gap in consensus participation or a double-signing window.

### Procedure

1. **Generate new key pair** on the new/replacement node (never reuse or copy private keys)
   ```bash
   midnight-node key generate --scheme sr25519 --output-type json
   ```

2. **Insert new keys into the keystore** of the replacement node while the old node is still running and registered
   ```bash
   curl -s -X POST http://localhost:9944 \
     -H "Content-Type: application/json" \
     -d '{"id":1,"jsonrpc":"2.0","method":"author_insertKey","params":["aura","<mnemonic>","<public_key>"]}'
   ```

3. **Submit `setKeys` extrinsic** to register the new keys on-chain. Wait for the transaction to be finalized (included in a finalized block, not just pending).

4. **Confirm on-chain registration** — query the session keys associated with your stash account before proceeding.

5. **Decommission the old node** only after the new keys are confirmed on-chain. There is a brief overlap window where both sets of keys are technically valid — this is expected and safe.

6. **Revoke old key material** from KMS / Vault and archive the backup.

### Risks and mitigations

| Risk | Mitigation |
|---|---|
| New keys registered but old node stopped before finalization | Wait for explicit on-chain confirmation before stopping old node |
| Rotation during high network load delays finalization | Schedule rotation during low-activity periods |
| Double-signing if both nodes run simultaneously with different keys | Managed by the overlap design — keys rotate on-chain atomically; old node's keys become invalid after `setKeys` finalizes |
| Backup of new keys not created before decommissioning old infra | Rotation checklist: backup new keys before any decommission step |

---

## 3. Incident Response — Suspected Key Exposure

**Scenario:** An operator reports they believe their signing key may have been exposed.

### First three actions (in order)

**Action 1 — Rotate immediately, before investigating**

Do not wait for confirmation. Generate a new key pair and submit `setKeys` to register it on-chain. The cost of rotating an unexposed key is low (downtime measured in minutes). The cost of leaving a compromised key active is unbounded (an attacker with the key can sign arbitrary cross-chain transactions or equivocate in consensus, potentially resulting in slashing or network disruption).

Key material should be treated as compromised the moment there is *reasonable suspicion*, not after confirmation.

**Action 2 — Audit the blast radius**

Once the new key is registered and the old key is inactive:
- Review CloudTrail / KMS audit logs for any signing operations that were not initiated by the node process — unexpected signing calls are evidence of active exploitation
- Check cross-chain transaction logs for any transactions signed with the old key that you did not authorize
- Review system access logs (`/var/log/auth.log`, SSH sessions, cloud IAM access) for the time window of suspected exposure to determine how the key may have been accessed

**Action 3 — Notify the network coordinators**

Contact the Midnight Foundation / FNO coordination channel immediately, even if the investigation is inconclusive. This is not optional. If the key was used to sign malicious cross-chain transactions, other participants and validators need to know so they can assess whether those transactions should be challenged. Delayed disclosure compounds the damage. Provide:
- The old public key (so others can audit transactions signed by it)
- The approximate time window of suspected exposure
- The status of the rotation (completed / in progress)
