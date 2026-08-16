"""
BQ-Vertex-Analyst -- Phase 2: LangGraph Agent Core
====================================================

Two modes, one graph:
  answer_question    -- NL question -> SQL against the profiled schema
  suggest_questions   -- ranked list of analytical questions worth asking

Schema-aware means literally that: the model only ever sees the actual
table/column names, types, inferred FK hints, and null rates from Phase 1's
schema_profile.json. It is never shown a cardinality number for any column
flagged cardinality_reliable=False -- that's the concrete implementation
of the "treat unreliable columns as off-limits" decision made before this
phase started. The model can't reason about a number it never receives.

SDK note: uses `google-genai` (`from google import genai`), NOT the older
`vertexai.generative_models` module -- that module was deprecated June 24,
2025 and its removal date (June 24, 2026) has already passed. If you see
tutorials using `from vertexai.generative_models import GenerativeModel`,
they're out of date.

Phase 3 wiring: the narrow, warn-only cardinality check from the original
Phase 2 build has been REPLACED by a real call into
phase3/grounding_gate.py's run_grounding_gate() -- full AST-based schema
validation, join-path checking, a BLOCKING cardinality guard (not just a
warning), and a real BigQuery dry-run cost check for answer_question mode.

PACKAGE STRUCTURE: phase1/, phase2/, phase3/, phase4/ are proper Python
packages (each has __init__.py). Run as a module from the repo root:

    python -m phase2.agent_core --mode answer --question "..."

Phase 4 wiring: execution + grounded answer generation now runs
automatically after a passed grounding gate, via a CONDITIONAL graph
edge -- not an if-check inside a node. This means a blocked query is
structurally prevented from ever reaching execution; it's not just
convention that it doesn't happen, the graph itself has no edge leading
there for a blocked or suggest_questions result.

What this phase deliberately does NOT do:
  - Execute any generated SQL against BigQuery (Phase 4)
  - Validate SQL against real query cost / EXPLAIN plans (Phase 3)
  - Block or gate output beyond a cheap keyword sanity check below
  - Anything with write/DDL SQL -- this agent is read-only by design,
    and the lightweight check flags (not silently allows) any DDL/write
    keyword that slips through, though the real enforcement is Phase 3's
    validation gate, not this script.

Prerequisites (run yourself)
------------------------------
    pip install google-genai langgraph
    gcloud auth application-default login   # if not already done

Usage
-----
    python agent_core.py --mode suggest
    python agent_core.py --mode answer --question "Which distribution center ships the most orders?"
"""

import argparse
import json
import re
from pathlib import Path
from typing import TypedDict, Literal, Optional

from google import genai
from google.genai import types as genai_types
from langgraph.graph import StateGraph, END

from phase3.grounding_gate import run_grounding_gate, GateResult
from phase4.execute_and_ground import execute_and_ground_answer

PROJECT_ID = "bq-vertex-analyst"
LOCATION = "us-central1"  # kept consistent with Phase 1 region decision --
                           # BigQuery public data lives in the US multi-region;
                           # see README Section 10 note on EU vs US for the
                           # production-vs-development distinction
MODEL_ID = "gemini-2.5-flash"  # VERIFY against Model Garden in your project --
                                 # Gemini model IDs are moving fast right now;
                                 # this is the most consistently-documented
                                 # stable option as of this writing, not a
                                 # guarantee of availability in your region

SCHEMA_PROFILE_PATH = Path(__file__).parent.parent / "phase1" / "schema_profile.json"


class AgentState(TypedDict):
    mode: Literal["answer_question", "suggest_questions"]
    nl_question: Optional[str]
    schema_profile: Optional[list]
    schema_context: Optional[str]
    raw_response: Optional[str]
    result: Optional[dict]
    warnings: list
    gate_passed: Optional[bool]
    gate_blocking: list
    gate_dry_run_bytes: Optional[int]
    execution_row_count: Optional[int]
    execution_bytes_billed: Optional[int]
    grounded_answer: Optional[str]
    cited_values: list
    hallucination_passed: Optional[bool]


def load_schema_profile(path: Path = SCHEMA_PROFILE_PATH) -> list:
    if not path.exists():
        raise FileNotFoundError(
            f"schema_profile.json not found at {path}. Run Phase 1's "
            f"profile_schema.py first -- this agent has nothing to reason "
            f"about without it."
        )
    with open(path) as f:
        return json.load(f)


