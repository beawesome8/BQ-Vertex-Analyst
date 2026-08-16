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
    schema_context: Optional[str]
    raw_response: Optional[str]
    result: Optional[dict]
    warnings: list


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


def _get_unreliable_columns(profile: list) -> set:
    """Set of (table, column) pairs marked cardinality_reliable=False."""
    unreliable = set()
    for table in profile:
        for col in table["columns"]:
            if col.get("cardinality_reliable") is False:
                unreliable.add((table["table"], col["name"]))
    return unreliable


def _check_cardinality_violations(sql: str, unreliable_columns: set) -> list:
    """
    Narrow, targeted check: does the generated SQL make a distinct-value
    claim (COUNT(DISTINCT ...), APPROX_COUNT_DISTINCT(...), or a bare
    SELECT DISTINCT) over any column flagged cardinality_reliable=False?

    This deliberately does NOT validate general table/column existence or
    join correctness -- that's Phase 3's grounding gate. It exists only to
    catch one specific, proven failure mode: the system instruction alone
    did not stop the model from generating
    `SELECT COUNT(DISTINCT user_id) FROM events` when asked directly,
    despite events.user_id being explicitly marked CARDINALITY UNKNOWN in
    the schema context it was given. Prompt instructions are not
    enforcement -- this function is the actual enforcement for this one
    failure mode, in code, not prose.
    """
    violations = []
    distinct_patterns = [
        r"COUNT\s*\(\s*DISTINCT\s+(?:\w+\.)?(\w+)\s*\)",
        r"APPROX_COUNT_DISTINCT\s*\(\s*(?:\w+\.)?(\w+)\s*\)",
        r"SELECT\s+DISTINCT\s+(?:\w+\.)?(\w+)",
    ]
    matched_columns = set()
    for pattern in distinct_patterns:
        for m in re.finditer(pattern, sql, re.IGNORECASE):
            matched_columns.add(m.group(1).lower())

    for table, column in unreliable_columns:
        if column.lower() in matched_columns:
            violations.append(
                f"CARDINALITY GUARD VIOLATION: generated SQL claims a distinct-value "
                f"count involving '{table}.{column}', which is marked "
                f"cardinality_reliable=False (true cardinality unknown -- Phase 1's "
                f"clamp only bounds it, doesn't measure it). The system instruction "
                f"told the model not to do this; it did it anyway. This query should "
                f"not be trusted or executed as-is."
            )
    return violations


def node_lightweight_check(state: AgentState) -> AgentState:
    """
    NOT the Phase 3 grounding gate. That's a dedicated phase with cost
    dry-runs, table/column existence checks against the real schema, and
    an actual block/allow decision. This is two narrow, code-enforced
    checks -- not prompt-based trust -- covering failure modes already
    proven to occur:
      1. A write/DDL keyword slipping through (defense in depth; this
         agent never executes anything, that's Phase 4's job).
      2. A distinct-value claim over a column marked cardinality_reliable
         =False -- proven to happen despite an explicit system instruction
         not to (see _check_cardinality_violations docstring).
    """
    if not state["result"]:
        return state

    if state["mode"] == "answer_question":
        sql = state["result"].get("sql", "")

        banned = re.search(r"\b(DROP|DELETE|INSERT|UPDATE|ALTER|TRUNCATE|MERGE)\b", sql, re.IGNORECASE)
        if banned:
            state["warnings"].append(
                f"Generated SQL contains a write/DDL keyword ({banned.group(0)}) -- "
                f"a read-only agent should never produce this. DO NOT EXECUTE."
            )

        profile = load_schema_profile()
        unreliable_columns = _get_unreliable_columns(profile)
        violations = _check_cardinality_violations(sql, unreliable_columns)
        if violations:
            state["warnings"].extend(violations)
            state["result"]["cardinality_guard_violation"] = True

    return state


def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("build_context", node_build_context)
    graph.add_node("generate", node_generate)
    graph.add_node("lightweight_check", node_lightweight_check)
    graph.set_entry_point("build_context")
    graph.add_edge("build_context", "generate")
    graph.add_edge("generate", "lightweight_check")
    graph.add_edge("lightweight_check", END)
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
        "schema_context": None,
        "raw_response": None,
        "result": None,
        "warnings": [],
    }

    final_state = app.invoke(initial_state)

    print(json.dumps(final_state["result"], indent=2))
    if final_state["warnings"]:
        print("\nWARNINGS:")
        for w in final_state["warnings"]:
            print(f"  - {w}")


if __name__ == "__main__":
    main()