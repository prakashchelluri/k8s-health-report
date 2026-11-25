# k8s-health-report

A read-only health and best-practice report for Kubernetes and OpenShift clusters. It runs live through `kubectl` or offline from saved JSON.

> Lab project: a clean-room reference implementation of a pattern I use at work. It contains no employer code or data.

## The problem

Before a maintenance window, a node drain, or an upgrade, and during on-call handover, you want to know quickly what is already unhealthy and what will break when nodes go away. That means pods that are crash-looping, can't pull their image, or were OOMKilled; nodes under pressure; and the quieter risks: single-replica workloads, multi-replica workloads with no PodDisruptionBudget, containers with no requests or probes, and images on `:latest`. Each of these is one `kubectl` and `jq` query, but in practice nobody runs all of them every time.

This tool runs all of those checks in one pass. It only ever calls `kubectl get`, and the output can go straight into a ticket.

## Quick start

```bash
python -m pip install -e .                       # Python 3.11+, no runtime dependencies

# offline, from saved snapshots (works in CI, needs no cluster)
k8s-health-report --from-file tests/fixtures/cluster.json tests/fixtures/nodes.json

# live, read-only
k8s-health-report --live --context my-cluster
k8s-health-report --live --format markdown > report.md
```

To capture a snapshot for offline use:

```bash
kubectl get pods,deployments,statefulsets,poddisruptionbudgets -A -o json > cluster.json
kubectl get nodes -o json > nodes.json
```

## Example output

Run against the fixtures that ship with the tests:

```
$ k8s-health-report --from-file tests/fixtures/cluster.json tests/fixtures/nodes.json
28 findings (4 critical, 17 warning, 7 info)

== (cluster) (1 critical, 2 warning, 1 info) ==
  CRITICAL  node-not-ready      Node/worker-3.example.com: Ready=Unknown (NodeStatusUnknown)
  WARNING   node-pressure       Node/worker-2.example.com: MemoryPressure=True
  WARNING   node-pressure       Node/worker-3.example.com: PIDPressure=True
  INFO      node-cordoned       Node/worker-4.example.com: node is cordoned (unschedulable)

== payments (6 warning, 3 info) ==
  WARNING   high-restarts       Pod/ledger-db-2: container postgres restarted 7 times
  WARNING   latest-tag          Pod/debug-shell: container shell: image 'busybox' uses :latest or no tag
  WARNING   no-readiness-probe  Pod/debug-shell: container shell: no readinessProbe
  WARNING   no-requests         Pod/debug-shell: container shell: no cpu/memory request
  WARNING   pod-not-ready       Pod/debug-shell: phase=Pending, ContainersNotReady
  WARNING   single-replica      StatefulSet/cache: 1 replica: any restart or node drain is an outage
  INFO      no-limits           Pod/debug-shell: container shell: no cpu/memory limit
  INFO      no-limits           StatefulSet/cache: container redis: no cpu/memory limit
  INFO      no-liveness-probe   Pod/debug-shell: container shell: no livenessProbe

== shop (3 critical, 9 warning, 3 info) ==
  CRITICAL  crashloop           Pod/api-5c6b7-pqrst: container api: CrashLoopBackOff (back-off 5m0s restarting failed container=api)
  CRITICAL  image-pull          Pod/worker-6f8d9-zrtwq: container worker: ImagePullBackOff (Back-off pulling image "registry.example.com/shop/worker")
  CRITICAL  oom-killed          Pod/api-5c6b7-pqrst: container api was OOMKilled (lastState)
  WARNING   high-restarts       Pod/api-5c6b7-pqrst: container api restarted 14 times
  WARNING   latest-tag          Deployment/api: container api: image 'registry.example.com/shop/api:latest' uses :latest or no tag
  WARNING   latest-tag          Deployment/worker: container worker: image 'registry.example.com/shop/worker' uses :latest or no tag
  WARNING   no-pdb              Deployment/api: 2 replicas but no PodDisruptionBudget selects them
  WARNING   no-readiness-probe  Deployment/worker: container worker: no readinessProbe
  WARNING   no-requests         Deployment/worker: container worker: no cpu/memory request
  WARNING   pod-not-ready       Pod/api-5c6b7-pqrst: phase=Running, ContainersNotReady
  WARNING   pod-not-ready       Pod/worker-6f8d9-zrtwq: phase=Pending, ContainersNotReady
  WARNING   single-replica      Deployment/worker: 1 replica: any restart or node drain is an outage
  INFO      no-limits           Deployment/worker: container worker: no cpu/memory limit
  INFO      no-liveness-probe   Deployment/api: container api: no livenessProbe
  INFO      no-liveness-probe   Deployment/worker: container worker: no livenessProbe
```

Exit code: `2`. The `kube-system` and `openshift-monitoring` problems in the fixture are hidden by the default excludes.

The same data with `--format markdown -n payments`, ready to paste into a ticket:

