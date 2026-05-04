#!/usr/bin/env python3
"""
node_health_check.py — Midnight FNO node health checker

Polls health endpoints for all FNO services, evaluates health conditions,
writes a timestamped JSON report, diffs against the previous run to surface
regressions, and exits non-zero if anything is unhealthy.

Supports two scrape types (configured per-service in services.json):
  prometheus    — HTTP GET /metrics, parses Prometheus text exposition format
  substrate_rpc — HTTP JSON-RPC POST (system_health, chain_getHeader, etc.)
                  works with any Substrate/Midnight node without needing
                  a separate Prometheus exporter

Usage:
    python3 node_health_check.py [options]

    --services FILE     Path to services.json (default: services.json next to this script)
    --report-dir DIR    Where to write JSON reports (default: ../reports/)
    --timeout SECS      Per-scrape HTTP timeout in seconds (default: 5)
    --interval SECS     Poll continuously on this interval; omit for single run
    --quiet             Suppress output when all services are healthy (for cron)

Exit codes:
    0 — all services healthy
    1 — one or more services unhealthy or unreachable
    2 — bad arguments
"""

import argparse
import json
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Service config loader
# ---------------------------------------------------------------------------

_DEFAULT_SERVICES_FILE = Path(__file__).parent / "services.json"


def load_services(path: Path) -> dict:
    """
    Load service definitions from a JSON file.
    Exits with code 2 on any file or parse error.

    The file must be a JSON object whose keys are service names and whose
    values follow the schema used in the default services.json:
      { "url": str, "track_metrics": [...], "checks": [...] }
    """
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"[error] services file not found: {path}", file=sys.stderr)
        sys.exit(2)
    except json.JSONDecodeError as exc:
        print(f"[error] invalid JSON in {path}: {exc}", file=sys.stderr)
        sys.exit(2)
    except OSError as exc:
        print(f"[error] could not read {path}: {exc}", file=sys.stderr)
        sys.exit(2)

    if not isinstance(data, dict) or not data:
        print(f"[error] {path} must be a non-empty JSON object", file=sys.stderr)
        sys.exit(2)

    return data


# Comparison operators used by threshold / derived_diff checks
_OPS = {
    ">=": lambda a, b: a >= b,
    ">":  lambda a, b: a > b,
    "==": lambda a, b: a == b,
    "<":  lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
}


# ---------------------------------------------------------------------------
# Prometheus text format parser
# ---------------------------------------------------------------------------

def parse_prometheus_metrics(text: str) -> dict[str, float]:
    """
    Parse Prometheus text exposition format into {metric_key: value}.

    Keys are either bare metric names ('metric_name') or include their label
    set ('metric_name{label="value"}'), so labeled metrics such as
    substrate_block_height{status="best"} can be looked up directly by the
    check definitions above.

    For the same bare metric name with multiple label sets, all variants are
    stored. The bare name entry holds the first value encountered.

    Handles the optional timestamp column correctly — the value is always the
    first whitespace-separated token after the name+labels part.
    """
    metrics: dict[str, float] = {}

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # Separate the name+labels part from "value [optional_timestamp]"
        if "{" in line:
            brace_end = line.index("}")
            full_key  = line[:brace_end + 1]      # e.g. metric{label="v"}
            bare_name = line[:line.index("{")]     # e.g. metric
            rest      = line[brace_end + 1:].strip()
        else:
            tokens = line.split(None, 1)
            if len(tokens) < 2:
                continue
            full_key = bare_name = tokens[0]
            rest = tokens[1].strip()

        # rest is "value [timestamp]" — value is always the first token
        value_str = rest.split()[0] if rest else ""
        try:
            value = float(value_str)
        except ValueError:
            continue

        metrics[full_key] = value
        # Bare name: first label-set variant wins (avoids clobbering with a
        # different label variant for the same metric family)
        if bare_name not in metrics:
            metrics[bare_name] = value

    return metrics


# ---------------------------------------------------------------------------
# Scrape
# ---------------------------------------------------------------------------

