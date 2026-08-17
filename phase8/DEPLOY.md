# Phase 8 Deployment Runbook

Follow this in order. Steps are numbered because the order genuinely
matters -- applying `service/` before the container image exists will
fail, and running these out of sequence is the most likely way this
goes wrong.

None of this has been run by Claude -- no Docker, Terraform, or gcloud
available in the environment that wrote it. This is the first live test
of every command below.

## 0. Prerequisites

```bash
gcloud auth application-default login
gcloud config set project bq-vertex-analyst
```

Get the two values the budget alert needs:
```bash
gcloud billing projects describe bq-vertex-analyst --format="value(billingAccountName)"
gcloud projects describe bq-vertex-analyst --format="value(projectNumber)"
```

## 1. Apply the PERMANENT budget alert first (once, ever)

```bash
cd phase8/terraform/budget-alert
terraform init
terraform validate
terraform plan \
  -var="billing_account_id=YOUR_BILLING_ACCOUNT_ID_WITHOUT_PREFIX" \
  -var="project_number=YOUR_PROJECT_NUMBER"
terraform apply \
  -var="billing_account_id=YOUR_BILLING_ACCOUNT_ID_WITHOUT_PREFIX" \
  -var="project_number=YOUR_PROJECT_NUMBER"
```

Confirm in the GCP Console (Billing > Budgets & alerts) that the budget
actually appears before moving on. **Do not run `terraform destroy` in
this directory as part of any later cleanup step -- this one stays.**

## 2. Enable required APIs (one-time, if not already on)

```bash
gcloud services enable run.googleapis.com \
    artifactregistry.googleapis.com \
    cloudbuild.googleapis.com \
    --project=bq-vertex-analyst
```

## 3. Provision the Artifact Registry repository

```bash
cd ../service
terraform init
terraform validate
```

This first `apply` will create the empty repository the image gets
pushed into -- the Cloud Run resource in this same file will fail at
this point since `container_image` doesn't exist yet. That's expected;
apply just the repository:

```bash
terraform apply -target=google_artifact_registry_repository.repo \
  -var="container_image=placeholder"
```

## 4. Build and push the container image

```bash
cd ../../..   # back to repo root, where the Dockerfile lives
gcloud builds submit \
  --tag us-central1-docker.pkg.dev/bq-vertex-analyst/bq-vertex-analyst/api:latest
```

This uses Cloud Build rather than local `docker build` -- avoids needing
Docker installed locally and matches the image architecture Cloud Run
expects without cross-platform build concerns.

## 5. Apply the full service state

```bash
cd phase8/terraform/service
terraform plan \
  -var="container_image=us-central1-docker.pkg.dev/bq-vertex-analyst/bq-vertex-analyst/api:latest"
terraform apply \
  -var="container_image=us-central1-docker.pkg.dev/bq-vertex-analyst/bq-vertex-analyst/api:latest"
```

Note the `service_url` output.

## 6. Verify live -- actually test it, don't just check "no errors"

```bash
curl "$(terraform output -raw service_url)/health"
curl -X POST "$(terraform output -raw service_url)/schema"
curl -X POST "$(terraform output -raw service_url)/answer" \
  -H "Content-Type: application/json" \
  -d '{"question": "What is the average order value by state?"}'
```

Check specifically:
- Does `/health` return quickly? Note the response time for the FIRST
  request after deployment -- this is the cold-start case
  (`min_instance_count = 0` means the container wasn't already running).
- Does a second, immediate request come back faster? That's the warm
  case. Record both numbers honestly in NOTES.md -- this is real,
  measurable evidence of the scale-to-zero tradeoff, not something to
  gloss over.
- Does `/answer` on the cardinality-violation question still correctly
  block, same as every local and CI run before it?

## 7. Tear down the EPHEMERAL state only

```bash
cd phase8/terraform/service
terraform destroy \
  -var="container_image=us-central1-docker.pkg.dev/bq-vertex-analyst/bq-vertex-analyst/api:latest"
```

Confirm in the GCP Console that:
- The Cloud Run service is gone
- The `bq-vertex-analyst-run` service account is gone
- The Artifact Registry repository (and the image inside it) is gone

**Do NOT run `terraform destroy` inside `budget-alert/`.** That state is
permanent by design -- destroying it removes the one guardrail that
would catch a cost problem in anything else in this project, Cloud Run
or otherwise.

## 8. Confirm nothing is still running or billing

```bash
gcloud run services list --project=bq-vertex-analyst
gcloud artifacts repositories list --project=bq-vertex-analyst
```

Both should come back empty (or not list `bq-vertex-analyst-api` /
`bq-vertex-analyst`). Paste the actual output as the final piece of
evidence for NOTES.md -- "I tore it down" and "confirmed empty via the
CLI" are different claims, and this project has consistently preferred
the second one.
