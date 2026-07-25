#!/usr/bin/env python3
"""Publish scanner output into TWO GitHub issues, and sort them properly.

Why two issues:
  `security-findings` previously held everything, and in practice ~96% of it was
  ruff style nits (UP045 union syntax, I001 import order, F401 unused imports).
  The converge fix-loop dispatches off that issue and takes the top N by
  severity, so it spent its cycles rewriting `Optional[str]` while a genuine
  MEDIUM `torch.load` finding sat beyond the 200-item display cutoff. Security
  and style now go to separate issues with separate labels.

Why the sort matters:
  The previous implementation printed "sorted by severity" in the issue body
  while performing no sort at all - the 200-item window was raw insertion order,
  so whichever scanner's SARIF happened to be globbed first filled it. Real
  findings fell off the end and were invisible to both humans and converge.
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

MAX_SHOWN = 200
SECURITY_LABEL = "security-findings"
QUALITY_LABEL = "code-quality"
SECURITY_TITLE = "\U0001f512 Security findings - auto-generated"
QUALITY_TITLE = "\U0001f9f9 Code quality findings - auto-generated"

_LEVEL_RANK = {"error": 0, "warning": 1, "note": 2, "none": 3}


def is_test_path(path: str) -> bool:
    base = os.path.basename(path)
    return "/tests/" in path or base.startswith("test_") or base == "conftest.py"


def sort_key(f: dict) -> tuple:
    """Severity first, then production code ahead of test code, then location.

    Test-code findings are deprioritised rather than dropped: a real issue in a
    test is still worth seeing, just never at the expense of a production one.
    """
    return (
        _LEVEL_RANK.get(str(f.get("level", "warning")).lower(), 1),
        1 if is_test_path(f.get("file", "")) else 0,
        str(f.get("file", "")),
        int(f.get("line", 0) or 0),
    )


def strip_workspace(path: str) -> str:
    """Runner paths are absolute (/home/runner/work/<repo>/<repo>/...) which makes
    the issue body unreadable and non-comparable between runs."""
    ws = os.environ.get("GITHUB_WORKSPACE", "")
    if ws and path.startswith(ws):
        return path[len(ws) :].lstrip("/")
    marker = "/work/"
    if marker in path:
        tail = path.split(marker, 1)[1]
        parts = tail.split("/", 2)
        if len(parts) == 3:
            return parts[2]
    return path


def parse_sarif(filepath: str) -> list[dict]:
    findings: list[dict] = []
    try:
        with open(filepath) as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return findings

    for run in data.get("runs", []):
        tool = run.get("tool", {}).get("driver", {}).get("name", "unknown")
        rules = {r.get("id", ""): r for r in run.get("tool", {}).get("driver", {}).get("rules", [])}
        for result in run.get("results", []):
            # SARIF keeps nosec/nosemgrep-suppressed results with a `suppressions`
            # marker rather than omitting them, so without this every justified
            # suppression is re-reported as an open finding forever.
            if result.get("suppressions"):
                continue
            rid = result.get("ruleId", "unknown")
            desc = rules.get(rid, {}).get("shortDescription", {}).get("text", "")
            for loc in result.get("locations", []):
                phys = loc.get("physicalLocation", {})
                findings.append({
                    "tool": tool,
                    "rule_id": rid,
                    "level": result.get("level", "warning"),
                    "message": result.get("message", {}).get("text", ""),
                    "file": strip_workspace(phys.get("artifactLocation", {}).get("uri", "unknown")),
                    "line": phys.get("region", {}).get("startLine", 0),
                    "description": desc,
                })
    return findings


def parse_ruff(filepath: str) -> list[dict]:
    try:
        with open(filepath) as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    out = []
    for item in data:
        out.append({
            "tool": "ruff",
            "rule_id": item.get("code") or "ruff",
            "level": "note",
            "message": item.get("message", ""),
            "file": strip_workspace(item.get("filename", "unknown")),
            "line": (item.get("location") or {}).get("row", 0),
            "description": item.get("url", ""),
        })
    return out


def build_body(findings: list[dict], scan_date: str, kind: str) -> str:
    if not findings:
        return f"\u2705 No {kind} findings \u2014 last scan: {scan_date}"

    findings.sort(key=sort_key)
    by_tool: dict[str, int] = {}
    by_level: dict[str, int] = {}
    for f in findings:
        by_tool[f["tool"]] = by_tool.get(f["tool"], 0) + 1
        by_level[f["level"]] = by_level.get(f["level"], 0) + 1

    lines = [f"Last scan: {scan_date}", ""]
    lines.append(f"**Total:** {len(findings)}  ")
    lines.append("**By severity:** " + ", ".join(f"{k}={v}" for k, v in sorted(by_level.items())) + "  ")
    lines.append("**By tool:** " + ", ".join(f"{k}={v}" for k, v in sorted(by_tool.items())))
    lines.append("")

    shown = findings[:MAX_SHOWN]
    if len(findings) > MAX_SHOWN:
        lines.append(
            f"Showing the {MAX_SHOWN} highest-severity of {len(findings)} findings "
            "(production code before test code)."
        )
        lines.append("")
    lines.append("```json")
    lines.append(json.dumps(shown, indent=2))
    lines.append("```")
    return "\n".join(lines)


def publish(title: str, label: str, body: str) -> None:
    body_file = f"/tmp/findings_{label}.md"
    with open(body_file, "w") as fh:
        fh.write(body)

    # Match on label, not a free-text search: `gh issue list --search` matched on
    # title words and could pick up an unrelated issue.
    res = subprocess.run(
        ["gh", "issue", "list", "--label", label, "--state", "open", "--json", "number", "--limit", "1"],
        capture_output=True, text=True,
    )
    try:
        issues = json.loads(res.stdout) if res.stdout.strip() else []
    except json.JSONDecodeError:
        issues = []

    if issues:
        num = str(issues[0]["number"])
        subprocess.run(["gh", "issue", "edit", num, "--body-file", body_file], check=False)
        print(f"updated issue #{num} ({label})")
    else:
        subprocess.run(
            ["gh", "issue", "create", "--title", title, "--label", label, "--body-file", body_file],
            check=False,
        )
        print(f"created issue ({label})")


def main() -> int:
    scan_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    security: list[dict] = []
    for path in sorted(glob.glob("*.sarif")):
        security.extend(parse_sarif(path))

    quality = parse_ruff("ruff-advisory.json")

    print(f"security findings: {len(security)} | quality findings: {len(quality)}")
    publish(SECURITY_TITLE, SECURITY_LABEL, build_body(security, scan_date, "security"))
    publish(QUALITY_TITLE, QUALITY_LABEL, build_body(quality, scan_date, "code quality"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
