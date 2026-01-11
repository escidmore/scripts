"""
Kubernetes health checks via kubectl.
"""

import json
import subprocess
from datetime import datetime, timezone
from typing import Optional

from output import CheckResult, Status


def _run_kubectl(args: list[str], context: Optional[str] = None,
                 timeout: int = 30) -> tuple[bool, str]:
    """Run a kubectl command and return success status and output."""
    cmd = ["kubectl"]
    if context:
        cmd.extend(["--context", context])
    cmd.extend(args)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        if result.returncode != 0:
            return False, result.stderr.strip() or "Unknown error"
        return True, result.stdout
    except subprocess.TimeoutExpired:
        return False, "Command timed out"
    except FileNotFoundError:
        return False, "kubectl not found in PATH"


def _check_nodes(context: Optional[str]) -> CheckResult:
    """Check node health."""
    success, output = _run_kubectl(
        ["get", "nodes", "-o", "json"],
        context=context
    )

    if not success:
        return CheckResult(
            name="k8s-nodes",
            status=Status.ERROR,
            message=f"Nodes: {output}"
        )

    try:
        data = json.loads(output)
        nodes = data.get("items", [])

        healthy = 0
        unhealthy = []

        for node in nodes:
            name = node.get("metadata", {}).get("name", "unknown")
            conditions = node.get("status", {}).get("conditions", [])

            # Check for Ready condition
            ready = False
            for cond in conditions:
                if cond.get("type") == "Ready":
                    ready = cond.get("status") == "True"
                    break

            if ready:
                healthy += 1
            else:
                unhealthy.append(name)

        total = len(nodes)

        if not unhealthy:
            return CheckResult(
                name="k8s-nodes",
                status=Status.OK,
                message=f"Nodes: {healthy}/{total} healthy"
            )
        else:
            result = CheckResult(
                name="k8s-nodes",
                status=Status.ERROR,
                message=f"Nodes: {healthy}/{total} healthy"
            )
            for node in unhealthy:
                result.add_detail(f"Not Ready: {node}")
            return result

    except json.JSONDecodeError:
        return CheckResult(
            name="k8s-nodes",
            status=Status.ERROR,
            message="Nodes: Failed to parse kubectl output"
        )


def _check_pods(context: Optional[str], config: dict) -> CheckResult:
    """Check pod health."""
    ignore_ns = config.get("ignore_namespaces", [])
    target_ns = config.get("namespaces", [])

    success, output = _run_kubectl(
        ["get", "pods", "-A", "-o", "json"],
        context=context
    )

    if not success:
        return CheckResult(
            name="k8s-pods",
            status=Status.ERROR,
            message=f"Pods: {output}"
        )

    try:
        data = json.loads(output)
        pods = data.get("items", [])

        running = 0
        problems = []

        for pod in pods:
            ns = pod.get("metadata", {}).get("namespace", "")
            name = pod.get("metadata", {}).get("name", "unknown")

            # Filter namespaces
            if ignore_ns and ns in ignore_ns:
                continue
            if target_ns and ns not in target_ns:
                continue

            phase = pod.get("status", {}).get("phase", "Unknown")
            container_statuses = pod.get("status", {}).get("containerStatuses", [])

            # Check for problems
            is_ok = phase in ("Running", "Succeeded")

            # Check for restart loops
            restart_count = 0
            waiting_reason = None
            for cs in container_statuses:
                restart_count += cs.get("restartCount", 0)
                waiting = cs.get("state", {}).get("waiting", {})
                if waiting:
                    waiting_reason = waiting.get("reason", "")

            if restart_count > 10:
                is_ok = False
            if waiting_reason in ("CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull"):
                is_ok = False

            if is_ok:
                running += 1
            else:
                reason = waiting_reason or phase
                if restart_count > 10:
                    reason = f"{reason} ({restart_count} restarts)"
                problems.append({
                    "ns": ns,
                    "name": name,
                    "reason": reason,
                })

        total = running + len(problems)

        if not problems:
            return CheckResult(
                name="k8s-pods",
                status=Status.OK,
                message=f"Pods: {running}/{total} running"
            )
        else:
            result = CheckResult(
                name="k8s-pods",
                status=Status.ERROR,
                message=f"Pods: {running}/{total} running"
            )
            for pod in problems[:10]:  # Limit to 10
                result.add_detail(f"{pod['ns']}/{pod['name']}: {pod['reason']}")
            if len(problems) > 10:
                result.add_detail(f"... and {len(problems) - 10} more")
            return result

    except json.JSONDecodeError:
        return CheckResult(
            name="k8s-pods",
            status=Status.ERROR,
            message="Pods: Failed to parse kubectl output"
        )


