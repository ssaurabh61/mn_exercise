#!/usr/bin/env python3
"""
fno_key_collection.py — Collect public keys from FNO operators.

Reads a list of operators from operators.json, requests each one's public key
from their configured endpoint, and writes a machine-readable report. Safe to
re-run: operators whose keys were already collected are skipped.

Usage:
    python3 fno_key_collection.py [options]

    --operators FILE    Operator list (default: operators.json next to this script)
    --state FILE        Persistent state across runs (default: reports/key_collection_state.json)
    --output-dir DIR    Where to write JSON + CSV reports (default: reports/)
    --timeout SECS      HTTP request timeout per operator (default: 10)
    --attempts N        Per-operator retry attempts on transient failures (default: 3)
    --retry             Re-request keys even from operators who already responded
    --operator ID       Only collect from this operator ID (repeatable)
    --dry-run           Print what would happen without making any HTTP requests

Exit codes:
    0 — all operators responded
    1 — one or more operators still pending
    2 — fatal error (bad config, unreadable file, etc.)
"""

import argparse
import csv
import json
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# Maximum response body size accepted from any endpoint (1 MB).
# Prevents a misbehaving or malicious endpoint from exhausting memory.
MAX_RESPONSE_BYTES = 1 * 1024 * 1024

REQUIRED_OPERATOR_FIELDS = {"id", "name", "key_endpoint"}


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


def load_operators(path: Path) -> list[dict]:
    """
    Load and validate the operators list.
    Exits with code 2 on any structural problem so the caller gets a clear message
    rather than a confusing KeyError or AttributeError deep in the stack.
    """
    if not path.exists():
        _fatal(f"Operators file not found: {path}")

    try:
        data = load_json(path)
    except json.JSONDecodeError as e:
        _fatal(f"Operators file is not valid JSON ({path}): {e}")

    if not isinstance(data, dict) or "operators" not in data:
        _fatal(f"Operators file must be a JSON object with an 'operators' array: {path}")

    operators = data["operators"]
    if not isinstance(operators, list) or len(operators) == 0:
        _fatal(f"'operators' must be a non-empty array in {path}")

    for i, op in enumerate(operators):
        missing = REQUIRED_OPERATOR_FIELDS - set(op.keys())
        if missing:
            _fatal(f"Operator at index {i} is missing required fields: {missing}")
        if not isinstance(op["id"], str) or not op["id"].strip():
            _fatal(f"Operator at index {i} has an empty or non-string 'id'")
        _validate_url(op["key_endpoint"], context=f"operator '{op['id']}'")

    return operators


