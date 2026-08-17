# BQ-Vertex-Analyst -- Phase 8: PERMANENT cost guardrail
#
# Deliberately its OWN Terraform state, separate from ../service/. Apply
# this ONCE and leave it in place indefinitely -- it protects against
# ANY runaway GCP cost across this entire project (BigQuery, Vertex AI,
# Cloud Run, everything), not just whatever Phase 8 happens to deploy.
# Keeping it in a separate state means `terraform destroy` on the
# ephemeral service/ deployment can never touch this by accident.
#
# NOT VERIFIED -- no Terraform binary available in the environment that
# wrote this file. Run `terraform validate` yourself before applying.

terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

# user_project_override + billing_project: required when authenticating
# via `gcloud auth application-default login` user credentials (as
# opposed to a service account). gcloud's own quota-project setting is
# NOT automatically picked up by Terraform's provider -- this is a
# documented gap, confirmed via a real 403 error on the first apply
# attempt, not assumed preemptively.
provider "google" {
  project                = var.project_id
  user_project_override  = true
  billing_project        = var.project_id
}

resource "google_billing_budget" "cost_alert" {
  billing_account = var.billing_account_id
  display_name    = "bq-vertex-analyst cost alert"

  budget_filter {
    projects = ["projects/${var.project_number}"]
  }

  amount {
    specified_amount {
      # MUST match the billing account's own configured currency, or the
      # API rejects with a generic 400 that names no specific field.
      # Confirmed via `gcloud billing accounts describe` after a real
      # failed apply defaulted to USD against a EUR-configured account --
      # see variable description in variables.tf for the full story.
      currency_code = var.budget_amount_currency
      units         = var.budget_amount
    }
  }

  # Multiple thresholds so a notification fires well before the full
  # budget is actually spent, not only after the fact.
  threshold_rules {
    threshold_percent = 0.5
  }
  threshold_rules {
    threshold_percent = 0.9
  }
  threshold_rules {
    threshold_percent = 1.0
  }
}
