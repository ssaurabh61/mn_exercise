#!/usr/bin/env python3
"""
fno_key_collection.py — Collect public keys from FNO operators.

Reads a list of operators from operators.json, requests each one's public key
from their configured endpoint, and writes a machine-readable report. Safe to
re-run: operators whose keys were already collected are skipped.

Usage:
    python3 scripts/fno_key_collection.py [options]

    --operators FILE    Operator list (default: scripts/operators.json)
    --state FILE        Persistent state across runs (default: reports/key_collection_state.json)
    --output-dir DIR    Where to write JSON + CSV reports (default: reports/)
    --timeout SECS      HTTP request timeout (default: 10)
    --retry             Re-request keys even from operators who already responded

Exit codes:
    0 — all operators responded
    1 — one or more operators still pending
"""

import argparse
import csv
import json
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def load_json(path: Path) -> dict | list:
    with open(path) as f:
        return json.load(f)


def save_json(path: Path, data: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_state(path: Path) -> dict:
    """Load persisted state; return empty dict if it doesn't exist yet."""
    if path.exists():
        return load_json(path)
    return {}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def fetch_key(endpoint: str, timeout: int) -> tuple[str, str]:
    """
    GET the operator's key endpoint.
    Returns (public_key, error). One of them will be an empty string.
    """
    try:
        with urllib.request.urlopen(endpoint, timeout=timeout) as resp:
            body = json.loads(resp.read())
        key = body.get("public_key", "")
        if not key:
            return "", "response missing 'public_key' field"
        return key, ""
    except urllib.error.HTTPError as e:
        return "", f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return "", str(e.reason)
    except (json.JSONDecodeError, Exception) as e:  # noqa: BLE001
        return "", f"parse error: {e}"


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def collect_key(operator: dict, state: dict, timeout: int) -> dict:
    """
    Attempt to collect the key for one operator.
    Returns a result dict and updates state in place.
    """
    op_id = operator["id"]

    if op_id in state:
        # Already have their key — skip
        return {**state[op_id], "status": "already_collected"}

    key, error = fetch_key(operator["key_endpoint"], timeout)
    now = datetime.now(timezone.utc).isoformat()

    if key:
        entry = {"id": op_id, "name": operator["name"], "public_key": key, "collected_at": now, "status": "collected"}
        state[op_id] = {k: v for k, v in entry.items() if k != "status"}
    else:
        entry = {"id": op_id, "name": operator["name"], "public_key": "", "error": error, "collected_at": now, "status": "pending"}

    return entry


def run_collection(operators: list, state: dict, timeout: int, retry: bool) -> list:
    """Collect keys for all operators. Returns list of result dicts."""
    if retry:
        state.clear()
    return [collect_key(op, state, timeout) for op in operators]


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def write_json_report(results: list, path: Path) -> None:
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(results),
        "collected": sum(1 for r in results if r["status"] in ("collected", "already_collected")),
        "pending": sum(1 for r in results if r["status"] == "pending"),
        "results": results,
    }
    save_json(path, summary)
    print(f"JSON report: {path}")


def write_csv_report(results: list, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["id", "name", "status", "public_key", "error", "collected_at"]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)
    print(f"CSV report:  {path}")


def print_summary(results: list) -> None:
    collected = [r for r in results if r["status"] in ("collected", "already_collected")]
    pending = [r for r in results if r["status"] == "pending"]
    print(f"\n{len(collected)}/{len(results)} operators responded\n")
    for r in collected:
        tag = "(cached)" if r["status"] == "already_collected" else ""
        print(f"  [+] {r['id']:15s} {r['public_key'][:24]}...  {tag}")
    for r in pending:
        print(f"  [-] {r['id']:15s} no key — {r.get('error', 'unknown')}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    here = Path(__file__).parent
    parser = argparse.ArgumentParser(description="Collect FNO public keys")
    parser.add_argument("--operators", default=str(here / "operators.json"))
    parser.add_argument("--state", default=str(here.parent / "reports" / "key_collection_state.json"))
    parser.add_argument("--output-dir", default=str(here.parent / "reports"))
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument("--retry", action="store_true", help="Re-request even already-collected keys")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    state_path = Path(args.state)
    output_dir = Path(args.output_dir)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    operators = load_json(Path(args.operators))["operators"]
    state = load_state(state_path)

    results = run_collection(operators, state, args.timeout, args.retry)

    save_json(state_path, state)

    write_json_report(results, output_dir / f"key_collection_{ts}.json")
    write_csv_report(results, output_dir / f"key_collection_{ts}.csv")
    print_summary(results)

    pending = [r for r in results if r["status"] == "pending"]
    return 0 if not pending else 1


if __name__ == "__main__":
    sys.exit(main())
