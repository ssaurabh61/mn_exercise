# cardano-db-sync Verification

Captured on **2026-05-04** before proceeding to Midnight node installation (Phase 8).

## Latest block indexed

```
 block_no |  slot_no  |        time
----------+-----------+---------------------
  4671291 | 122189878 | 2026-05-04 05:37:58
(1 row)
```

## Sync percentage

```
    sync_percent
---------------------
 99.9999732821075104
(1 row)
```

**Verdict: db-sync is fully synced.** The remaining 0.0000267% is the few blocks produced in the seconds between the query and now — this is the expected steady-state for a live node.
