# Phase 8 Notes

Cloud Run + Terraform deployment. Decision made deliberately, not
defaulted: deploy, verify live, then destroy the ephemeral pieces --
one-time cost, fully documented, nothing left running afterward. The
budget alert is the one exception, kept permanently in its own separate
Terraform state specifically so tearing down the demo service can never
accidentally remove the cost guardrail.

## Finding: a generic 400 error, resolved by systematic bisection, not repeated guessing

First `terraform apply` on `budget-alert/` failed with a `403`
naming a Google-internal project number as the "consumer" -- not
`bq-vertex-analyst`. After fixing that, it failed again with a
completely uninformative `400: Request contains an invalid argument` --
no field name, no hint. Confirmed via `TF_LOG=DEBUG` that this wasn't a
log-truncation issue; the API's actual response body genuinely contained
nothing more specific.

Resolved by working outward from the error instead of guessing at fixes
one at a time:
1. Captured the raw HTTP request Terraform actually sent via debug
   logging -- confirmed the URL, headers, and JSON body were all
   correctly formed against the documented API shape.
2. Reproduced the identical failure via `gcloud billing budgets create`
   directly, bypassing Terraform entirely -- same generic error from a
   structurally different client, which ruled out a Terraform-specific
   bug and pointed at either the billing account itself or the request
   content.
3. Bisected the request by stripping it to the two required fields only
   (no project filter, no threshold rules) -- still failed identically,
   ruling out `budget_filter` and `threshold_rules` as the cause.
4. Tested the one remaining hypothesis directly: `gcloud billing
   accounts describe` showed `currencyCode: EUR`. The original HCL
   defaulted to `USD`. Retried via `gcloud` with `--budget-amount=10EUR`
   -- succeeded immediately.

Root cause: **a budget's currency must match the billing account's own
configured currency**, and the API's error message for this specific
mismatch gives zero indication that currency is the problem. Fixed by
adding an explicit `budget_amount_currency` variable (default `"EUR"`,
documented as needing verification against the actual account before
reuse on a different project) instead of a silently wrong hardcoded
`USD` default that neither Claude nor Ben checked against the real
account before the first attempt.

## Finding: gcloud user ADC credentials need an explicit quota project override

Before the currency issue, the very first `apply` attempt failed with a
`403`/`SERVICE_DISABLED` naming a Google-internal project number
(`764086051850`) as the "consumer." This is a documented gap between how
`gcloud` itself resolves a quota project (via `gcloud auth
application-default set-quota-project`) and how Terraform's `google`
provider does -- it does not automatically read that setting. Fixed by
adding `user_project_override = true` and `billing_project =
var.project_id` directly to the provider block -- confirmed via the
debug log's actual request headers (`X-Goog-User-Project:
bq-vertex-analyst`) that this was genuinely applied, not just assumed to
have worked because the next error happened to be different.

## Verified applied for real, confirmed independently, not just trusted from Terraform's own state

`terraform apply` reported `Creation complete`, real budget ID
(`billingAccounts/01901A-D07F42-AA26DC/budgets/9e6ee513-...`). Confirmed
separately in the GCP Console (Billing > Budgets & alerts) that exactly
the expected budget exists. Also found, investigated, and correctly left
alone an unrelated pre-existing account-wide budget (scoped to "All
projects (2), All services (1777)") that predates this project --
confirmed via its scope settings that it doesn't conflict with or
duplicate the new one, rather than assuming that from its name alone.

## Service deployment (Cloud Run, Artifact Registry) -- status

Not yet started as of this note. See phase8/DEPLOY.md for the full
runbook. Will be appended here once live-verified, following the same
discipline as every phase before this one: documented after real
evidence, not before.
