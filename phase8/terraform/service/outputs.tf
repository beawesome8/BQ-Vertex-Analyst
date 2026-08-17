output "service_url" {
  value       = google_cloud_run_v2_service.api.uri
  description = "Live URL once deployed. Test /health here first, before anything else."
}

output "runtime_service_account" {
  value       = google_service_account.cloud_run_runtime.email
  description = "Confirm this is NOT the same account as Phase 7's CI account (bq-vertex-analyst-ci)."
}
