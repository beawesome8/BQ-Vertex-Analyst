# BQ-Vertex-Analyst -- container image for the Phase 5 FastAPI service
#
# Placed at REPO ROOT, deliberately breaking the per-phase-folder
# convention used everywhere else in this project. Reason: Docker's
# build context is whatever directory you run `docker build` from, and
# this image needs phase1/ through phase5/ plus requirements.txt all
# accessible in that context -- keeping it inside phase8/ would require
# a nonstandard `-f phase8/Dockerfile .` invocation with the context set
# to the repo root anyway, so placing it where the context naturally is
# avoids a confusing mismatch between "where the file lives" and "what
# directory it actually builds from."
#
# Build from the repo root:
#   docker build -t bq-vertex-analyst-api .
#
# NOT verified by build -- no Docker daemon available in the environment
# that wrote this file. First real build is yours.

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Only copying what the deployed service actually needs at runtime --
# phase6/ (eval harness) and phase7/ (CI config) are deliberately
# excluded, since they're development/CI-time concerns, not something
# the live service itself imports or executes.
COPY phase1/ ./phase1/
COPY phase2/ ./phase2/
COPY phase3/ ./phase3/
COPY phase4/ ./phase4/
COPY phase5/ ./phase5/

# Cloud Run injects the PORT environment variable (8080 by default) and
# expects the container to listen on THAT port, not a hardcoded one --
# a container that ignores $PORT gets marked unhealthy and never
# receives traffic, regardless of whether the app itself works fine.
ENV PORT=8080
EXPOSE 8080

CMD exec uvicorn phase5.service:app --host 0.0.0.0 --port ${PORT}
