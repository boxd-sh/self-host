"""The boxd Cloud Monitoring dashboard on GKE ([gke] observability): PromQL
over Managed Service for Prometheus, GKE's node metrics and a logs panel.

Every boxd series comes from the PodMonitorings kube_render renders (control
9101, proxy 9102, dns 9103, worker 9104). boxd's latency metrics are
Prometheus summaries (a `quantile` label), not histograms. terraform/gke
creates the dashboard from render()'s JSON (google_monitoring_dashboard)."""
import json

COLS = 48


def promql(query: str, legend: str = "", unit: str = "") -> dict:
    q = {"prometheusQuery": query}
    if unit:
        q["unitOverride"] = unit
    ds = {"timeSeriesQuery": q, "plotType": "LINE", "targetAxis": "Y1"}
    if legend:
        ds["legendTemplate"] = legend
    return ds


def chart(title: str, *datasets: dict, stacked: bool = False) -> dict:
    sets = []
    for ds in datasets:
        ds = dict(ds)
        if stacked:
            ds["plotType"] = "STACKED_AREA"
        sets.append(ds)
    return {"title": title, "xyChart": {"dataSets": sets, "yAxis": {"scale": "LINEAR"}}}


def score(title: str, query: str, unit: str = "") -> dict:
    q = {"prometheusQuery": query}
    if unit:
        q["unitOverride"] = unit
    return {"title": title, "scorecard": {"timeSeriesQuery": q, "sparkChartView": {"sparkChartType": "SPARK_LINE"}}}


def header(title: str, subtitle: str) -> dict:
    return {"title": title, "sectionHeader": {"subtitle": subtitle, "dividerBelow": True}}


