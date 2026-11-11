# [gke] observability: the Cloud Monitoring dashboard the installer renders
# (gke_dashboard.py): PromQL over Managed Service for Prometheus (the
# PodMonitorings), GKE's node metrics and a logs panel.
resource "google_monitoring_dashboard" "boxd" {
  count          = var.dashboard_json != "" ? 1 : 0
  dashboard_json = var.dashboard_json
  depends_on     = [google_project_service.services]
}
