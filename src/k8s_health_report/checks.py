"""Health and best-practice checks over plain `kubectl get -o json` objects."""

from __future__ import annotations

from dataclasses import dataclass

SEVERITIES = ["info", "warning", "critical"]  # ascending


@dataclass
class Finding:
    severity: str
    check: str
    namespace: str  # "" for cluster-scoped objects (nodes)
    kind: str
    name: str
    message: str

    @property
    def rank(self) -> int:
        return SEVERITIES.index(self.severity)


def _meta(obj: dict) -> tuple[str, str, str]:
    m = obj.get("metadata", {})
    return obj.get("kind", "?"), m.get("namespace", ""), m.get("name", "?")


def _finding(obj: dict, severity: str, check: str, message: str) -> Finding:
    kind, ns, name = _meta(obj)
    return Finding(severity, check, ns, kind, name, message)


# --- pod status ------------------------------------------------------------

WAITING_REASONS = {
    "CrashLoopBackOff": ("crashloop", "critical"),
    "ImagePullBackOff": ("image-pull", "critical"),
    "ErrImagePull": ("image-pull", "critical"),
}


def check_pod_status(pod: dict, restart_threshold: int) -> list[Finding]:
    out = []
    status = pod.get("status", {})
    phase = status.get("phase", "Unknown")
    if phase != "Succeeded":
        ready = next((c for c in status.get("conditions", []) if c.get("type") == "Ready"), {})
        if ready.get("status") != "True":
            why = ready.get("reason") or status.get("reason") or "no Ready condition"
            out.append(_finding(pod, "warning", "pod-not-ready", f"phase={phase}, {why}"))

    statuses = status.get("initContainerStatuses", []) + status.get("containerStatuses", [])
    for cs in statuses:
        c = cs.get("name", "?")
        waiting = cs.get("state", {}).get("waiting") or {}
        if waiting.get("reason") in WAITING_REASONS:
            check, sev = WAITING_REASONS[waiting["reason"]]
            detail = waiting.get("message", "")
            out.append(_finding(pod, sev, check, f"container {c}: {waiting['reason']}"
                                + (f" ({detail})" if detail else "")))
        for when in ("state", "lastState"):
            terminated = cs.get(when, {}).get("terminated") or {}
            if terminated.get("reason") == "OOMKilled":
                out.append(_finding(pod, "critical", "oom-killed",
                                    f"container {c} was OOMKilled ({when})"))
                break
        restarts = cs.get("restartCount", 0)
        if restarts >= restart_threshold:
            out.append(_finding(pod, "warning", "high-restarts",
                                f"container {c} restarted {restarts} times"))
    return out


# --- container spec (workload templates and bare pods) -----------------------

def image_has_floating_tag(image: str) -> bool:
    """True for 'app', 'app:latest', 'registry:5000/app'; False for pinned tags/digests."""
    if "@" in image:
        return False
    last = image.rsplit("/", 1)[-1]
    return ":" not in last or last.rsplit(":", 1)[1] == "latest"


def check_containers(obj: dict, pod_spec: dict) -> list[Finding]:
    out = []
    for c in pod_spec.get("containers", []):
        name = c.get("name", "?")
        res = c.get("resources", {})
        missing_req = [r for r in ("cpu", "memory") if r not in res.get("requests", {})]
        missing_lim = [r for r in ("cpu", "memory") if r not in res.get("limits", {})]
        if missing_req:
            out.append(_finding(obj, "warning", "no-requests",
                                f"container {name}: no {'/'.join(missing_req)} request"))
        if missing_lim:
            out.append(_finding(obj, "info", "no-limits",
                                f"container {name}: no {'/'.join(missing_lim)} limit"))
        if "readinessProbe" not in c:
            out.append(_finding(obj, "warning", "no-readiness-probe",
                                f"container {name}: no readinessProbe"))
        if "livenessProbe" not in c:
            out.append(_finding(obj, "info", "no-liveness-probe",
                                f"container {name}: no livenessProbe"))
        image = c.get("image", "")
        if image_has_floating_tag(image):
            out.append(_finding(obj, "warning", "latest-tag",
                                f"container {name}: image '{image}' uses :latest or no tag"))
    return out


# --- workloads and PDBs ----------------------------------------------------

def selector_matches(selector: dict | None, labels: dict) -> bool:
    """Kubernetes LabelSelector semantics. An empty selector matches everything."""
    if selector is None:
        return False
    for k, v in (selector.get("matchLabels") or {}).items():
        if labels.get(k) != v:
            return False
    for expr in selector.get("matchExpressions") or []:
        key, op, values = expr["key"], expr["operator"], expr.get("values") or []
        ok = {
            "In": key in labels and labels[key] in values,
            "NotIn": key not in labels or labels[key] not in values,
            "Exists": key in labels,
            "DoesNotExist": key not in labels,
        }.get(op, False)
        if not ok:
            return False
    return True


def check_workload(wl: dict, pdbs: list[dict]) -> list[Finding]:
    out = []
    spec = wl.get("spec", {})
    replicas = spec.get("replicas", 1)
    if replicas == 1:
        out.append(_finding(wl, "warning", "single-replica",
                            "1 replica: any restart or node drain is an outage"))
    elif replicas > 1:
        _, ns, _ = _meta(wl)
        labels = spec.get("template", {}).get("metadata", {}).get("labels", {})
        if not any(_meta(p)[1] == ns and selector_matches(p.get("spec", {}).get("selector"),
                                                          labels) for p in pdbs):
            out.append(_finding(wl, "warning", "no-pdb",
                                f"{replicas} replicas but no PodDisruptionBudget selects them"))
    out += check_containers(wl, spec.get("template", {}).get("spec", {}))
    return out


# --- nodes -----------------------------------------------------------------

def check_node(node: dict) -> list[Finding]:
    out = []
    conditions = {c.get("type"): c for c in node.get("status", {}).get("conditions", [])}
    ready = conditions.get("Ready", {})
    if ready.get("status") != "True":
        out.append(_finding(node, "critical", "node-not-ready",
                            f"Ready={ready.get('status', 'missing')}"
                            + (f" ({ready['reason']})" if ready.get("reason") else "")))
    for cond in ("MemoryPressure", "DiskPressure", "PIDPressure"):
        if conditions.get(cond, {}).get("status") == "True":
            out.append(_finding(node, "warning", "node-pressure", f"{cond}=True"))
    if node.get("spec", {}).get("unschedulable"):
        out.append(_finding(node, "info", "node-cordoned", "node is cordoned (unschedulable)"))
    return out


# --- entry point -----------------------------------------------------------

def run_checks(objects: list[dict], restart_threshold: int = 5) -> list[Finding]:
    by_kind: dict[str, list[dict]] = {}
    for obj in objects:
        by_kind.setdefault(obj.get("kind", ""), []).append(obj)
    pdbs = by_kind.get("PodDisruptionBudget", [])

    findings = []
    for node in by_kind.get("Node", []):
        findings += check_node(node)
    for wl in by_kind.get("Deployment", []) + by_kind.get("StatefulSet", []):
        findings += check_workload(wl, pdbs)
    for pod in by_kind.get("Pod", []):
        findings += check_pod_status(pod, restart_threshold)
        # Controller-owned pods are covered by their template; check bare pods directly.
        if not pod.get("metadata", {}).get("ownerReferences"):
            findings += check_containers(pod, pod.get("spec", {}))
    return findings
