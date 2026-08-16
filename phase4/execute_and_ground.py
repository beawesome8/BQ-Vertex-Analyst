"""
BQ-Vertex-Analyst -- Phase 4: Execution + Grounded Answer + Hallucination Cross-Check
=======================================================================================

Three responsibilities, in order:
  1. Execute SQL against BigQuery for real (not a dry-run -- this is the
     first phase that actually runs a query).
  2. Generate a natural-language answer to the original question, using
     Gemini, constrained to ONLY the actual returned rows.
  3. Cross-check the answer's cited values against the real row data --
     if the model states a number or fact that doesn't actually appear in
     the rows, that's a caught hallucination, not a trusted claim.

PRECONDITION, not re-verified by this module: the SQL passed here MUST
already have passed Phase 3's grounding gate. This module does NOT
re-run table/column/join/cardinality checks -- that's the gate's job, one
phase, one responsibility. It DOES re-run the byte-budget dry-run check
immediately before executing, as deliberate defense in depth (the gate's
dry-run and this execution are separate calls; re-checking costs nothing
and catches drift). Never call execute_query() on SQL that hasn't already
been through the gate.

Hallucination check is a naive string-containment match, not semantic
verification. KNOWN LIMITATION, documented rather than discovered later:
formatting differences (currency symbols, rounding, thousands separators)
can cause false-positive flags even when a value is genuinely grounded --
e.g. a cited "$1,234.50" won't literally match a raw row value of
1234.5. This trades some false positives for zero false negatives on
exact-match hallucinations, which is the safer failure direction for a
grounding check.

What this phase does NOT do: retry or auto-correct a failed hallucination
check. It detects; it doesn't remediate. Remediation (regenerate with the
violation as feedback, or fall back to raw rows) is a reasonable Phase 5+
enhancement, not built here.

Prerequisites
-------------
    pip install google-cloud-bigquery google-genai

Usage (standalone)
-------------------
    python execute_and_ground.py --question "What is the average order value by state?" \\
        --sql "SELECT state, AVG(sale_price) AS avg_value FROM ..." \\
        --project bq-vertex-analyst
"""

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path

from google.cloud import bigquery
from google import genai
from google.genai import types as genai_types

PROJECT_ID = "bq-vertex-analyst"
LOCATION = "us-central1"
MODEL_ID = "gemini-2.5-flash"  # keep in sync with phase2/agent_core.py -- verify against Model Garden
DATASET = "bigquery-public-data.thelook_ecommerce"  # must match phase1 and phase3

MAX_EXECUTION_BYTES = 500_000_000  # same budget as Phase 3's dry-run check
MAX_ROWS_IN_PROMPT = 50  # cap on rows serialized into the answer-generation prompt


@dataclass
class ExecutionResult:
    rows: list
    row_count: int
    bytes_billed: int
    truncated: bool = False


@dataclass
class GroundedAnswer:
    answer: str
    cited_values: list = field(default_factory=list)
    hallucination_violations: list = field(default_factory=list)
    passed: bool = True


def execute_query(sql: str, project_id: str = PROJECT_ID, max_bytes: int = MAX_EXECUTION_BYTES) -> ExecutionResult:
    """
    Executes SQL for real against BigQuery. Re-checks the byte budget via
    a fresh dry-run immediately before executing -- deliberate defense in
    depth, not redundant paranoia. The gate's dry-run and this execution
    are separate calls that could, in principle, see a changed table
    between them; re-checking is cheap, skipping it and being wrong is not.
    """
    client = bigquery.Client(project=project_id)

    dry_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False, default_dataset=DATASET)
    dry_job = client.query(sql, job_config=dry_config)
    est_bytes = dry_job.total_bytes_processed or 0
    if est_bytes > max_bytes:
        raise ValueError(
            f"Execution blocked: dry-run re-check estimates {est_bytes:,} bytes, "
            f"exceeding the {max_bytes:,} byte budget. Refusing to execute -- if this "
            f"query passed Phase 3's gate with a different estimate, the underlying "
            f"table may have changed size since then."
        )

    real_config = bigquery.QueryJobConfig(default_dataset=DATASET)
    job = client.query(sql, job_config=real_config)
    result_rows = list(job.result())
    rows_as_dicts = [dict(row.items()) for row in result_rows]

    return ExecutionResult(
        rows=rows_as_dicts,
        row_count=len(rows_as_dicts),
        bytes_billed=job.total_bytes_billed or 0,
        truncated=len(rows_as_dicts) > MAX_ROWS_IN_PROMPT,
    )