```markdown
## Cluster health report

**9 findings (0 critical, 6 warning, 3 info)**

### payments

| Severity | Check | Object | Detail |
|---|---|---|---|
| warning | `high-restarts` | Pod/ledger-db-2 | container postgres restarted 7 times |
| warning | `latest-tag` | Pod/debug-shell | container shell: image 'busybox' uses :latest or no tag |
| warning | `no-readiness-probe` | Pod/debug-shell | container shell: no readinessProbe |
| warning | `no-requests` | Pod/debug-shell | container shell: no cpu/memory request |
| warning | `pod-not-ready` | Pod/debug-shell | phase=Pending, ContainersNotReady |
| warning | `single-replica` | StatefulSet/cache | 1 replica: any restart or node drain is an outage |
| info | `no-limits` | Pod/debug-shell | container shell: no cpu/memory limit |
| info | `no-limits` | StatefulSet/cache | container redis: no cpu/memory limit |
| info | `no-liveness-probe` | Pod/debug-shell | container shell: no livenessProbe |
```

## Checks

| Check | Severity | What it looks at |
|---|---|---|
| `pod-not-ready` | warning | Pod `Ready` condition is not `True` (Succeeded pods are skipped) |
| `crashloop` | critical | Container waiting with `CrashLoopBackOff` (init containers too) |
| `image-pull` | critical | Container waiting with `ImagePullBackOff` / `ErrImagePull` |
| `oom-killed` | critical | `state` or `lastState` terminated with `OOMKilled` |
| `high-restarts` | warning | `restartCount` >= `--restart-threshold` (default 5) |
| `no-requests` | warning | Container without a cpu and/or memory request |
| `no-limits` | info | Container without a cpu and/or memory limit |
| `no-readiness-probe` | warning | Container without `readinessProbe` |
| `no-liveness-probe` | info | Container without `livenessProbe` |
| `single-replica` | warning | Deployment/StatefulSet with `replicas: 1` |
| `no-pdb` | warning | Deployment/StatefulSet with more than 1 replica and no PDB in its namespace whose selector matches the pod template labels |
| `node-not-ready` | critical | Node `Ready` is `False` or `Unknown` |
| `node-pressure` | warning | `MemoryPressure`, `DiskPressure` or `PIDPressure` is `True` |
| `node-cordoned` | info | Node is `unschedulable` |
| `latest-tag` | warning | Image with `:latest` or no tag (digest-pinned images are fine) |

The container-spec checks (requests, limits, probes, image tag) run on Deployment and StatefulSet pod templates and on bare pods that have no owner. They are not repeated for every replica.

## Options

| Option | Meaning |
|---|---|
| `--live` / `--from-file F [F ...]` | Input source (one is required). Files can be any `kubectl get -o json` output: a `List` or a single object. |
| `--context` | kubectl context for `--live`. |
| `--format text\|markdown\|json` | Output format (default `text`). |
| `-n, --namespace NS` | Only report these namespaces (repeatable). Takes precedence over excludes. |
| `--exclude-namespace GLOB` | Namespaces to skip (repeatable, glob). Replaces the default `kube-system openshift-*`. Use `--exclude-namespace ''` to include everything. |
| `--restart-threshold N` | Restart count that counts as high (default 5). |
| `--fail-on info\|warning\|critical\|never` | Lowest severity that makes the exit code non-zero (default `warning`). |

Exit codes: `0` nothing at or above `--fail-on`, `1` highest finding is warning or info, `2` critical findings, `3` input error (kubectl failed, unreadable file). In CI, `--fail-on critical` is a sensible gate.

## How it works

- `--live` runs exactly two commands, both `kubectl get` with a request timeout: `get pods,deployments,statefulsets,poddisruptionbudgets -A -o json` and `get nodes -o json`. Nothing is written to the cluster. The user only needs `get`/`list` RBAC on those resources.
- All objects go into one list, grouped by `kind`. Each check is a small function over plain dicts (`src/k8s_health_report/checks.py`), so adding a check takes a few lines plus a fixture.
- PDB matching follows the Kubernetes `LabelSelector` rules: `matchLabels` plus `matchExpressions` with `In`, `NotIn`, `Exists` and `DoesNotExist`. An empty selector matches every pod in the namespace.
- Findings are grouped by namespace (nodes under `(cluster)`) and sorted by severity.
- Standard library only.

## Development

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"      # Windows: .venv\Scripts\python
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
```

`tests/fixtures/` holds realistic `kubectl get -o json` output that triggers every check, with healthy counter-examples next to it (a PDB-covered Deployment, a `matchExpressions` PDB, a digest-pinned image, a Succeeded Job pod, a stale PDB that selects nothing). Live mode is tested with a mocked `subprocess.run`. No cluster is needed.

## Limitations

- Workload coverage is Deployments, StatefulSets and bare pods. DaemonSets, Jobs/CronJobs and OpenShift DeploymentConfigs are not checked for spec issues, although their pods still get the status checks (not ready, crash loop, OOM, restarts).
- Restart counts are lifetime totals from the pod status. There is no time window, so an old pod with a few historic restarts can still be flagged.
- PDB checks only confirm a PDB exists and selects the pods. They do not judge whether its `minAvailable`/`maxUnavailable` actually allows a drain.
- Init containers are checked for crash and pull errors, but not for probes or resources.
