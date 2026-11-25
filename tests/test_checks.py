import json
import subprocess
from pathlib import Path

import pytest

from k8s_health_report import cli
from k8s_health_report.checks import image_has_floating_tag, run_checks, selector_matches

FIXTURES = Path(__file__).parent / "fixtures"
FILES = [str(FIXTURES / "cluster.json"), str(FIXTURES / "nodes.json")]


@pytest.fixture(scope="module")
def findings():
    return run_checks(cli.load_files(FILES))


def found(findings, check):
    """{(kind/name): finding} for one check id."""
    return {f"{f.kind}/{f.name}": f for f in findings if f.check == check}


def test_pod_not_ready(findings):
    hits = found(findings, "pod-not-ready")
    assert {"Pod/api-5c6b7-pqrst", "Pod/worker-6f8d9-zrtwq", "Pod/debug-shell"} <= set(hits)
    assert "Pod/db-migrate-28411-abcde" not in hits  # Succeeded job pods are fine
    assert not any(k.startswith("Pod/web-") for k in hits)
    assert hits["Pod/worker-6f8d9-zrtwq"].message.startswith("phase=Pending")


def test_crashloop_image_pull_oom(findings):
    crash = found(findings, "crashloop")
    assert set(crash) == {"Pod/api-5c6b7-pqrst", "Pod/coredns-787d4945fb-xk2lp"}
    assert crash["Pod/api-5c6b7-pqrst"].severity == "critical"
    assert set(found(findings, "image-pull")) == {"Pod/worker-6f8d9-zrtwq"}
    oom = found(findings, "oom-killed")
    assert set(oom) == {"Pod/api-5c6b7-pqrst"} and "lastState" in oom["Pod/api-5c6b7-pqrst"].message


def test_high_restarts_threshold(findings):
    assert set(found(findings, "high-restarts")) == {
        "Pod/api-5c6b7-pqrst", "Pod/ledger-db-2", "Pod/coredns-787d4945fb-xk2lp"}
    strict = run_checks(cli.load_files(FILES), restart_threshold=10)
    assert set(found(strict, "high-restarts")) == {"Pod/api-5c6b7-pqrst"}


def test_resources(findings):
    requests = found(findings, "no-requests")
    assert set(requests) == {"Deployment/worker", "Pod/debug-shell"}
    assert "cpu/memory" in requests["Deployment/worker"].message
    assert set(found(findings, "no-limits")) == {
        "Deployment/worker", "StatefulSet/cache", "Pod/debug-shell"}


def test_probes(findings):
    assert set(found(findings, "no-readiness-probe")) == {"Deployment/worker", "Pod/debug-shell"}
    assert set(found(findings, "no-liveness-probe")) == {
        "Deployment/api", "Deployment/worker", "Pod/debug-shell"}


def test_controller_owned_pods_are_not_double_counted(findings):
    spec_checks = {"no-requests", "no-limits", "no-readiness-probe", "no-liveness-probe",
                   "latest-tag"}
    pods = {f.name for f in findings if f.check in spec_checks and f.kind == "Pod"}
    assert pods == {"debug-shell"}  # the only pod without an owner


def test_single_replica(findings):
    assert set(found(findings, "single-replica")) == {
        "Deployment/worker", "StatefulSet/cache", "Deployment/thanos-querier"}


def test_pdb_matching(findings):
    # web: matchLabels PDB; ledger-db: matchExpressions PDB; api: none (old-ledger-pdb is stale)
    assert set(found(findings, "no-pdb")) == {"Deployment/api"}


def test_nodes(findings):
    assert set(found(findings, "node-not-ready")) == {"Node/worker-3.example.com"}
    pressure = sorted(f"{f.name}:{f.message}" for f in findings if f.check == "node-pressure")
    assert pressure == ["worker-2.example.com:MemoryPressure=True",
                        "worker-3.example.com:PIDPressure=True"]
    assert set(found(findings, "node-cordoned")) == {"Node/worker-4.example.com"}


def test_latest_tag(findings):
    assert set(found(findings, "latest-tag")) == {
        "Deployment/api", "Deployment/worker", "Pod/debug-shell"}


@pytest.mark.parametrize("image,floating", [
    ("busybox", True),
    ("nginx:latest", True),
    ("registry.example.com:5000/team/app", True),
    ("registry.example.com:5000/team/app:1.2.3", False),
    ("registry.example.com/team/app@sha256:" + "a" * 64, False),
    ("registry.example.com/team/app:latest@sha256:" + "a" * 64, False),
])
def test_image_tag_parsing(image, floating):
    assert image_has_floating_tag(image) is floating


