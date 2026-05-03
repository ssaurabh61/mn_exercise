#!/usr/bin/env python3
"""
node_health_check.py — Midnight FNO node health checker (Section 3, Option C)

Polls Prometheus /metrics endpoints for all FNO services, evaluates health
conditions, writes a timestamped JSON report, diffs regressions against the
previous report, and exits non-zero if any service is unhealthy.

Usage:
    python3 scripts/node_health_check.py [--report-dir /path/to/reports]

Exit codes:
    0 — all services healthy
    1 — one or more services unhealthy or unreachable

Designed to run as a cron job or CI health-gate. Uses stdlib only (no pip deps).
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SERVICES = {
    "cardano-node": {
        "url": "http://127.0.0.1:12798/metrics",
        "checks": [
            {
                "name": "service_up",
                "metric": None,  # None means the scrape itself is the check
                "description": "Prometheus endpoint is reachable",
            },
            {
                "name": "peers_sufficient",
                "metric": "cardano_node_metrics_connectedPeers_int",
                "operator": ">=",
                "threshold": 3,
                "description": "Connected peer count >= 3",
            },
        ],
    },
    "cardano-db-sync": {
        "url": "http://127.0.0.1:8080/metrics",
        "checks": [
            {
                "name": "service_up",
                "metric": None,
                "description": "Prometheus endpoint is reachable",
            },
        ],
    },
    "midnight-node": {
        "url": "http://127.0.0.1:9615/metrics",
        "checks": [
            {
                "name": "service_up",
                "metric": None,
                "description": "Prometheus endpoint is reachable",
            },
        ],
    },
    "host": {
        "url": "http://127.0.0.1:9100/metrics",
        "checks": [
            {
                "name": "service_up",
                "metric": None,
                "description": "node_exporter is reachable",
            },
        ],
    },
}

SCRAPE_TIMEOUT = 5  # seconds

# ---------------------------------------------------------------------------
# Prometheus text format parser (minimal — handles gauge/counter, ignores help/type)
# ---------------------------------------------------------------------------

def parse_prometheus_metrics(text: str) -> dict[str, float]:
    """Parse Prometheus text exposition format into {metric_name: value}."""
    metrics: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Split off the value (and optional timestamp) from the label set
        # Format: metric_name{labels} value [timestamp]
        # or:     metric_name value [timestamp]
        parts = line.rsplit(" ", 1 if line.count(" ") >= 2 else 0)
        if len(parts) < 2:
            continue
        try:
            value = float(parts[-1])
        except ValueError:
            continue
        # Extract bare metric name (strip labels)
        name_part = parts[0].split("{")[0]
        metrics[name_part] = value
    return metrics


# ---------------------------------------------------------------------------
# Scrape and evaluate
# ---------------------------------------------------------------------------

def scrape(url: str, timeout: int) -> tuple[bool, dict[str, float], str]:
    """Fetch metrics URL. Returns (success, parsed_metrics, error_message)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        return True, parse_prometheus_metrics(body), ""
    except urllib.error.URLError as exc:
        return False, {}, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, {}, f"unexpected error: {exc}"


def evaluate_service(service_name: str, config: dict) -> dict:
    """Run all checks for a service and return a result dict."""
    up, metrics, error = scrape(config["url"], SCRAPE_TIMEOUT)
    checks = []
    all_passed = up  # if scrape failed, nothing can pass

    for check in config["checks"]:
        if check["name"] == "service_up":
            checks.append(
                {
                    "name": "service_up",
                    "description": check["description"],
                    "passed": up,
                    "detail": error if not up else "endpoint reachable",
                }
            )
            continue

        if not up:
            checks.append(
                {
                    "name": check["name"],
                    "description": check["description"],
                    "passed": False,
                    "detail": "skipped — endpoint unreachable",
                }
            )
            all_passed = False
            continue

        metric_value = metrics.get(check["metric"])
        if metric_value is None:
            passed = False
            detail = f"metric '{check['metric']}' not found in response"
        else:
            op = check["operator"]
            threshold = check["threshold"]
            if op == ">=":
                passed = metric_value >= threshold
            elif op == ">":
                passed = metric_value > threshold
            elif op == "==":
                passed = metric_value == threshold
            elif op == "<":
                passed = metric_value < threshold
            elif op == "<=":
                passed = metric_value <= threshold
            else:
                passed = False
            detail = f"{metric_value} {op} {threshold} → {'pass' if passed else 'FAIL'}"

        if not passed:
            all_passed = False
        checks.append(
            {
                "name": check["name"],
                "description": check["description"],
                "passed": passed,
                "detail": detail,
            }
        )

    return {
        "service": service_name,
        "healthy": all_passed,
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# Diff against previous report
# ---------------------------------------------------------------------------

def find_previous_report(report_dir: Path, current_path: Path) -> Path | None:
    """Return the most recent report file that isn't the current one."""
    reports = sorted(
        [p for p in report_dir.glob("health_*.json") if p != current_path],
        reverse=True,
    )
    return reports[0] if reports else None


def diff_reports(previous: dict, current: dict) -> list[dict]:
    """Return a list of regressions (services that went healthy → unhealthy)."""
    regressions = []
    prev_by_service = {s["service"]: s for s in previous.get("services", [])}
    for svc in current.get("services", []):
        prev = prev_by_service.get(svc["service"])
        if prev and prev["healthy"] and not svc["healthy"]:
            regressions.append(
                {
                    "service": svc["service"],
                    "regression": "was healthy, now unhealthy",
                    "failed_checks": [c["name"] for c in svc["checks"] if not c["passed"]],
                }
            )
    return regressions


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="FNO node health checker")
    parser.add_argument(
        "--report-dir",
        default=str(Path(__file__).parent.parent / "reports"),
        help="Directory to write JSON health reports (default: ../reports/)",
    )
    args = parser.parse_args()

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc)
    ts_str = timestamp.strftime("%Y%m%dT%H%M%SZ")
    report_path = report_dir / f"health_{ts_str}.json"

    # Evaluate all services
    results = [evaluate_service(name, cfg) for name, cfg in SERVICES.items()]

    overall_healthy = all(r["healthy"] for r in results)

    report = {
        "timestamp": timestamp.isoformat(),
        "overall_healthy": overall_healthy,
        "services": results,
        "regressions": [],
    }

    # Diff against previous report
    prev_path = find_previous_report(report_dir, report_path)
    if prev_path:
        try:
            with open(prev_path) as f:
                prev_report = json.load(f)
            report["regressions"] = diff_reports(prev_report, report)
        except (OSError, json.JSONDecodeError):
            pass  # missing/corrupt previous report is not fatal

    # Write report
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)

    # Print summary to stdout
    status = "HEALTHY" if overall_healthy else "UNHEALTHY"
    print(f"[{ts_str}] Overall: {status}")
    for svc in results:
        svc_status = "OK" if svc["healthy"] else "FAIL"
        print(f"  {svc['service']:20s} {svc_status}")
        for check in svc["checks"]:
            mark = "+" if check["passed"] else "x"
            print(f"    [{mark}] {check['description']}: {check['detail']}")

    if report["regressions"]:
        print("\nREGRESSIONS since last run:")
        for reg in report["regressions"]:
            print(f"  {reg['service']}: {reg['regression']} (failed: {', '.join(reg['failed_checks'])})")

    print(f"\nReport written: {report_path}")

    return 0 if overall_healthy else 1


if __name__ == "__main__":
    sys.exit(main())
