"""
BQ-Vertex-Analyst -- Phase 6: Eval Harness
=============================================

Runs golden_set.json against the full live pipeline (generate -> gate ->
execute -> grounded answer) and produces the project's actual headline
metrics: pass rate, adversarial-guard catch rate, average latency,
average cost per query.

Design choice, stated up front: for "scalar_accuracy" cases, ground
truth is NOT hardcoded in golden_set.json. It's computed at eval time by
running a small, hand-written, trusted reference query directly against
BigQuery (bypassing the agent entirely) and comparing the agent's answer
against that live result. A static answer key would be a claim I can't
verify from a sandbox with no BigQuery access; a live-computed reference
is verifiable by construction -- it can't go stale, and it isn't a
number I guessed at.

Four check types, one per golden-set case:
  blocked           -- the agent's query MUST be blocked by the grounding
                        gate, with a specific substring in the violation
                        message. Tests the cardinality guard's robustness
                        under paraphrase, not just the exact wording that
                        originally proved Phase 2's failure.
  scalar_accuracy    -- the agent's cited_values must contain a number
                        matching an independently-computed reference
                        query's result, within tolerance.
  structural         -- multi-row results aren't checked against an exact
                        value (comparing "average order value by state"
                        against a single number doesn't make sense) --
                        checked instead for a clean pass through every
                        stage: gate passed, execution succeeded,
                        hallucination check passed.
  suggest            -- suggest_questions mode returns at least a minimum
                        number of questions with no gate violations.

Prerequisites
-------------
    Run from the repo root (same package requirement as every phase
    since Phase 2's restructure):
        python -m phase6.eval_harness --project bq-vertex-analyst
"""

import argparse
import json
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

from phase2.agent_core import build_graph, AgentState, PROJECT_ID

GOLDEN_SET_PATH = Path(__file__).parent / "golden_set.json"
DATASET = "bigquery-public-data.thelook_ecommerce"  # must match phase1/phase3/phase4/phase5


@dataclass
class CaseResult:
    case_id: str
    check_type: str
    passed: bool
    detail: str
    latency_seconds: float
    bytes_billed: Optional[int] = None


def load_golden_set() -> list:
    with open(GOLDEN_SET_PATH) as f:
        return json.load(f)


def run_reference_query(sql: str, project_id: str) -> float:
    """
    Executes a trusted, hand-written reference query directly against
    BigQuery -- NOT through the agent, NOT through the grounding gate.
    This is the harness's own ground truth, independent of anything the
    agent generates. Raises on failure rather than silently returning a
    default; a broken reference query should fail loudly, not produce a
    false pass or fail downstream.
    """
    from google.cloud import bigquery
    client = bigquery.Client(project=project_id)
    job_config = bigquery.QueryJobConfig(default_dataset=DATASET)
    job = client.query(sql, job_config=job_config)
    rows = list(job.result())
    if not rows:
        raise ValueError(f"Reference query returned no rows: {sql}")
    return float(rows[0]["v"])


def numeric_match(reference_value: float, cited_values: list, tolerance: float) -> bool:
    for v in cited_values:
        cleaned = str(v).replace(",", "").replace("$", "").strip()
        try:
            v_float = float(cleaned)
        except ValueError:
            continue
        if reference_value == 0:
            if abs(v_float) < 1e-9:
                return True
        elif abs(v_float - reference_value) / abs(reference_value) <= tolerance:
            return True
    return False


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