def test_selector_operators():
    labels = {"app": "web", "tier": "front"}
    assert selector_matches({}, labels)  # empty selector selects everything
    assert not selector_matches(None, labels)
    assert selector_matches({"matchLabels": {"app": "web"}}, labels)
    assert not selector_matches({"matchLabels": {"app": "web", "tier": "back"}}, labels)
    expr = lambda op, *v: {"matchExpressions": [{"key": "tier", "operator": op,  # noqa: E731
                                                  "values": list(v)}]}
    assert selector_matches(expr("In", "front", "x"), labels)
    assert not selector_matches(expr("NotIn", "front"), labels)
    assert selector_matches(expr("Exists"), labels)
    assert not selector_matches(expr("DoesNotExist"), labels)


# --- CLI: filtering, formats, exit codes, live mode ------------------------

def run(capsys, *argv):
    code = cli.main(list(argv))
    return code, capsys.readouterr().out


def test_default_excludes_system_namespaces(capsys):
    code, out = run(capsys, "--from-file", *FILES, "--format", "json")
    report = json.loads(out)
    namespaces = {f["namespace"] for f in report["findings"]}
    assert namespaces == {"", "shop", "payments"}
    assert code == 2  # critical findings present
    assert report["summary"]["total"] == len(report["findings"])


def test_namespace_and_exclude_flags(capsys):
    _, out = run(capsys, "--from-file", *FILES, "--format", "json", "-n", "kube-system")
    assert {f["namespace"] for f in json.loads(out)["findings"]} == {"", "kube-system"}
    _, out = run(capsys, "--from-file", *FILES, "--format", "json", "--exclude-namespace", "")
    assert "openshift-monitoring" in {f["namespace"] for f in json.loads(out)["findings"]}
    _, out = run(capsys, "--from-file", *FILES, "--format", "json",
                 "--exclude-namespace", "shop", "--exclude-namespace", "pay*")
    assert {f["namespace"] for f in json.loads(out)["findings"]} == {"", "kube-system",
                                                                     "openshift-monitoring"}


def test_fail_on(capsys):
    args = ["--from-file", str(FIXTURES / "cluster.json"), "-n", "payments"]
    assert run(capsys, *args)[0] == 1  # only warnings/info in payments
    assert run(capsys, *args, "--fail-on", "critical")[0] == 0
    assert run(capsys, *args, "--fail-on", "never")[0] == 0
    assert run(capsys, "--from-file", *FILES, "--fail-on", "never")[0] == 0


def test_no_findings(tmp_path, capsys):
    empty = tmp_path / "empty.json"
    empty.write_text('{"kind": "List", "items": []}')
    assert run(capsys, "--from-file", str(empty)) == (0, "No findings.\n")


def test_text_and_markdown_output(capsys):
    _, text = run(capsys, "--from-file", *FILES)
    assert text.splitlines()[0] == "28 findings (4 critical, 17 warning, 7 info)"
    assert "== (cluster) (1 critical, 2 warning, 1 info) ==" in text
    assert text.index("== (cluster)") < text.index("== payments") < text.index("== shop")
    _, md = run(capsys, "--from-file", *FILES, "--format", "markdown")
    assert "### shop" in md
    assert "| critical | `crashloop` | Pod/api-5c6b7-pqrst |" in md


def test_bad_input_file(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    assert cli.main(["--from-file", str(bad)]) == 3
    assert cli.main(["--from-file", str(tmp_path / "nope.json")]) == 3


def test_live_mode_runs_read_only_kubectl(monkeypatch, capsys):
    calls = []
    outputs = {"pods,deployments,statefulsets,poddisruptionbudgets": "cluster.json",
               "nodes": "nodes.json"}

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        fixture = outputs[cmd[cmd.index("get") + 1]]
        return subprocess.CompletedProcess(cmd, 0, (FIXTURES / fixture).read_text(), "")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    code, out = run(capsys, "--live", "--context", "lab", "--format", "json")
    assert code == 2 and json.loads(out)["summary"]["critical"] == 4
    assert all(c[:4] == ["kubectl", "--context", "lab", "get"] for c in calls)


def test_live_mode_errors(monkeypatch, capsys):
    def fails(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "error: context \"lab\" does not exist")
    monkeypatch.setattr(cli.subprocess, "run", fails)
    assert cli.main(["--live", "--context", "lab"]) == 3
    assert "does not exist" in capsys.readouterr().err

    def missing(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])
    monkeypatch.setattr(cli.subprocess, "run", missing)
    assert cli.main(["--live"]) == 3