# v2 finding: the original instruction ("list every value you rely on") combined
# with "never state an ungrounded fact" pushed the model toward reciting every
# row verbatim rather than synthesizing -- passed the hallucination check (every
# cited value was genuinely grounded) while producing a useless wall-of-numbers
# answer on a 228-row real result. Grounding and usefulness are different axes;
# this instruction now optimizes for both, not just the one that's easy to check
# in code.
ANSWER_SYSTEM_INSTRUCTION = """You are answering a user's question using ONLY the query result rows provided below.

Rules you MUST follow, without exception:
1. Base your answer strictly on the provided rows. Never state a number, name, or fact that isn't present in the rows.
2. If the rows are empty, say so plainly -- do not invent a plausible-sounding answer.
3. Write a CONCISE, SYNTHESIZED answer -- a few sentences, not an exhaustive listing. If there are many rows (many states, categories, etc.), highlight the extremes (highest, lowest), the overall range, and any clear pattern. Do NOT enumerate every single row's value one by one unless the question explicitly asks for a full row-by-row breakdown -- a wall of 50 numbers is not a useful answer even if every number is accurate.
4. List ONLY the specific values your synthesized answer actually references in "cited_values", written EXACTLY as they appear in the row data. This should typically be a small handful of values (the ones you actually mention in prose), not one per row.
5. Output ONLY valid JSON matching this exact shape, with no markdown fences and no text outside the JSON object:
{"answer": "...", "cited_values": ["...", "..."]}
"""


def _extract_json(text: str) -> dict:
    import re
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    return json.loads(text)


def get_client(project_id: str = PROJECT_ID) -> genai.Client:
    return genai.Client(vertexai=True, project=project_id, location=LOCATION)


def generate_grounded_answer(nl_question: str, rows: list, client: genai.Client) -> tuple:
    """Returns (answer: str, cited_values: list)."""
    rows_for_prompt = rows[:MAX_ROWS_IN_PROMPT]
    rows_json = json.dumps(rows_for_prompt, default=str)
    prompt = (
        f"QUESTION: {nl_question}\n\n"
        f"QUERY RESULT ROWS ({len(rows)} total, showing {len(rows_for_prompt)}):\n{rows_json}"
    )

    response = client.models.generate_content(
        model=MODEL_ID,
        contents=prompt,
        config=genai_types.GenerateContentConfig(
            system_instruction=ANSWER_SYSTEM_INSTRUCTION,
            temperature=0.0,
            response_mime_type="application/json",
        ),
    )
    parsed = _extract_json(response.text)
    return parsed.get("answer", ""), parsed.get("cited_values", [])


def check_hallucination(cited_values: list, rows: list) -> list:
    """
    For each value the model claims it cited from the data, verify it
    actually appears somewhere in the returned rows via a flattened
    string-containment check. Crude but concrete and code-enforced,
    rather than trusting the model's own claim that it grounded its
    answer. See module docstring for the known false-positive limitation.
    """
    flattened = " ".join(str(v) for row in rows for v in row.values()).lower()

    violations = []
    for value in cited_values:
        value_str = str(value).strip().lower()
        if not value_str:
            continue
        if value_str not in flattened:
            violations.append(
                f"Cited value '{value}' does not appear anywhere in the actual query "
                f"result rows -- likely hallucinated, not grounded in real data. "
                f"(Note: exact-match check -- reformatted values like added currency "
                f"symbols or rounding can also trigger this; verify manually before "
                f"assuming a true hallucination.)"
            )
    return violations


def execute_and_ground_answer(nl_question: str, sql: str, project_id: str = PROJECT_ID) -> tuple:
    """Returns (GroundedAnswer, ExecutionResult). Raises if execution itself fails."""
    execution = execute_query(sql, project_id)
    client = get_client(project_id)
    answer, cited_values = generate_grounded_answer(nl_question, execution.rows, client)
    violations = check_hallucination(cited_values, execution.rows)

    grounded = GroundedAnswer(
        answer=answer,
        cited_values=cited_values,
        hallucination_violations=violations,
        passed=(len(violations) == 0),
    )
    return grounded, execution


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--question", required=True)
    parser.add_argument("--sql", required=True, help="SQL that has already passed Phase 3's grounding gate")
    parser.add_argument("--project", default=PROJECT_ID)
    args = parser.parse_args()

    grounded, execution = execute_and_ground_answer(args.question, args.sql, args.project)

    print(f"Rows returned: {execution.row_count} (bytes billed: {execution.bytes_billed:,})")
    if execution.truncated:
        print(f"NOTE: only the first {MAX_ROWS_IN_PROMPT} rows were shown to the model for answer generation.")
    print()
    print("ANSWER:", grounded.answer)
    print()
    print("CITED VALUES:", grounded.cited_values)
    print()
    print(f"HALLUCINATION CHECK: {'PASSED' if grounded.passed else 'FAILED'}")
    if grounded.hallucination_violations:
        for v in grounded.hallucination_violations:
            print(f"  - {v}")


if __name__ == "__main__":
    main()