def build_schema_context(profile: list) -> str:
    """
    Renders the schema profile into a compact text block for the prompt.

    Cardinality is included ONLY when cardinality_reliable is True. For
    unreliable columns (currently just events.user_id -- see Phase 1
    NOTES.md), an explicit warning replaces the number entirely. This is
    the actual enforcement point for "off-limits": the model literally
    never receives a cardinality figure for that column, so it can't
    accidentally reason over a number that was capped, not measured.
    """
    lines = []
    for table in profile:
        lines.append(f"TABLE {table['table']} ({table['row_count']} rows)")
        for col in table["columns"]:
            parts = [f"  - {col['name']} ({col['field_type']}, {col['mode']})"]

            if col.get("inferred_fk_target"):
                parts.append(
                    f"[inferred FK -> {col['inferred_fk_target']}, "
                    f"NOT a declared constraint -- treat as a hint to verify, not ground truth]"
                )

            if col.get("null_rate") is not None and col["null_rate"] > 0:
                parts.append(f"[null_rate={col['null_rate']}]")

            if col.get("cardinality_reliable") is False:
                parts.append(
                    "[CARDINALITY UNKNOWN -- do not state or infer a distinct-value "
                    "count or uniqueness claim for this column under any circumstances]"
                )
            elif col.get("estimated_full_cardinality") is not None:
                parts.append(f"[~{col['estimated_full_cardinality']} distinct values, estimated]")
            elif col.get("approx_distinct_in_sample") is not None and col.get("sample_fraction") is None:
                parts.append(f"[{col['approx_distinct_in_sample']} distinct values, exact full-table count]")

            lines.append(" ".join(parts))
        lines.append("")
    return "\n".join(lines)


def get_client() -> genai.Client:
    return genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)


SQL_SYSTEM_INSTRUCTION = """You are a read-only SQL analyst agent working against a real BigQuery schema.

Rules you MUST follow, without exception:
1. Only reference tables and columns that appear in the schema below. Never invent a table or column name -- if you're unsure one exists, don't use it.
2. Foreign keys marked "inferred" are naming-convention guesses, not verified database constraints. If a query depends on one, say so in your explanation.
3. Never state or imply how many distinct values a column has, or whether its values are unique, for any column marked CARDINALITY UNKNOWN. If the question genuinely requires that reasoning, say so explicitly in your explanation instead of guessing a number.
4. Generate SELECT queries only. Never generate DROP, DELETE, INSERT, UPDATE, ALTER, TRUNCATE, or MERGE.
5. Output ONLY valid JSON matching this exact shape, with no markdown code fences and no text outside the JSON object:
{"sql": "...", "tables_used": ["table1", "table2"], "explanation": "...", "caveats": ["..."]}
"""

QUESTIONS_SYSTEM_INSTRUCTION = """You are a data analyst agent proposing interesting analytical questions grounded ONLY in the schema below.

Rules you MUST follow, without exception:
1. Never invent a table or column name that isn't in the schema below.
2. Rank questions by likely business value to a retail operations team.
3. If a question's answer would depend on knowing how many distinct values a column has, and that column is marked CARDINALITY UNKNOWN, do not propose that question at all -- skip it silently rather than proposing it with a caveat.
4. Output ONLY valid JSON matching this exact shape, with no markdown code fences and no text outside the JSON object:
{"questions": [{"question": "...", "rationale": "...", "relevant_tables": ["table1", "table2"]}]}
"""


def node_build_context(state: AgentState) -> AgentState:
    profile = load_schema_profile()
    state["schema_profile"] = profile
    state["schema_context"] = build_schema_context(profile)
    return state