def _scrape_prometheus(url: str, timeout: int) -> tuple[bool, dict[str, float], str]:
    """Fetch a Prometheus /metrics URL and parse the text format."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        return True, parse_prometheus_metrics(body), ""
    except urllib.error.URLError as exc:
        return False, {}, f"network error: {exc.reason}"
    except OSError as exc:
        return False, {}, f"OS error: {exc}"


def _scrape_substrate_rpc(url: str, timeout: int) -> tuple[bool, dict[str, float], str]:
    """
    Scrape a Substrate/Midnight JSON-RPC endpoint (HTTP POST).

    Makes four RPC calls and maps results to synthetic metric keys:
      rpc.peers           — number of connected peers
      rpc.is_syncing      — 1.0 if actively syncing, 0.0 if caught up
      rpc.best_block      — best (latest) block number
      rpc.finalized_block — latest finalized block number

    These names are used in services.json check and track_metrics definitions.
    Port 9944 is the default Substrate RPC port — available whenever the node
    is running, regardless of whether Prometheus is configured.
    """
    def _call(method: str, params: list | None = None):
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
        ).encode()
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        if "error" in data:
            raise ValueError(f"{method} returned RPC error: {data['error']}")
        return data["result"]

    try:
        health   = _call("system_health")
        best_hdr = _call("chain_getHeader")
        fin_hash = _call("chain_getFinalizedHead")
        fin_hdr  = _call("chain_getHeader", [fin_hash])
    except urllib.error.URLError as exc:
        return False, {}, f"network error: {exc.reason}"
    except (json.JSONDecodeError, ValueError) as exc:
        return False, {}, f"RPC response error: {exc}"
    except OSError as exc:
        return False, {}, f"OS error: {exc}"

    try:
        metrics = {
            "rpc.peers":           float(health["peers"]),
            "rpc.is_syncing":      1.0 if health["isSyncing"] else 0.0,
            "rpc.best_block":      float(int(best_hdr["number"], 16)),
            "rpc.finalized_block": float(int(fin_hdr["number"], 16)),
        }
    except (KeyError, ValueError) as exc:
        return False, {}, f"unexpected RPC response shape: {exc}"

    return True, metrics, ""


def scrape(url: str, timeout: int, scrape_type: str = "prometheus") -> tuple[bool, dict[str, float], str]:
    """
    Dispatch to the appropriate scraper based on scrape_type.
    Returns (success, parsed_metrics, error_message).
    """
    if scrape_type == "substrate_rpc":
        return _scrape_substrate_rpc(url, timeout)
    return _scrape_prometheus(url, timeout)


# ---------------------------------------------------------------------------
# Check evaluation
# ---------------------------------------------------------------------------

def _evaluate_check(check: dict, up: bool, metrics: dict[str, float]) -> dict:
    """Evaluate a single check definition. Returns a check result dict."""
    name        = check["name"]
    description = check["description"]
    check_type  = check["type"]

    if check_type == "service_up":
        return {
            "name": name, "description": description, "passed": up,
            "detail": "endpoint reachable" if up else "endpoint unreachable",
        }

    if not up:
        return {
            "name": name, "description": description, "passed": False,
            "detail": "skipped — endpoint unreachable",
        }

    if check_type == "threshold":
        key   = check["metric"]
        value = metrics.get(key)
        if value is None:
            return {
                "name": name, "description": description, "passed": False,
                "detail": f"metric '{key}' not present in response",
            }
        op_fn  = _OPS.get(check["op"])
        passed = bool(op_fn(value, check["threshold"])) if op_fn else False
        detail = f"{value} {check['op']} {check['threshold']} → {'pass' if passed else 'FAIL'}"
        return {"name": name, "description": description, "passed": passed, "detail": detail}

    if check_type == "derived_diff":
        val_a = metrics.get(check["metric_a"])
        val_b = metrics.get(check["metric_b"])
        if val_a is None or val_b is None:
            missing = [k for k, v in {check["metric_a"]: val_a, check["metric_b"]: val_b}.items() if v is None]
            return {
                "name": name, "description": description, "passed": False,
                "detail": f"metric(s) not present: {missing}",
            }
        diff   = val_a - val_b
        op_fn  = _OPS.get(check["op"])
        passed = bool(op_fn(diff, check["threshold"])) if op_fn else False
        detail = (
            f"lag={diff:.0f} ({val_a:.0f} best - {val_b:.0f} finalized) "
            f"{check['op']} {check['threshold']} → {'pass' if passed else 'FAIL'}"
        )
        return {"name": name, "description": description, "passed": passed, "detail": detail}

    return {
        "name": name, "description": description, "passed": False,
        "detail": f"unknown check type: {check_type!r}",
    }


def evaluate_service(service_name: str, config: dict, timeout: int) -> dict:
    """Run all checks for a service. Returns a result dict."""
    scrape_type = config.get("scrape_type", "prometheus")
    up, metrics, error = scrape(config["url"], timeout, scrape_type)

    check_results = [_evaluate_check(c, up, metrics) for c in config["checks"]]
    healthy = all(c["passed"] for c in check_results)

    # Snapshot tracked metrics so the diff pass can detect stalled block heights
    metric_snapshot = {
        key: metrics[key]
        for key in config.get("track_metrics", [])
        if key in metrics
    }

    result: dict = {
        "service":         service_name,
        "healthy":         healthy,
        "checks":          check_results,
        "metric_snapshot": metric_snapshot,
    }
    if not up:
        result["error"] = error
    return result


# ---------------------------------------------------------------------------
# Diff against previous report
# ---------------------------------------------------------------------------

def diff_reports(previous: dict, current: dict) -> list[dict]:
    """
    Return regressions between two consecutive reports.
    Detects two kinds:
      health_degraded — a service was healthy last run, is unhealthy now
      metric_stalled  — a tracked metric value is identical between runs
                        (block height not advancing = stalled chain)
    """
    regressions: list[dict] = []
    prev_by_svc = {s["service"]: s for s in previous.get("services", [])}

    for svc in current.get("services", []):
        name = svc["service"]
        prev = prev_by_svc.get(name)
        if not prev:
            continue

        # Health flip: was healthy, now isn't
        if prev["healthy"] and not svc["healthy"]:
            failed = [c["name"] for c in svc["checks"] if not c["passed"]]
            regressions.append({
                "service":       name,
                "type":          "health_degraded",
                "detail":        "was healthy, now unhealthy",
                "failed_checks": failed,
            })

        # Block height stall: metric present in both runs but value unchanged
        prev_snap = prev.get("metric_snapshot", {})
        curr_snap = svc.get("metric_snapshot", {})
        for metric, curr_val in curr_snap.items():
            prev_val = prev_snap.get(metric)
            if prev_val is not None and curr_val == prev_val:
                regressions.append({
                    "service": name,
                    "type":    "metric_stalled",
                    "detail":  f"{metric} unchanged between runs (value: {curr_val})",
                })

    return regressions


def load_previous_report(report_dir: Path, current_path: Path) -> dict | None:
    """Return parsed contents of the most recent prior report, or None."""
    candidates = sorted(
        [p for p in report_dir.glob("health_*.json") if p != current_path],
        reverse=True,
    )
    if not candidates:
        return None
    try:
        with open(candidates[0]) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None  # corrupt or missing previous report is not fatal


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_report(report: dict, quiet: bool) -> None:
    """
    Print a human-readable summary.
    In quiet mode, only prints when something is unhealthy or a regression fired.
    This makes it safe to run from cron without flooding the log file.
    """
    overall_healthy = report["overall_healthy"]
    regressions     = report.get("regressions", [])

    if quiet and overall_healthy and not regressions:
        return

    ts     = report["timestamp"]
    status = "HEALTHY" if overall_healthy else "UNHEALTHY"
    print(f"[{ts}] Overall: {status}")

    for svc in report["services"]:
        svc_label = "OK  " if svc["healthy"] else "FAIL"
        print(f"  {svc['service']:22s} {svc_label}")
        for check in svc["checks"]:
            mark = "+" if check["passed"] else "x"
            print(f"    [{mark}] {check['description']}: {check['detail']}")
        if "error" in svc:
            print(f"         error: {svc['error']}")

    if regressions:
        print("\nREGRESSIONS since last run:")
        for reg in regressions:
            print(f"  [{reg['service']}] {reg['detail']}")
            if "failed_checks" in reg:
                print(f"    failed checks: {', '.join(reg['failed_checks'])}")

    print(f"\nReport: {report['_path']}")


# ---------------------------------------------------------------------------
# Single run
# ---------------------------------------------------------------------------

def run_once(services: dict, report_dir: Path, timeout: int, quiet: bool) -> int:
    """Evaluate all services, write report, print summary. Returns exit code."""
    report_dir.mkdir(parents=True, exist_ok=True)

    timestamp   = datetime.now(timezone.utc)
    ts_str      = timestamp.strftime("%Y%m%dT%H%M%SZ")
    report_path = report_dir / f"health_{ts_str}.json"

    services        = [evaluate_service(name, cfg, timeout) for name, cfg in services.items()]
    overall_healthy = all(s["healthy"] for s in services)

    report = {
        "timestamp":       timestamp.isoformat(),
        "overall_healthy": overall_healthy,
        "services":        services,
        "regressions":     [],
    }

    prev = load_previous_report(report_dir, report_path)
    if prev:
        report["regressions"] = diff_reports(prev, report)

    # _path is for display only — excluded from the written file
    report["_path"] = str(report_path)

    try:
        with open(report_path, "w") as f:
            json.dump({k: v for k, v in report.items() if k != "_path"}, f, indent=2)
    except OSError as exc:
        print(f"[error] Could not write report to {report_path}: {exc}", file=sys.stderr)

    print_report(report, quiet)
    return 0 if overall_healthy else 1


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    here = Path(__file__).parent
    parser = argparse.ArgumentParser(
        description="Midnight FNO node health checker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--services",
        default=str(_DEFAULT_SERVICES_FILE),
        metavar="FILE",
        help="Path to services JSON config (default: services.json next to this script)",
    )
    parser.add_argument(
        "--report-dir",
        default=str(here.parent / "reports"),
        metavar="DIR",
        help="Directory to write JSON health reports (default: ../reports/)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=5,
        metavar="SECS",
        help="Per-scrape HTTP timeout in seconds (default: 5)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        metavar="SECS",
        help="Poll continuously on this interval; omit for a single run",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress output when all services are healthy (useful for cron)",
    )
    return parser.parse_args()


def main() -> int:
    args       = parse_args()
    services   = load_services(Path(args.services))
    report_dir = Path(args.report_dir)

    if args.interval is not None:
        if args.interval < 1:
            print("[error] --interval must be >= 1 second", file=sys.stderr)
            return 2
        print(f"Polling every {args.interval}s — Ctrl-C to stop\n")
        last_exit = 0
        try:
            while True:
                last_exit = run_once(services, report_dir, args.timeout, args.quiet)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nStopped.")
        return last_exit

    return run_once(services, report_dir, args.timeout, args.quiet)


if __name__ == "__main__":
    sys.exit(main())
