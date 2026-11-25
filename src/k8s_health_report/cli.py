"""Command line entry point: k8s-health-report --live | --from-file FILE..."""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys

from .checks import SEVERITIES, run_checks
from .report import render_json, render_markdown, render_text

EXIT_OK, EXIT_FINDINGS, EXIT_CRITICAL, EXIT_ERROR = 0, 1, 2, 3
DEFAULT_EXCLUDES = ["kube-system", "openshift-*"]
KUBECTL_QUERIES = [
    ["get", "pods,deployments,statefulsets,poddisruptionbudgets", "-A", "-o", "json"],
    ["get", "nodes", "-o", "json"],
]


class InputError(Exception):
    pass


def _items(doc: dict, source: str) -> list[dict]:
    """Accept a kubectl List (`get -o json`) or a single object."""
    if not isinstance(doc, dict):
        raise InputError(f"{source}: expected a JSON object")
    return doc.get("items", []) if "items" in doc else [doc]


def load_files(paths: list[str]) -> list[dict]:
    objects = []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                objects += _items(json.load(fh), path)
        except (OSError, ValueError) as exc:
            raise InputError(f"cannot read {path}: {exc}") from exc
    return objects


def load_live(context: str | None, kubectl: str = "kubectl", timeout: int = 60) -> list[dict]:
    """Read-only: only ever runs `kubectl get`."""
    base = [kubectl] + (["--context", context] if context else [])
    objects = []
    for query in KUBECTL_QUERIES:
        cmd = base + query + [f"--request-timeout={timeout}s"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
        except FileNotFoundError as exc:
            raise InputError(f"{kubectl} not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise InputError(f"timed out after {timeout}s: {' '.join(cmd)}") from exc
        if proc.returncode != 0:
            raise InputError(f"{' '.join(cmd)} failed: {proc.stderr.strip()}")
        try:
            objects += _items(json.loads(proc.stdout), " ".join(cmd))
        except ValueError as exc:
            raise InputError(f"invalid JSON from {' '.join(cmd)}") from exc
    return objects


def namespace_filter(namespaces: list[str] | None, excludes: list[str]):
    """Return a predicate for a finding's namespace. Nodes ("") always pass."""
    def keep(ns: str) -> bool:
        if not ns:
            return True
        if namespaces:  # an explicit --namespace wins over the exclude list
            return ns in namespaces
        return not any(fnmatch.fnmatchcase(ns, pat) for pat in excludes)
    return keep


def exit_code(findings, fail_on: str) -> int:
    if fail_on == "never":
        return EXIT_OK
    threshold = SEVERITIES.index(fail_on)
    worst = max((f.rank for f in findings), default=-1)
    if worst < threshold:
        return EXIT_OK
    return EXIT_CRITICAL if SEVERITIES[worst] == "critical" else EXIT_FINDINGS


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="k8s-health-report",
        description="Read-only Kubernetes/OpenShift health and best-practice report.",
        epilog="Exit codes: 0 nothing at/above --fail-on, 1 warning/info findings, "
               "2 critical findings, 3 input error.",
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--live", action="store_true", help="query the cluster with kubectl get")
    src.add_argument("--from-file", nargs="+", metavar="JSON",
                     help="saved `kubectl get ... -o json` output (one or more files)")
    p.add_argument("--context", help="kubectl context to use with --live")
    p.add_argument("--format", choices=["text", "markdown", "json"], default="text")
    p.add_argument("-n", "--namespace", action="append",
                   help="only report these namespaces (repeatable)")
    p.add_argument("--exclude-namespace", action="append", metavar="PATTERN",
                   help="glob of namespaces to skip (repeatable; replaces the default "
                        f"{' '.join(DEFAULT_EXCLUDES)}; use '' to exclude nothing)")
    p.add_argument("--restart-threshold", type=int, default=5,
                   help="restart count that counts as high (default 5)")
    p.add_argument("--fail-on", choices=SEVERITIES + ["never"], default="warning",
                   help="lowest severity that makes the exit code non-zero (default warning)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        objects = load_live(args.context) if args.live else load_files(args.from_file)
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    excludes = DEFAULT_EXCLUDES if args.exclude_namespace is None else [
        e for e in args.exclude_namespace if e]
    keep = namespace_filter(args.namespace, excludes)
    findings = [f for f in run_checks(objects, args.restart_threshold) if keep(f.namespace)]

    render = {"text": render_text, "markdown": render_markdown, "json": render_json}
    print(render[args.format](findings))
    return exit_code(findings, args.fail_on)


if __name__ == "__main__":
    sys.exit(main())
