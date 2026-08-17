# BQ-Vertex-Analyst -- Phase 8: ephemeral demo infrastructure
#
# EPHEMERAL BY DESIGN. This state is meant to be applied, verified live,
# then destroyed -- see phase8/DEPLOY.md for the full runbook. It does
# NOT include the budget alert (that lives in ../budget-alert/, its own
# separate state) specifically so that destroying this state can never
# accidentally remove the cost guardrail.
#
# NOT VERIFIED by `terraform validate` or `terraform plan` -- no
# Terraform binary was available in the environment that wrote this
# file. Run both before `apply`, don't trust this file blindly.

terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

# user_project_override + billing_project: required when authenticating
# via `gcloud auth application-default login` user credentials. Applied
# here preemptively -- this exact gap caused a real 403 on the first
# apply attempt in ../budget-alert/ (same project, same ADC credentials,
# same underlying cause), documented in phase8/NOTES.md.
provider "google" {
  project                = var.project_id
  region                  = var.region
  user_project_override  = true
  billing_project        = var.project_id
}

# Terraform provisions the REPOSITORY, not the image inside it.
# Building and pushing the container image is a separate step (gcloud
# builds submit, or docker build + push) that must happen BEFORE
# `terraform apply`, since the Cloud Run resource below needs an image
# URI that already exists -- Terraform cannot build Docker images itself.
resource "google_artifact_registry_repository" "repo" {
  location      = var.region
  repository_id = "bq-vertex-analyst"
  format        = "DOCKER"
  description   = "Container images for the BQ-Vertex-Analyst demo API"
}

# Dedicated runtime identity for the deployed service -- deliberately
# SEPARATE from Phase 7's CI service account (bq-vertex-analyst-ci).
# The CI account authenticates GitHub Actions to run the eval harness;
# this one authenticates a live, publicly-reachable web service. Keeping
# them apart means compromising one doesn't hand over the permissions of
# the other -- least privilege scoped per workload, not one shared
# identity doing everything GCP-related in this project.
resource "google_service_account" "cloud_run_runtime" {
  account_id   = "bq-vertex-analyst-run"
  display_name = "BQ-Vertex-Analyst Cloud Run runtime (least privilege)"
}

resource "google_project_iam_member" "bq_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.cloud_run_runtime.email}"
}

resource "google_project_iam_member" "bq_data_viewer" {
  project = var.project_id
  role    = "roles/bigquery.dataViewer"
  member  = "serviceAccount:${google_service_account.cloud_run_runtime.email}"
}

resource "google_project_iam_member" "vertex_ai_user" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.cloud_run_runtime.email}"
}

resource "google_cloud_run_v2_service" "api" {
  name     = "bq-vertex-analyst-api"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.cloud_run_runtime.email

    containers {
      image = var.container_image

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }
    }

    scaling {
      # min_instance_count = 0 is the actual cost-control decision here:
      # the service scales to zero and costs nothing while idle, rather
      # than staying warm around the clock. The honest tradeoff is
      # cold-start latency on the first request after a period of no
      # traffic -- documented in NOTES.md as an observed number, not
      # hidden as a downside of this choice.
      min_instance_count = 0
      max_instance_count = 2
    }
  }
}

# DELIBERATE, EXPLICIT choice, same category as Phase 5's wide-open CORS
# policy: public, unauthenticated access. Reasonable for a short
# verification window on a demo with no real user data behind it --
# NOT something to leave enabled indefinitely, which is exactly why this
# whole state is meant to be destroyed after verification, not left
# running with this binding in place long-term.
resource "google_cloud_run_v2_service_iam_member" "public_access" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.api.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
