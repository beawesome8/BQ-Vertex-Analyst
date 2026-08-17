variable "project_id" {
  type    = string
  default = "bq-vertex-analyst"
}

variable "billing_account_id" {
  type        = string
  description = <<-EOT
    From: gcloud billing projects describe bq-vertex-analyst --format="value(billingAccountName)"
    Strip the "billingAccounts/" prefix -- pass only the ID portion,
    e.g. "01901A-D07F42-AA26DC".
  EOT
}

variable "project_number" {
  type        = string
  description = "From: gcloud projects describe bq-vertex-analyst --format=\"value(projectNumber)\""
}

variable "budget_amount_currency" {
  type        = string
  default     = "EUR"
  description = <<-EOT
    MUST match the billing account's own configured currency, or the
    API rejects the request with a generic "Request contains an
    invalid argument" -- no field-level detail pointing at currency
    specifically. Confirmed via `gcloud billing accounts describe` on
    this project's actual account (currencyCode: EUR) after a failed
    apply attempt defaulted to USD. Check your own account's currency
    before assuming EUR is correct for a different project:
      gcloud billing accounts describe YOUR_BILLING_ACCOUNT_ID
  EOT
}

variable "budget_amount" {
  type    = number
  default = 10
}