def _check_snapshots(context: Optional[str], config: dict) -> CheckResult:
    """Check for stale volume snapshots."""
    stale_hours = config.get("snapshot_stale_hours", 24)
    ignore_ns = config.get("ignore_namespaces", [])

    success, output = _run_kubectl(
        ["get", "volumesnapshots", "-A", "-o", "json"],
        context=context
    )

    if not success:
        if "the server doesn't have a resource type" in output.lower():
            return CheckResult(
                name="k8s-snapshots",
                status=Status.OK,
                message="Snapshots: VolumeSnapshots not available"
            )
        return CheckResult(
            name="k8s-snapshots",
            status=Status.ERROR,
            message=f"Snapshots: {output}"
        )

    try:
        data = json.loads(output)
        snapshots = data.get("items", [])

        now = datetime.now(timezone.utc)
        stale = []

        for snap in snapshots:
            ns = snap.get("metadata", {}).get("namespace", "")
            name = snap.get("metadata", {}).get("name", "unknown")

            if ignore_ns and ns in ignore_ns:
                continue

            # Check creation time from status
            creation_time = snap.get("status", {}).get("creationTime")
            if not creation_time:
                continue

            try:
                created = datetime.fromisoformat(creation_time.replace("Z", "+00:00"))
                age_hours = (now - created).total_seconds() / 3600

                if age_hours > stale_hours:
                    stale.append({
                        "ns": ns,
                        "name": name,
                        "age_hours": int(age_hours),
                    })
            except (ValueError, TypeError):
                continue

        if not stale:
            return CheckResult(
                name="k8s-snapshots",
                status=Status.OK,
                message=f"Snapshots: All within {stale_hours}h"
            )
        else:
            result = CheckResult(
                name="k8s-snapshots",
                status=Status.WARNING,
                message=f"Snapshots: {len(stale)} stale (>{stale_hours}h)"
            )
            for snap in sorted(stale, key=lambda s: s["age_hours"], reverse=True)[:5]:
                result.add_detail(f"{snap['ns']}/{snap['name']}: {snap['age_hours']}h old")
            return result

    except json.JSONDecodeError:
        return CheckResult(
            name="k8s-snapshots",
            status=Status.ERROR,
            message="Snapshots: Failed to parse kubectl output"
        )


def _check_cnpg(context: Optional[str], config: dict) -> CheckResult:
    """Check CNPG (CloudNativePG) cluster backup status."""
    ignore_ns = config.get("ignore_namespaces", [])

    success, output = _run_kubectl(
        ["get", "clusters.postgresql.cnpg.io", "-A", "-o", "json"],
        context=context
    )

    if not success:
        if "the server doesn't have a resource type" in output.lower():
            return CheckResult(
                name="k8s-cnpg",
                status=Status.OK,
                message="CNPG: Not installed"
            )
        return CheckResult(
            name="k8s-cnpg",
            status=Status.ERROR,
            message=f"CNPG: {output}"
        )

    try:
        data = json.loads(output)
        clusters = data.get("items", [])

        if not clusters:
            return CheckResult(
                name="k8s-cnpg",
                status=Status.OK,
                message="CNPG: No clusters found"
            )

        problems = []

        for cluster in clusters:
            ns = cluster.get("metadata", {}).get("namespace", "")
            name = cluster.get("metadata", {}).get("name", "unknown")

            if ignore_ns and ns in ignore_ns:
                continue

            status = cluster.get("status", {})

            # Check overall health
            phase = status.get("phase", "Unknown")
            if phase != "Cluster in healthy state":
                problems.append({
                    "ns": ns,
                    "name": name,
                    "issue": f"Phase: {phase}",
                })
                continue

            # Check backup status
            first_recov_point = status.get("firstRecoverabilityPoint")
            last_successful_backup = status.get("lastSuccessfulBackup")

            if not first_recov_point and not last_successful_backup:
                # Check if backups are configured
                backup_config = cluster.get("spec", {}).get("backup")
                if backup_config:
                    problems.append({
                        "ns": ns,
                        "name": name,
                        "issue": "No successful backups",
                    })

        if not problems:
            return CheckResult(
                name="k8s-cnpg",
                status=Status.OK,
                message=f"CNPG Backups: {len(clusters)} cluster(s) OK"
            )
        else:
            result = CheckResult(
                name="k8s-cnpg",
                status=Status.WARNING,
                message=f"CNPG: {len(problems)} issue(s)"
            )
            for p in problems:
                result.add_detail(f"{p['ns']}/{p['name']}: {p['issue']}")
            return result

    except json.JSONDecodeError:
        return CheckResult(
            name="k8s-cnpg",
            status=Status.ERROR,
            message="CNPG: Failed to parse kubectl output"
        )


