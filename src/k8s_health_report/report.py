"""Render findings as console text, Markdown (for tickets) or JSON."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict

from .checks import SEVERITIES, Finding

CLUSTER = "(cluster)"  # group label for cluster-scoped objects such as nodes


def _sorted_groups(findings: list[Finding]) -> list[tuple[str, list[Finding]]]:
    groups: dict[str, list[Finding]] = {}
    for f in findings:
        groups.setdefault(f.namespace or CLUSTER, []).append(f)
    order = sorted(groups, key=lambda ns: (ns != CLUSTER, ns))
    return [(ns, sorted(groups[ns], key=lambda f: (-f.rank, f.check, f.kind, f.name)))
            for ns in order]


def summary_line(findings: list[Finding]) -> str:
    counts = Counter(f.severity for f in findings)
    parts = ", ".join(f"{counts[s]} {s}" for s in reversed(SEVERITIES))
    return f"{len(findings)} findings ({parts})"


def render_text(findings: list[Finding]) -> str:
    if not findings:
        return "No findings."
    out = [summary_line(findings)]
    for ns, items in _sorted_groups(findings):
        counts = Counter(f.severity for f in items)
        tally = ", ".join(f"{counts[s]} {s}" for s in reversed(SEVERITIES) if counts[s])
        out += ["", f"== {ns} ({tally}) =="]
        for f in items:
            out.append(f"  {f.severity.upper():<8}  {f.check:<18}  {f.kind}/{f.name}: "
                       f"{f.message}")
    return "\n".join(out)


def render_markdown(findings: list[Finding]) -> str:
    out = ["## Cluster health report", "", f"**{summary_line(findings)}**"]
    for ns, items in _sorted_groups(findings):
        out += ["", f"### {ns}", "", "| Severity | Check | Object | Detail |",
                "|---|---|---|---|"]
        for f in items:
            detail = f.message.replace("|", "\\|")
            out.append(f"| {f.severity} | `{f.check}` | {f.kind}/{f.name} | {detail} |")
    return "\n".join(out)


def render_json(findings: list[Finding]) -> str:
    counts = Counter(f.severity for f in findings)
    return json.dumps({
        "summary": {s: counts[s] for s in reversed(SEVERITIES)} | {"total": len(findings)},
        "findings": [asdict(f) for ns, items in _sorted_groups(findings) for f in items],
    }, indent=2)
