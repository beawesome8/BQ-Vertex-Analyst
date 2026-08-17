variable "project_id" {
  type    = string
  default = "bq-vertex-analyst"
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "container_image" {
  type        = string
  description = "Full image URI in Artifact Registry, e.g. us-central1-docker.pkg.dev/bq-vertex-analyst/bq-vertex-analyst/api:latest -- must exist BEFORE terraform apply, since Terraform does not build images."
}