def _check_resource_pressure(context: Optional[str], config: dict) -> CheckResult:
    """Check pods approaching resource limits."""
    mem_warn = config.get("pod_memory_warning_percent", 80)
    cpu_warn = config.get("pod_cpu_warning_percent", 80)
    ignore_ns = config.get("ignore_namespaces", [])

    # Get pod resource usage
    success, output = _run_kubectl(
        ["top", "pods", "-A", "--no-headers"],
        context=context
    )

    if not success:
        if "Metrics API not available" in output or "metrics" in output.lower():
            return CheckResult(
                name="k8s-resources",
                status=Status.OK,
                message="Resources: Metrics not available"
            )
        return CheckResult(
            name="k8s-resources",
            status=Status.ERROR,
            message=f"Resources: {output}"
        )

    # Get pod specs for limits
    success2, specs_output = _run_kubectl(
        ["get", "pods", "-A", "-o", "json"],
        context=context
    )

    if not success2:
        return CheckResult(
            name="k8s-resources",
            status=Status.ERROR,
            message="Resources: Failed to get pod specs"
        )

    try:
        specs_data = json.loads(specs_output)
        pod_limits = {}

        for pod in specs_data.get("items", []):
            ns = pod.get("metadata", {}).get("namespace", "")
            name = pod.get("metadata", {}).get("name", "")
            key = f"{ns}/{name}"

            containers = pod.get("spec", {}).get("containers", [])
            total_mem_limit = 0
            total_cpu_limit = 0

            for container in containers:
                limits = container.get("resources", {}).get("limits", {})

                # Parse memory limit
                mem_str = limits.get("memory", "")
                if mem_str:
                    if mem_str.endswith("Gi"):
                        total_mem_limit += int(float(mem_str[:-2]) * 1024)
                    elif mem_str.endswith("Mi"):
                        total_mem_limit += int(float(mem_str[:-2]))
                    elif mem_str.endswith("Ki"):
                        total_mem_limit += int(float(mem_str[:-2]) / 1024)

                # Parse CPU limit
                cpu_str = limits.get("cpu", "")
                if cpu_str:
                    if cpu_str.endswith("m"):
                        total_cpu_limit += int(cpu_str[:-1])
                    else:
                        total_cpu_limit += int(float(cpu_str) * 1000)

            if total_mem_limit > 0 or total_cpu_limit > 0:
                pod_limits[key] = {
                    "mem_limit_mi": total_mem_limit,
                    "cpu_limit_m": total_cpu_limit,
                }

        # Parse top output and check against limits
        warnings = []

        for line in output.strip().split("\n"):
            if not line:
                continue
            parts = line.split()
            if len(parts) < 4:
                continue

            ns, name, cpu_str, mem_str = parts[0], parts[1], parts[2], parts[3]
            key = f"{ns}/{name}"

            if ignore_ns and ns in ignore_ns:
                continue

            if key not in pod_limits:
                continue

            limits = pod_limits[key]

            # Parse current usage
            current_cpu = 0
            if cpu_str.endswith("m"):
                current_cpu = int(cpu_str[:-1])

            current_mem = 0
            if mem_str.endswith("Mi"):
                current_mem = int(mem_str[:-2])
            elif mem_str.endswith("Gi"):
                current_mem = int(float(mem_str[:-2]) * 1024)

            # Check percentages
            mem_pct = 0
            if limits["mem_limit_mi"] > 0:
                mem_pct = (current_mem / limits["mem_limit_mi"]) * 100

            cpu_pct = 0
            if limits["cpu_limit_m"] > 0:
                cpu_pct = (current_cpu / limits["cpu_limit_m"]) * 100

            if mem_pct >= mem_warn:
                warnings.append(f"{key}: {int(mem_pct)}% memory limit")
            elif cpu_pct >= cpu_warn:
                warnings.append(f"{key}: {int(cpu_pct)}% CPU limit")

        if not warnings:
            return CheckResult(
                name="k8s-resources",
                status=Status.OK,
                message="Resources: No pressure"
            )
        else:
            result = CheckResult(
                name="k8s-resources",
                status=Status.WARNING,
                message=f"Resources: {len(warnings)} pod(s) under pressure"
            )
            for w in warnings[:5]:
                result.add_detail(w)
            if len(warnings) > 5:
                result.add_detail(f"... and {len(warnings) - 5} more")
            return result

    except (json.JSONDecodeError, ValueError):
        return CheckResult(
            name="k8s-resources",
            status=Status.ERROR,
            message="Resources: Failed to parse data"
        )


def check_kubernetes(config: dict) -> list[CheckResult]:
    """
    Run all Kubernetes health checks.

    Args:
        config: Kubernetes config section

    Returns:
        List of CheckResult objects
    """
    context = config.get("context") or None
    thresholds = config.get("thresholds", {})

    # Merge thresholds into config for individual checks
    check_config = {**config, **thresholds}

    results = []

    # Run all checks
    results.append(_check_nodes(context))
    results.append(_check_pods(context, check_config))
    results.append(_check_snapshots(context, check_config))
    results.append(_check_cnpg(context, check_config))
    results.append(_check_resource_pressure(context, check_config))

    return results