def run_case(case: dict, graph, project_id: str) -> CaseResult:
    check_type = case["check_type"]
    mode = "answer_question" if case["mode"] == "answer" else "suggest_questions"

    start = time.time()
    try:
        final_state = graph.invoke(_initial_state(mode, case.get("question")))
    except Exception as e:
        elapsed = time.time() - start
        return CaseResult(case["id"], check_type, False, f"Pipeline raised: {e}", elapsed)
    elapsed = time.time() - start

    bytes_billed = final_state.get("execution_bytes_billed")

    if check_type == "blocked":
        gate_passed = final_state.get("gate_passed")
        blocking = final_state.get("gate_blocking", [])
        expected_substring = case["expect_violation_substring"]
        if gate_passed:
            return CaseResult(case["id"], check_type, False, "Expected BLOCKED, gate PASSED", elapsed)
        if not any(expected_substring in v for v in blocking):
            return CaseResult(
                case["id"], check_type, False,
                f"Blocked, but expected substring '{expected_substring}' not found in: {blocking}",
                elapsed,
            )
        return CaseResult(case["id"], check_type, True, "Correctly blocked", elapsed)

    if check_type == "scalar_accuracy":
        if not final_state.get("gate_passed"):
            return CaseResult(
                case["id"], check_type, False,
                f"Expected PASS, gate BLOCKED: {final_state.get('gate_blocking')}",
                elapsed, bytes_billed,
            )
        try:
            reference_value = run_reference_query(case["reference_sql"], project_id)
        except Exception as e:
            return CaseResult(case["id"], check_type, False, f"Reference query failed: {e}", elapsed, bytes_billed)

        cited = final_state.get("cited_values", [])
        if numeric_match(reference_value, cited, case["tolerance"]):
            return CaseResult(
                case["id"], check_type, True,
                f"Matched reference value {reference_value} within tolerance {case['tolerance']}",
                elapsed, bytes_billed,
            )
        return CaseResult(
            case["id"], check_type, False,
            f"Reference value {reference_value} not found in cited_values {cited} (tolerance {case['tolerance']})",
            elapsed, bytes_billed,
        )

    if check_type == "structural":
        if not final_state.get("gate_passed"):
            return CaseResult(case["id"], check_type, False, f"Gate blocked: {final_state.get('gate_blocking')}", elapsed, bytes_billed)
        if final_state.get("grounded_answer") is None:
            return CaseResult(case["id"], check_type, False, "No grounded answer produced", elapsed, bytes_billed)
        if not final_state.get("hallucination_passed"):
            return CaseResult(case["id"], check_type, False, "Hallucination check failed", elapsed, bytes_billed)
        return CaseResult(case["id"], check_type, True, "Passed gate, executed, hallucination-clean", elapsed, bytes_billed)

    if check_type == "suggest":
        if not final_state.get("gate_passed"):
            return CaseResult(case["id"], check_type, False, f"Gate blocked: {final_state.get('gate_blocking')}", elapsed)
        questions = (final_state.get("result") or {}).get("questions", [])
        min_q = case.get("min_questions", 1)
        if len(questions) < min_q:
            return CaseResult(case["id"], check_type, False, f"Only {len(questions)} questions, expected >= {min_q}", elapsed)
        return CaseResult(case["id"], check_type, True, f"{len(questions)} questions, gate clean", elapsed)

    return CaseResult(case["id"], check_type, False, f"Unknown check_type: {check_type}", elapsed)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default=PROJECT_ID)
    parser.add_argument("--output", default="eval_report.json")
    args = parser.parse_args()

    golden_set = load_golden_set()
    graph = build_graph()

    results = []
    for case in golden_set:
        print(f"Running: {case['id']} ({case['check_type']})...")
        result = run_case(case, graph, args.project)
        results.append(result)
        status = "PASS" if result.passed else "FAIL"
        print(f"  [{status}] {result.detail} ({result.latency_seconds:.2f}s)")

    total = len(results)
    passed = sum(1 for r in results if r.passed)

    blocked_cases = [r for r in results if r.check_type == "blocked"]
    blocked_pass = sum(1 for r in blocked_cases if r.passed)

    scalar_cases = [r for r in results if r.check_type == "scalar_accuracy"]
    scalar_pass = sum(1 for r in scalar_cases if r.passed)

    latencies = [r.latency_seconds for r in results]
    avg_latency = sum(latencies) / len(latencies) if latencies else 0

    bytes_billed_values = [r.bytes_billed for r in results if r.bytes_billed is not None]
    avg_bytes = sum(bytes_billed_values) / len(bytes_billed_values) if bytes_billed_values else None

    summary = {
        "total_cases": total,
        "total_passed": passed,
        "pass_rate": round(passed / total, 4) if total else None,
        "adversarial_guard_catch_rate": round(blocked_pass / len(blocked_cases), 4) if blocked_cases else None,
        "scalar_accuracy_rate": round(scalar_pass / len(scalar_cases), 4) if scalar_cases else None,
        "avg_latency_seconds": round(avg_latency, 3),
        "avg_bytes_billed_per_query": round(avg_bytes, 0) if avg_bytes is not None else None,
        "cases": [asdict(r) for r in results],
    }

    print()
    print("=" * 60)
    print(f"TOTAL: {passed}/{total} passed ({summary['pass_rate']:.0%})" if total else "No cases run")
    print(f"Adversarial guard catch rate: {summary['adversarial_guard_catch_rate']}")
    print(f"Scalar accuracy rate: {summary['scalar_accuracy_rate']}")
    print(f"Average latency: {summary['avg_latency_seconds']}s")
    print(f"Average bytes billed per query: {summary['avg_bytes_billed_per_query']}")
    print("=" * 60)

    with open(args.output, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\nFull report written to {args.output}")


if __name__ == "__main__":
    main()
