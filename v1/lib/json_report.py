"""JSON report generation for branch test analysis."""

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional


def build_repo_json_data(
    repo: str,
    branch: str,
    master_branch: str,
    results: Any,
    behavior_analysis: Dict[str, Dict],
    stats: Dict,
    all_test_details: Dict[str, List[Dict]],
    stable_since: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Build JSON-serializable analytics data for one repo/branch.

    Includes: latest run info, summary statistics, per-test classification,
    error messages, and stable-failing duration from MongoDB history.
    """
    stable_failing = behavior_analysis.get('stable_failing', {})
    fixed_tests = behavior_analysis.get('fixed_tests', {})
    flaky_tests = behavior_analysis.get('flaky_tests', {})
    all_behavior = {**stable_failing, **fixed_tests, **flaky_tests}

    # Latest run info (last key in chronologically ordered summary)
    ordered_keys = list(results.summary.keys())
    latest_key = ordered_keys[-1] if ordered_keys else None
    latest_meta = results.meta.get(latest_key, {}) if latest_key else {}
    latest_failed = results.summary.get(latest_key, set()) if latest_key else set()

    # Build per-test entries for the latest run
    failed_tests = []
    for test_name in sorted(latest_failed):
        details_items = all_test_details.get(test_name, [])
        error_msg = _extract_error(details_items)
        project = _extract_project(details_items)
        classification = _classify_test(test_name, stable_failing, fixed_tests, flaky_tests)

        entry: Dict[str, Any] = {
            "test_name": test_name,
            "error_message": error_msg,
            "classification": classification,
            "in_master": test_name in results.master_failed,
            "project": project,
        }

        behavior = all_behavior.get(test_name)
        if behavior:
            total = max(behavior.get('total_runs', 1), 1)
            entry["fail_rate_pct"] = round(
                (behavior.get('fail_count', 0) / total) * 100, 1
            )
            entry["pattern"] = behavior.get('pattern', '')

        # Probable cause: commit that started the current failure streak
        streak = _find_streak_start(
            behavior.get('pattern', '') if behavior else '',
            ordered_keys, results.meta,
        )
        if streak:
            entry["probable_cause"] = streak
        elif not behavior:
            entry["probable_cause"] = {
                "sha": latest_meta.get("sha", ""),
                "commit_title": latest_meta.get("title", ""),
                "timestamp": latest_meta.get("ts", ""),
                "run_link": latest_meta.get("link", ""),
                "streak_length": 1,
            }

        # Stable-failing: add duration info
        if test_name in stable_failing:
            since = stable_since.get(test_name)
            if since:
                entry["failing_since"] = {
                    "run_id": since.get("run_id"),
                    "date": since.get("created_at", ""),
                }
            first_fail = (stable_failing[test_name].get('failed_runs') or [{}])[0]
            entry["first_seen_in_analysis"] = {
                "timestamp": first_fail.get('meta', {}).get('ts', ''),
                "commit": first_fail.get('meta', {}).get('title', ''),
                "run_link": first_fail.get('meta', {}).get('link', ''),
            }

        # Flaky: add pattern info
        if test_name in flaky_tests:
            entry["flaky_info"] = {
                "fail_count": flaky_tests[test_name].get('fail_count', 0),
                "total_runs": flaky_tests[test_name].get('total_runs', 0),
            }

        failed_tests.append(entry)

    return {
        "repo": repo,
        "branch": branch,
        "master_branch": master_branch,
        "latest_run": {
            "run_id": latest_meta.get("run_id"),
            "sha": latest_meta.get("sha", ""),
            "commit_title": latest_meta.get("title", ""),
            "timestamp": latest_meta.get("ts", ""),
            "conclusion": latest_meta.get("concl", ""),
            "link": latest_meta.get("link", ""),
            "total_failed": len(latest_failed),
        },
        "summary": {
            "total_runs_analyzed": stats.get('total_runs', 0),
            "unique_failed_tests": stats.get('unique_failed_tests', 0),
            "master_failed_tests": stats.get('master_failed_tests', 0),
            "new_failures": stats.get('new_failures', 0),
            "stable_failing_count": len(stable_failing),
            "fixed_count": len(fixed_tests),
            "flaky_count": len(flaky_tests),
        },
        "failed_tests": failed_tests,
    }


def generate_json_report(projects_data: List[Dict[str, Any]], output_dir: Path) -> Path:
    """
    Generate a combined JSON report for all projects.

    File is named with current datetime: report_YYYYMMDD_HHMMSS.json
    Returns path to the generated file.
    """
    now = datetime.now()
    report: Dict[str, Any] = {
        "generated_at": now.isoformat(),
        "projects": {},
    }

    for proj in projects_data:
        key = f"{proj['repo']}/{proj['branch']}"
        report["projects"][key] = proj

    filename = f"report_{now.strftime('%Y%m%d_%H%M%S')}.json"
    report_path = output_dir / filename
    report_path.parent.mkdir(parents=True, exist_ok=True)

    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)

    print(f"📄 JSON отчёт сохранён: {report_path}")
    return report_path


def _extract_error(details_items: List[Dict]) -> str:
    """Extract first error context from test details."""
    if not details_items:
        return ""
    return (details_items[0].get('context') or '').strip()


def _extract_project(details_items: List[Dict]) -> str:
    """Extract project name from test details."""
    if not details_items:
        return ""
    return details_items[0].get('project', '')


def _find_streak_start(
    pattern: str,
    ordered_keys: list,
    meta: Dict,
) -> Optional[Dict[str, Any]]:
    """
    Find the run that started the current consecutive failure streak.

    Walks the pattern backwards from the latest run, counting 🔴.
    Returns info about the commit where the streak began.
    """
    if not pattern or pattern[-1] != '\U0001f534':  # 🔴
        return None

    idx = len(pattern) - 1
    while idx > 0 and pattern[idx - 1] == '\U0001f534':
        idx -= 1

    if idx >= len(ordered_keys):
        return None

    key = ordered_keys[idx]
    run_meta = meta.get(key, {})
    return {
        "sha": run_meta.get("sha", ""),
        "commit_title": run_meta.get("title", ""),
        "timestamp": run_meta.get("ts", ""),
        "run_link": run_meta.get("link", ""),
        "streak_length": len(pattern) - idx,
    }


def _classify_test(
    test_name: str,
    stable_failing: Dict,
    fixed_tests: Dict,
    flaky_tests: Dict,
) -> str:
    """Classify test by its behavior type."""
    if test_name in stable_failing:
        return "stable_failing"
    if test_name in fixed_tests:
        return "fixed"
    if test_name in flaky_tests:
        return "flaky"
    return "single_failure"
