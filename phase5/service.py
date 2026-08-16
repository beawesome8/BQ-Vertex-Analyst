"""
BQ-Vertex-Analyst -- Phase 5: FastAPI Service
================================================

Wraps agent_core.py's compiled LangGraph (generate -> grounding gate ->
conditional execution -> grounded answer) as an HTTP API.

Endpoints
---------
    GET  /health              -- liveness check, no agent invocation
    GET  /schema               -- table/column list from schema_profile.json
    GET  /sample/{table_name}  -- live LIMIT preview of real row data
    POST /answer               -- {"question": "..."} -> full pipeline result
    POST /suggest               -- ranked analytical questions, no body required

CRITICAL DESIGN CHOICE, verified not assumed: both /answer and /suggest
are defined as plain `def`, NOT `async def`. The underlying Gemini and
BigQuery clients used throughout this project are synchronous. An
`async def` endpoint calling a blocking client stalls FastAPI's single
event loop for the full duration of every LLM call and BigQuery
round-trip, serializing ALL concurrent requests regardless of how many
users hit the API at once. A plain `def` endpoint is automatically run
in a thread pool by Starlette, so concurrent requests genuinely run in
parallel. Measured this directly before writing this file: 5 concurrent
requests to a sync endpoint with a 0.5s blocking call completed in 0.54s
total; the same test against an async endpoint calling the same blocking
call took 2.51s (fully serialized). See phase5/NOTES.md for the test.

CORS is wide open (allow_origins=["*"]) -- explicitly a local-demo-only
choice, not appropriate for any real deployment. Flagged here so it's
never mistaken for a production-ready default.

Scope boundary: synchronous request/response JSON only. No streaming, no
websockets, no auth. This is a demo service for a portfolio project, not
a hardened API -- deliberate, not an oversight.

Prerequisites
-------------
    pip install fastapi uvicorn

Usage
-----
    # From the repo root:
    python -m uvicorn phase5.service:app --reload --port 8000

    # Then:
    curl http://127.0.0.1:8000/health
    curl -X POST http://127.0.0.1:8000/answer -H "Content-Type: application/json" \\
        -d '{"question": "What is the average order value by state?"}'
    curl -X POST http://127.0.0.1:8000/suggest
"""

from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import json
from pathlib import Path

from phase2.agent_core import build_graph, AgentState, PROJECT_ID

SCHEMA_PROFILE_PATH = Path(__file__).parent.parent / "phase1" / "schema_profile.json"
DATASET = "bigquery-public-data.thelook_ecommerce"  # must match phase1/phase3/phase4

app = FastAPI(title="BQ-Vertex-Analyst API", version="0.5.0")

# Local-demo-only CORS policy. Do not carry this into any real deployment.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Compiled once at import time, reused across requests -- rebuilding the
# graph per-request would be wasted work with no benefit, since the graph
# structure itself never changes between calls.
_graph = build_graph()


class AnswerRequest(BaseModel):
    question: str


class AnswerResponse(BaseModel):
    sql: Optional[str] = None
    explanation: Optional[str] = None
    caveats: list = []
    gate_passed: Optional[bool] = None
    gate_blocking: list = []
    execution_row_count: Optional[int] = None
    execution_bytes_billed: Optional[int] = None
    grounded_answer: Optional[str] = None
    cited_values: list = []
    hallucination_passed: Optional[bool] = None
    warnings: list = []


class SuggestResponse(BaseModel):
    questions: list = []
    gate_passed: Optional[bool] = None
    warnings: list = []


def _initial_state(mode: str, question: Optional[str]) -> AgentState:
    return {
        "mode": mode,
        "nl_question": question,
        "schema_profile": None,
        "schema_context": None,
        "raw_response": None,
        "result": None,
        "warnings": [],
        "gate_passed": None,
        "gate_blocking": [],
        "gate_dry_run_bytes": None,
        "execution_row_count": None,
        "execution_bytes_billed": None,
        "grounded_answer": None,
        "cited_values": [],
        "hallucination_passed": None,
    }


@app.get("/health")
def health():
    return {"status": "ok"}


def _load_schema_profile() -> list:
    with open(SCHEMA_PROFILE_PATH) as f:
        return json.load(f)


@app.get("/schema")
def get_schema():
    """
    Lets a user see what tables/columns actually exist before asking a
    question -- without this, a demo user has no way to know what's a
    reasonable thing to ask. Read from the same schema_profile.json every
    other phase uses; no live BigQuery call needed for this one.
    """
    profile = _load_schema_profile()
    return {
        "tables": [
            {
                "table": t["table"],
                "row_count": t["row_count"],
                "columns": [
                    {
                        "name": c["name"],
                        "type": c["field_type"],
                        "cardinality_reliable": c.get("cardinality_reliable", True),
                        "inferred_fk_target": c.get("inferred_fk_target"),
                    }
                    for c in t["columns"]
                ],
            }
            for t in profile
        ]
    }


@app.get("/sample/{table_name}")
def get_sample(table_name: str, limit: int = 10):
    """
    Live LIMIT query so a user can see real example values, not just
    column names/types -- schema alone often isn't enough to know what a
    reasonable question looks like.

    SAFETY: table_name comes from the request path, so it's validated
    against the real table allowlist (from schema_profile.json) BEFORE it
    ever touches a SQL string, not after. This isn't agent-generated SQL
    running through the Phase 3 gate -- it's a raw endpoint parameter, so
    it gets its own, simpler, independent check: only exact matches
    against known real tables are ever interpolated into a query.
    """
    profile = _load_schema_profile()
    known_tables = {t["table"] for t in profile}
    if table_name not in known_tables:
        raise HTTPException(status_code=404, detail=f"Unknown table '{table_name}'")

    limit = min(max(limit, 1), 50)  # hard cap regardless of what the caller asks for

    from google.cloud import bigquery
    query = f"SELECT * FROM `{DATASET}.{table_name}` LIMIT {limit}"

    try:
        client = bigquery.Client(project=PROJECT_ID)
        job = client.query(query)
        rows = [dict(row.items()) for row in job.result()]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sample query failed: {e}")

    return {"table": table_name, "rows": rows}


@app.post("/answer", response_model=AnswerResponse)
def answer_question(req: AnswerRequest):
    if not req.question or not req.question.strip():
        raise HTTPException(status_code=422, detail="question must not be empty")

    try:
        final_state = _graph.invoke(_initial_state("answer_question", req.question))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Agent pipeline failed: {e}")

    result = final_state.get("result") or {}
    return AnswerResponse(
        sql=result.get("sql"),
        explanation=result.get("explanation"),
        caveats=result.get("caveats", []),
        gate_passed=final_state.get("gate_passed"),
        gate_blocking=final_state.get("gate_blocking", []),
        execution_row_count=final_state.get("execution_row_count"),
        execution_bytes_billed=final_state.get("execution_bytes_billed"),
        grounded_answer=final_state.get("grounded_answer"),
        cited_values=final_state.get("cited_values", []),
        hallucination_passed=final_state.get("hallucination_passed"),
        warnings=final_state.get("warnings", []),
    )


@app.post("/suggest", response_model=SuggestResponse)
def suggest_questions():
    try:
        final_state = _graph.invoke(_initial_state("suggest_questions", None))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Agent pipeline failed: {e}")

    result = final_state.get("result") or {}
    return SuggestResponse(
        questions=result.get("questions", []),
        gate_passed=final_state.get("gate_passed"),
        warnings=final_state.get("warnings", []),
    )