def load_state(path: Path) -> dict:
    """
    Load persisted state. Returns an empty dict if the file doesn't exist yet.
    If the file exists but is corrupt, renames it and starts fresh rather than
    crashing — losing cached progress is better than blocking the whole run.
    """
    if not path.exists():
        return {}

    try:
        state = load_json(path)
        if not isinstance(state, dict):
            raise ValueError("state file must be a JSON object")
        return state
    except (json.JSONDecodeError, ValueError) as e:
        backup = path.with_suffix(".corrupt.json")
        path.rename(backup)
        print(f"[warn] State file was corrupt ({e}); renamed to {backup} and starting fresh.")
        return {}


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_url(url: str, context: str = "") -> None:
    """Exit with code 2 if the URL is not a valid http/https address."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"scheme must be http or https, got '{parsed.scheme}'")
        if not parsed.netloc:
            raise ValueError("missing host")
    except ValueError as e:
        prefix = f" (for {context})" if context else ""
        _fatal(f"Invalid endpoint URL{prefix} '{url}': {e}")


def _fatal(message: str) -> None:
    print(f"[error] {message}", file=sys.stderr)
    sys.exit(2)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _fetch_once(endpoint: str, timeout: int) -> tuple[str, str, bool]:
    """
    Single HTTP GET attempt.
    Returns (public_key, error_message, retryable).
    retryable=True for transient failures (timeouts, connection errors, 5xx).
    retryable=False for deterministic failures (4xx, bad JSON, missing field).
    """
    try:
        req = urllib.request.Request(endpoint, headers={"User-Agent": "fno-key-collection/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES + 1)

        if len(raw) > MAX_RESPONSE_BYTES:
            return "", f"response too large (> {MAX_RESPONSE_BYTES // 1024} KB)", False

        try:
            body = json.loads(raw)
        except json.JSONDecodeError as e:
            return "", f"response is not valid JSON: {e}", False

        key = body.get("public_key")
        if key is None:
            return "", "response missing 'public_key' field", False
        if not isinstance(key, str) or not key.strip():
            return "", f"'public_key' must be a non-empty string, got: {type(key).__name__}", False

        return key.strip(), "", False

    except urllib.error.HTTPError as e:
        # 5xx = server-side / transient; 4xx = client error, won't change on retry
        retryable = e.code >= 500
        return "", f"HTTP {e.code} {e.reason}", retryable
    except urllib.error.URLError as e:
        reason = str(e.reason)
        if "timed out" in reason.lower():
            return "", f"connection timed out after {timeout}s", True
        if "connection refused" in reason.lower():
            return "", "connection refused — endpoint may be down", True
        if "ssl" in reason.lower() or "certificate" in reason.lower():
            return "", f"SSL error: {reason}", False
        return "", f"network error: {reason}", True
    except OSError as e:
        return "", f"OS error: {e}", True


def fetch_key(endpoint: str, timeout: int, attempts: int) -> tuple[str, str]:
    """
    GET the operator's key endpoint, retrying on transient failures.
    Returns (public_key, error_message). Exactly one will be a non-empty string.

    Uses exponential backoff between attempts (1s, 2s, 4s, ...).
    Deterministic failures (4xx, malformed response) are not retried.
    """
    last_error = ""
    for attempt in range(1, attempts + 1):
        key, error, retryable = _fetch_once(endpoint, timeout)
        if key:
            return key, ""
        last_error = error
        if not retryable or attempt == attempts:
            break
        delay = 2 ** (attempt - 1)  # 1s, 2s, 4s ...
        print(f"\n      attempt {attempt}/{attempts} failed ({error}) — retrying in {delay}s ...", end=" ", flush=True)
        time.sleep(delay)
    return "", last_error


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def collect_key(operator: dict, state: dict, timeout: int, attempts: int, dry_run: bool) -> dict:
    """
    Attempt to collect the key for one operator.
    Returns a result dict and updates state in place.
    """
    op_id = operator["id"]
    now = datetime.now(timezone.utc).isoformat()

    if op_id in state:
        print(f"  [=] {op_id:20s} skipped (already collected)")
        return {**state[op_id], "status": "already_collected"}

    if dry_run:
        print(f"  [?] {op_id:20s} would request {operator['key_endpoint']}")
        return {
            "id": op_id,
            "name": operator["name"],
            "public_key": "",
            "collected_at": now,
            "status": "dry_run",
        }

    print(f"  [ ] {op_id:20s} contacting {operator['key_endpoint']} ...", end=" ", flush=True)
    key, error = fetch_key(operator["key_endpoint"], timeout, attempts)

    if key:
        print("OK")
        entry = {
            "id": op_id,
            "name": operator["name"],
            "public_key": key,
            "collected_at": now,
            "status": "collected",
        }
        # Persist to state (without the transient 'status' field)
        state[op_id] = {k: v for k, v in entry.items() if k != "status"}
    else:
        print(f"FAILED ({error})")
        entry = {
            "id": op_id,
            "name": operator["name"],
            "public_key": "",
            "error": error,
            "collected_at": now,
            "status": "pending",
        }

    return entry


def run_collection(
    operators: list,
    state: dict,
    timeout: int,
    attempts: int,
    retry: bool,
    dry_run: bool,
    filter_ids: set[str] | None,
) -> list:
    """Collect keys for all (or selected) operators. Returns list of result dicts."""
    if retry:
        if filter_ids:
            # Only clear state for the targeted operators
            for op_id in filter_ids:
                state.pop(op_id, None)
        else:
            state.clear()

    results = []
    for op in operators:
        if filter_ids and op["id"] not in filter_ids:
            continue
        results.append(collect_key(op, state, timeout, attempts, dry_run))

    return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def write_json_report(results: list, path: Path) -> None:
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(results),
        "collected": sum(1 for r in results if r["status"] in ("collected", "already_collected")),
        "pending": sum(1 for r in results if r["status"] == "pending"),
        "results": results,
    }
    save_json(path, report)
    print(f"\nJSON report: {path}")


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
    pending   = [r for r in results if r["status"] == "pending"]
    dry_run   = [r for r in results if r["status"] == "dry_run"]

    print(f"\n--- Summary ---")
    if dry_run:
        print(f"Dry run — {len(dry_run)} operator(s) would be contacted, no requests made.")
        return

    print(f"{len(collected)}/{len(results)} operators responded")

    if collected:
        print("\nCollected:")
        for r in collected:
            tag = " (cached)" if r["status"] == "already_collected" else ""
            key_preview = r["public_key"][:32] + "..." if len(r["public_key"]) > 32 else r["public_key"]
            print(f"  [+] {r['id']:20s} {key_preview}{tag}")

    if pending:
        print("\nPending (no key received):")
        for r in pending:
            print(f"  [-] {r['id']:20s} {r.get('error', 'unknown error')}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    here = Path(__file__).parent
    parser = argparse.ArgumentParser(
        description="Collect public keys from FNO operators.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--operators",
        default=str(here / "operators.json"),
        metavar="FILE",
        help="Path to operators JSON file (default: operators.json next to this script)",
    )
    parser.add_argument(
        "--state",
        default=str(here.parent / "reports" / "key_collection_state.json"),
        metavar="FILE",
        help="Persistent state file — tracks already-collected keys across runs",
    )
    parser.add_argument(
        "--output-dir",
        default=str(here.parent / "reports"),
        metavar="DIR",
        help="Directory for timestamped JSON + CSV reports",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        metavar="SECS",
        help="HTTP request timeout per operator in seconds (default: 10)",
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=3,
        metavar="N",
        help="Max retry attempts per operator on transient failures (default: 3, min: 1)",
    )
    parser.add_argument(
        "--retry",
        action="store_true",
        help="Re-request keys even from operators who already responded",
    )
    parser.add_argument(
        "--operator",
        action="append",
        dest="operators_filter",
        metavar="ID",
        help="Only collect from this operator ID (can be repeated for multiple)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen without making any HTTP requests",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    state_path = Path(args.state)
    output_dir = Path(args.output_dir)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filter_ids = set(args.operators_filter) if args.operators_filter else None

    operators = load_operators(Path(args.operators))

    # Warn early if any --operator IDs don't exist in the file
    if filter_ids:
        known_ids = {op["id"] for op in operators}
        unknown = filter_ids - known_ids
        if unknown:
            print(f"[warn] Unknown operator ID(s) specified: {', '.join(sorted(unknown))}", file=sys.stderr)

    state = load_state(state_path)

    if args.dry_run:
        print(f"[dry-run] No requests will be made.\n")

    attempts = max(1, args.attempts)  # guard against --attempts 0
    print(f"Collecting keys from {len(operators)} operator(s) (up to {attempts} attempt(s) each):\n")
    results = run_collection(operators, state, args.timeout, attempts, args.retry, args.dry_run, filter_ids)

    if not args.dry_run:
        save_json(state_path, state)
        write_json_report(results, output_dir / f"key_collection_{ts}.json")
        write_csv_report(results, output_dir / f"key_collection_{ts}.csv")

    print_summary(results)

    pending = [r for r in results if r["status"] == "pending"]
    return 0 if not pending else 1


if __name__ == "__main__":
    sys.exit(main())