def _extract_json(text: str) -> dict:
    """Defensive strip in case the model wraps output in markdown fences despite instructions."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    return json.loads(text)


def node_generate(state: AgentState) -> AgentState:
    client = get_client()

    if state["mode"] == "answer_question":
        system = SQL_SYSTEM_INSTRUCTION
        user_prompt = f"SCHEMA:\n{state['schema_context']}\n\nQUESTION: {state['nl_question']}"
    else:
        system = QUESTIONS_SYSTEM_INSTRUCTION
        user_prompt = f"SCHEMA:\n{state['schema_context']}\n\nPropose 5-8 ranked analytical questions."

    response = client.models.generate_content(
        model=MODEL_ID,
        contents=user_prompt,
        config=genai_types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.1,
            response_mime_type="application/json",
        ),
    )
    state["raw_response"] = response.text

    try:
        state["result"] = _extract_json(response.text)
    except (json.JSONDecodeError, TypeError) as e:
        state["warnings"].append(f"Model output was not valid JSON: {e}")
        state["result"] = None

    return state


def node_grounding_gate(state: AgentState) -> AgentState:
    """
    Replaces Phase 2's original narrow, warn-only cardinality check. Now
    calls Phase 3's real grounding gate: AST-based table/column existence
    validation, join-path checking against inferred FKs, a BLOCKING
    cardinality guard (not a warning), DDL/DML rejection, and -- for
    answer_question mode -- a real BigQuery dry-run cost check.

    Dry-run only runs for answer_question mode (suggest_questions has no
    SQL to dry-run) and only if the offline checks passed first -- see
    run_grounding_gate()'s fail-fast ordering in phase3/grounding_gate.py.
    """
    if not state["result"]:
        return state

    gate_result: GateResult = run_grounding_gate(
        agent_output=state["result"],
        mode=state["mode"],
        schema_profile=state["schema_profile"],
        project_id=PROJECT_ID,
        run_dry_run=(state["mode"] == "answer_question"),
    )

    state["gate_passed"] = gate_result.passed
    state["gate_blocking"] = gate_result.blocking_violations
    state["gate_dry_run_bytes"] = gate_result.dry_run_bytes
    state["warnings"].extend(gate_result.warnings)

    if not gate_result.passed:
        state["result"]["grounding_gate_blocked"] = True

    return state


def node_execute_and_ground(state: AgentState) -> AgentState:
    """
    Only reached via conditional routing (_route_after_gate) when mode is
    answer_question AND the grounding gate passed -- this is structural,
    not a convention. A blocked or suggest_questions result has no graph
    edge leading here at all.
    """
    sql = state["result"].get("sql", "")

    try:
        grounded, execution = execute_and_ground_answer(state["nl_question"], sql, PROJECT_ID)
    except Exception as e:
        state["warnings"].append(f"Execution/grounding failed: {e}")
        return state

    state["execution_row_count"] = execution.row_count
    state["execution_bytes_billed"] = execution.bytes_billed
    state["grounded_answer"] = grounded.answer
    state["cited_values"] = grounded.cited_values
    state["hallucination_passed"] = grounded.passed

    if grounded.hallucination_violations:
        state["warnings"].extend(grounded.hallucination_violations)

    return state


def _route_after_gate(state: AgentState) -> str:
    """
    Structural gate: only route to execution if this is an answer_question
    result AND the grounding gate actually passed. Everything else (a
    blocked query, or suggest_questions mode, which has no SQL to run)
    goes straight to END.
    """
    if state["mode"] == "answer_question" and state.get("gate_passed"):
        return "execute_and_ground"
    return "end"


def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("build_context", node_build_context)
    graph.add_node("generate", node_generate)
    graph.add_node("grounding_gate", node_grounding_gate)
    graph.add_node("execute_and_ground", node_execute_and_ground)
    graph.set_entry_point("build_context")
    graph.add_edge("build_context", "generate")
    graph.add_edge("generate", "grounding_gate")
    graph.add_conditional_edges(
        "grounding_gate",
        _route_after_gate,
        {"execute_and_ground": "execute_and_ground", "end": END},
    )
    graph.add_edge("execute_and_ground", END)
    return graph.compile()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["answer", "suggest"], required=True)
    parser.add_argument("--question", help="Required if --mode answer")
    args = parser.parse_args()

    if args.mode == "answer" and not args.question:
        parser.error("--question is required when --mode is 'answer'")

    app = build_graph()
    initial_state: AgentState = {
        "mode": "answer_question" if args.mode == "answer" else "suggest_questions",
        "nl_question": args.question,
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

    final_state = app.invoke(initial_state)

    print(json.dumps(final_state["result"], indent=2))

    if final_state["gate_passed"] is not None:
        print(f"\nGROUNDING GATE: {'PASSED' if final_state['gate_passed'] else 'BLOCKED'}")
        if final_state["gate_dry_run_bytes"] is not None:
            print(f"Dry-run bytes: {final_state['gate_dry_run_bytes']:,}")
        if final_state["gate_blocking"]:
            print("BLOCKING VIOLATIONS:")
            for v in final_state["gate_blocking"]:
                print(f"  - {v}")

    if final_state["grounded_answer"] is not None:
        print(f"\nEXECUTION: {final_state['execution_row_count']} rows "
              f"({final_state['execution_bytes_billed']:,} bytes billed)")
        print(f"ANSWER: {final_state['grounded_answer']}")
        print(f"CITED VALUES: {final_state['cited_values']}")
        print(f"HALLUCINATION CHECK: {'PASSED' if final_state['hallucination_passed'] else 'FAILED'}")

    if final_state["warnings"]:
        print("\nWARNINGS:")
        for w in final_state["warnings"]:
            print(f"  - {w}")


if __name__ == "__main__":
    main()
# CI test comment -- safe to remove after this test
