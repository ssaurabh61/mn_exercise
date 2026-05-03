# Midnight Network - DevOps Take-Home Assessment

**Role:** DevOps Engineer  
**Estimated time:** ~2 hours (not including Node synching time)  
**Submission:** GitHub repository (public or private; if private, invite the hiring team)

---

## Overview
This assessment is designed to evaluate your ability to operate, monitor, and automate infrastructure in the context of a real-world blockchain network. You will be working against Midnight's pre-production environment, which mirrors mainnet.

There are four sections. You do not need to complete every task perfectly—we care more about your approach, your documentation, and how you reason through problems than pixel-perfect output.

---

## Section 1 - Node Setup: Become an FNO on Pre-Prod
**Estimated:** ~40 mins

Midnight uses a set of Founding Node Operators (FNOs) to run the initial validator set. Your task is to simulate onboarding as an FNO on pre-prod.

**Important - read before starting:** Cardano DB Sync is a hard prerequisite for the Midnight node stack. It must be fully synchronized before you can proceed with the final stages of the Midnight-specific setup. Sync takes a minimum of 6 hours against pre-prod. Start this step immediately when you begin the assessment and leave it running overnight. Do not attempt the Midnight steps until DB Sync has completed.

### Tasks:
1. Follow the [Midnight Preprod setup documentation](https://www.notion.so/midnightfoundation/FNO-Preprod-Docs-3374057b9f2380258fb1d69ac7e55816) to spin up a node against the pre-prod network. Start Cardano DB Sync first—nothing else can proceed until it is fully synced.
2. Once DB Sync is complete, confirm your Midnight node is running and syncing by providing evidence of block progression (logs, screenshots, or CLI output committed to your repo).
3. Document the full setup steps—including DB Sync—in a `RUNBOOK.md` file in your repo. Write it as if you were handing it off to another engineer who knows DevOps but not Midnight specifically.

**What we're looking for:**
* Can you navigate unfamiliar documentation and get something running?
* Is your runbook clear, reproducible, and honest about any gotchas you hit?

---

## Section 2 - Monitoring & Alerting (Telemetry)
**Estimated:** ~35 mins

A node is only as reliable as your visibility into it.

### Tasks:
1. Set up monitoring for your pre-prod node. You may use any tooling you are comfortable with (Prometheus + Grafana, Datadog, a custom script - your call).
2. Define and implement at least three meaningful alerts. Examples to consider (not exhaustive):
    * Node stops producing/receiving blocks
    * Peer count drops below a threshold
    * Memory or CPU crosses a threshold
    * Process crash / service restart
3. Include your monitoring config, dashboard export, or alert definitions in a `/monitoring` directory in your repo.
4. In your `README.md`, briefly explain your alert design choices: why those three, and what the operational response to each would be.

**What we're looking for:**
* Signal-over-noise thinking: are your alerts actionable or just noisy?
* Evidence that you understand what "healthy" looks like for a validator node.

---

## Section 3 - Automation & Scripting
**Estimated:** ~25 mins

Pick one of the following automation tasks:

* **Option A: Key collection script**
  Write a script that, given a list of FNO operator identifiers (you can mock these), sends a structured request for their public keys and logs which operators have responded and which have not. Output should be machine-readable (JSON or CSV). The script should be re-runnable and idempotent.

* **Option B: Maintenance notification script**
  Write a script that generates and sends (or dry-run prints) a structured maintenance window notification to a list of node operators. The notification should include: window start/end time, expected impact, and a required acknowledgement flag. Operators who have not acknowledged within a configurable timeout should be flagged.

* **Option C: Node health checker**
  Write a script that polls your pre-prod node's RPC or metrics endpoint on a configurable interval, evaluates a set of health conditions, and writes a structured health report to disk. Include a mechanism to diff against the previous report and surface any regressions.

Place your script(s) in a `/scripts` directory. Include brief inline comments and detailed usage instructions in the `README.md`.

**What we're looking for:**
* Clean, readable code
* Error handling and edge cases considered
* Practical operational utility: would you actually use this?

---

## Section 4 - Security & Key Management
**Estimated:** ~20 mins

This is a written/design section (no implementation required). Midnight nodes require operators to register cryptographic keys as part of the governance process. These keys are sensitive: loss or compromise could have network-wide consequences.

Answer the following (in a `SECURITY.md` file in your repo):

1. **Key storage:** How would you store and protect a node operator's private keys in a production cloud environment? Walk through your recommended approach (consider HSM, KMS, secrets managers, and the tradeoffs of each).
2. **Key rotation:** Describe the process you would follow to rotate a registered node key with minimal disruption to network participation. What are the risks, and how do you mitigate them?
3. **Incident response:** An operator reports that they believe their signing key may have been exposed. What are your first three actions, and why?

Keep answers concise (bullet points or short paragraphs are fine). We're assessing your security intuition, not your essay writing.

---

## Repo Structure
We expect something roughly like this—adapt as needed:

| Path | Description |
| :--- | :--- |
| `README.md` | Overview, setup instructions, design notes |
| `RUNBOOK.md` | Section 1: FNO onboarding steps |
| `SECURITY.md` | Section 4: Key management answers |
| `monitoring/` | Section 2: configs, dashboards, alert definitions |
| `scripts/` | Section 3: automation script(s) |

---

## Submission
1. Push your repo to GitHub.
2. If private, share access with the hiring team (they will provide GitHub handles).
3. Include a top-level `README.md` that summarizes what you built, any assumptions you made, and anything you'd do differently with more time.

We will review your repo and use it as the basis for a follow-up technical conversation. You don't need to have everything perfect - just be ready to talk through your decisions. Good luck. If you hit a genuine blocker with pre-prod access or tooling, note it in your README and describe how you would have approached it—that's a valid response!
Midnight_DevOps_Take_Home.md
Displaying Midnight_DevOps_Take_Home.md.