def render(ns: str, project: str, cluster: str) -> dict:
    s = f'namespace="{ns}"'
    rows: list[tuple[int, list[dict]]] = []  # (height, widgets in the row, equal widths)

    rows.append((4, [header("Cluster", f"boxd in namespace {ns}: roles, Raft leader, machines, license")]))
    rows.append((8, [
        score("Roles up", f"sum(up{{{s}}})"),
        score("Raft leader (node id)", f"max(boxd_raft_leader_id{{{s},pod=~\"boxd-control-.*\"}})"),
        score("Machines running", f'sum(boxd_worker_vm_count{{{s},status="running"}})'),
        score("Machines hibernated", f'sum(boxd_worker_vm_count{{{s},status="hibernated"}})'),
        score("Worker vCPUs (license counts)", f"max(boxd_license_worker_vcpus{{{s}}})"),
        score("License days left", f"min(boxd_license_days_left{{{s}}})"),
    ]))
    rows.append((14, [
        chart("Roles up", promql(f"sum by (job) (up{{{s}}})", "${labels.job}")),
        chart("Machines by state",
              promql(f'sum by (status) (boxd_worker_vm_count{{{s},status!="destroyed"}})', "${labels.status}"),
              stacked=True),
        chart("Running machines per worker",
              promql(f'sum by (pod) (boxd_worker_vm_count{{{s},status="running"}})', "${labels.pod}")),
    ]))

    rows.append((4, [header("Raft", "Every member's replica: applied index, lag behind the furthest member, leader changes, proposal latency")]))
    rows.append((14, [
        chart("Applied index", promql(f"max by (pod) (boxd_raft_applied_index{{{s}}})", "${labels.pod}")),
        chart("Replication lag (entries behind the furthest member)",
              promql(f"scalar(max(boxd_raft_applied_index{{{s}}})) - max by (pod) (boxd_raft_applied_index{{{s}}})",
                     "${labels.pod}")),
        chart("Leader changes (1 h)", promql(f"sum(increase(boxd_raft_leader_changes_total{{{s}}}[1h]))", "changes")),
    ]))
    rows.append((14, [
        # Summaries (precomputed quantiles per process): the worst process, per phase.
        chart("Proposal latency p50 / p99 (worst member)",
              promql(f'max by (phase) (boxd_raft_proposal_seconds{{{s},quantile="0.5"}})', "p50 ${labels.phase}", "s"),
              promql(f'max by (phase) (boxd_raft_proposal_seconds{{{s},quantile="0.99"}})', "p99 ${labels.phase}", "s")),
        chart("Applies per second by command (top 10)",
              promql(f"topk(10, sum by (command) (rate(boxd_raft_apply_total{{{s},pod=~\"boxd-control-.*\"}}[5m])))",
                     "${labels.command}"), stacked=True),
        chart("Apply time p99 by command (top 10)",
              promql(f'topk(10, max by (command) (boxd_raft_apply_seconds{{{s},pod=~"boxd-control-.*",quantile="0.99"}}))',
                     "${labels.command}", "s")),
    ]))

    rows.append((4, [header("Workers", "Host memory, placement verdict, VMM processes and warm slots, traffic, hibernation")]))
    rows.append((14, [
        chart("Host memory used",
              promql(f"max by (pod) (boxd_worker_memory_used_bytes{{{s}}}) / max by (pod) (boxd_worker_memory_total_bytes{{{s}}})",
                     "${labels.pod}", "10^2.%")),
        chart("Placement accepting (1 = takes new machines)",
              promql(f"max by (pod) (boxd_worker_placement_accepting{{{s}}})", "${labels.pod}")),
        chart("VMM processes and idle warm slots",
              promql(f"max by (pod) (boxd_worker_vmm_processes{{{s}}})", "${labels.pod} vmms"),
              promql(f"max by (pod) (boxd_worker_vmm_pool_idle{{{s}}})", "${labels.pod} warm idle")),
    ]))
    rows.append((14, [
        chart("Machine traffic (all taps)",
              promql(f"sum by (pod) (rate(boxd_worker_tap_bytes_total{{{s}}}[5m]))", "${labels.pod}", "By/s")),
        chart("Hibernation: queued dumps, RAM uploads pending",
              promql(f"max by (pod) (boxd_worker_hibernate_admission_queued{{{s}}})", "${labels.pod} queued"),
              promql(f"max by (pod) (boxd_worker_hibernation_blobs_pending{{{s}}})", "${labels.pod} uploads pending")),
        chart("VMM exits and spawn attempts (1 h)",
              promql(f"sum by (pod) (increase(boxd_worker_vmm_exit_total{{{s}}}[1h]))", "${labels.pod} exits"),
              promql(f"sum by (pod) (increase(boxd_worker_vmm_spawn_attempts_total{{{s}}}[1h]))", "${labels.pod} spawns")),
    ]))

    rows.append((4, [header("Machines", "Top 10 machines by CPU, memory, disk and network")]))
    rows.append((14, [
        chart("CPU (top 10)", promql(f'topk(10, max by (vm_name) (boxd_vm_cpu_pct{{{s},status="running"}}))', "${labels.vm_name}", "%")),
        chart("Guest memory (top 10)", promql(f"topk(10, max by (vm_name) (boxd_vm_memory_anon_bytes{{{s}}}))", "${labels.vm_name}", "By")),
    ]))
    rows.append((14, [
        chart("Disk used (top 10)", promql(f"topk(10, max by (vm_name) (boxd_vm_disk_bytes{{{s}}}))", "${labels.vm_name}", "By")),
        chart("Packets into the machine (top 10)",
              promql(f"topk(10, max by (vm_name) (boxd_vm_tap_into_vm_packets{{{s}}}))", "${labels.vm_name}")),
    ]))

    rows.append((4, [header("GKE nodes", "GKE's system metrics for the cluster's nodes")]))
    rows.append((14, [
        chart("Node CPU (allocatable used)",
              promql(f'max by (node_name) (kubernetes_io:node_cpu_allocatable_utilization{{monitored_resource="k8s_node",cluster_name="{cluster}"}})',
                     "${labels.node_name}", "10^2.%")),
        chart("Node memory (allocatable used, non-evictable)",
              promql(f'max by (node_name) (kubernetes_io:node_memory_allocatable_utilization{{monitored_resource="k8s_node",cluster_name="{cluster}",memory_type="non-evictable"}})',
                     "${labels.node_name}", "10^2.%")),
    ]))

    rows.append((4, [header("Logs", "Warnings and errors from boxd's pods (Cloud Logging, workload logs)")]))
    rows.append((18, [{
        "title": "boxd warnings and errors",
        "logsPanel": {
            "filter": f'resource.type="k8s_container" AND resource.labels.namespace_name="{ns}" '
                      f'AND resource.labels.cluster_name="{cluster}" AND (severity>=WARNING OR textPayload=~" (WARN|ERROR) ")',
            "resourceNames": [f"projects/{project}"],
        },
    }]))

    tiles, y = [], 0
    for height, widgets in rows:
        width = COLS // len(widgets)
        for i, w in enumerate(widgets):
            tile = {"width": width, "height": height, "widget": w}
            # The API leaves out zero positions: a tile with xPos/yPos 0 came back without
            # them, and terraform showed the dashboard as changed on every apply.
            if i * width:
                tile["xPos"] = i * width
            if y:
                tile["yPos"] = y
            tiles.append(tile)
        y += height
    return {
        "displayName": f"boxd on GKE ({cluster}/{ns})",
        "mosaicLayout": {"columns": COLS, "tiles": tiles},
        "labels": {"boxd": ""},
    }


def dashboard_json(ns: str, project: str, cluster: str) -> str:
    """The dashboard as terraform's dashboard_json takes it."""
    return json.dumps(render(ns, project, cluster), sort_keys=True)
