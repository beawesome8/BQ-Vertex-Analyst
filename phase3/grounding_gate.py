"""
BQ-Vertex-Analyst -- Phase 3: Grounding / Validation Gate
============================================================

This is the actual enforcement layer that Phase 2 proved was necessary:
Phase 2's system instruction told the model never to claim a cardinality
figure for `events.user_id`, and the model did it anyway when asked
directly. Prose instructions are not enforcement. This module is.

Checks, in fail-fast order (cheapest / free first):
  1. Statement type -- must be SELECT. Any DDL/DML is an immediate block.
  2. Table existence -- every FROM/JOIN table must exist in the profiled
     schema.
  3. Column existence -- every column reference must resolve (via alias)
     to a real column on a real table. Ambiguous unqualified columns are
     warned, not blocked (can't prove them wrong).
  4. Join-path check -- every JOIN ON condition is checked against the
     schema profile's inferred FK pairs. NOT blocking -- inferred FKs are
     naming-convention guesses with known false negatives (a valid join
     can exist that Phase 1's heuristic simply didn't guess). Warned only.
  5. Cardinality guard -- COUNT(DISTINCT ...), APPROX_COUNT_DISTINCT(...),
     and SELECT DISTINCT are checked against cardinality_reliable=False
     columns. BLOCKING here, unlike Phase 2's version, which only warned.
  6. BigQuery dry-run cost check -- only runs if 1-5 all passed, since
     there's no reason to pay an API round-trip validating a query that's
     already known to be invalid.

For `suggest_questions` mode, there's no SQL to parse -- see
validate_questions_grounding()'s docstring for why that check is
deliberately weaker and heuristic, not a gap I'm pretending doesn't exist.

Verified against sqlglot's actual parsed AST shapes in this build (not
assumed from memory) -- see phase3/NOTES.md for what was checked and why
it mattered.

Prerequisites
-------------
    pip install sqlglot google-cloud-bigquery

Usage
-----
    python grounding_gate.py --input answer_output.json --mode answer --project bq-vertex-analyst
"""

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import sqlglot
from sqlglot import exp

SCHEMA_PROFILE_PATH = Path(__file__).parent.parent / "phase1" / "schema_profile.json"


@dataclass
class GateResult:
    passed: bool
    blocking_violations: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    dry_run_bytes: int = None


def load_schema_profile(path: Path = SCHEMA_PROFILE_PATH) -> list:
    with open(path) as f:
        return json.load(f)


def _get_unreliable_columns(profile: list) -> set:
    return {
        (t["table"], c["name"])
        for t in profile
        for c in t["columns"]
        if c.get("cardinality_reliable") is False
    }


def _get_fk_pairs(profile: list) -> set:
    pairs = set()
    for t in profile:
        for c in t["columns"]:
            if c.get("inferred_fk_target"):
                target_table, target_col = c["inferred_fk_target"].split(".")
                pairs.add(frozenset([(t["table"], c["name"]), (target_table, target_col)]))
    return pairs


def _resolve_table(qualifier: str, alias_map: dict) -> str:
    return alias_map.get(qualifier, qualifier)


def validate_sql_grounding(sql: str, schema_profile: list) -> tuple:
    """Returns (blocking_violations: list[str], warnings: list[str])."""
    blocking = []
    warnings = []

    table_columns = {t["table"]: {c["name"] for c in t["columns"]} for t in schema_profile}
    known_tables = set(table_columns.keys())
    unreliable = _get_unreliable_columns(schema_profile)
    fk_pairs = _get_fk_pairs(schema_profile)

    try:
        parsed = sqlglot.parse_one(sql, read="bigquery")
    except Exception as e:
        blocking.append(f"SQL failed to parse: {e}")
        return blocking, warnings

    if not isinstance(parsed, exp.Select):
        blocking.append(
            f"Statement is a {type(parsed).__name__}, not a SELECT. This agent is "
            f"read-only -- no DDL or DML statement is ever permitted, regardless of content."
        )
        return blocking, warnings

    alias_map = {}
    for table_expr in parsed.find_all(exp.Table):
        real_name = table_expr.name
        alias = table_expr.alias_or_name
        if real_name not in known_tables:
            blocking.append(f"References table '{real_name}', which does not exist in the profiled schema.")
        alias_map[alias] = real_name

    for col_expr in parsed.find_all(exp.Column):
        col_name = col_expr.name
        qualifier = col_expr.table

        if qualifier:
            real_table = _resolve_table(qualifier, alias_map)
            if real_table in table_columns and col_name not in table_columns[real_table]:
                blocking.append(
                    f"Column '{qualifier}.{col_name}' does not exist on table "
                    f"'{real_table}' in the profiled schema."
                )
        else:
            referenced_tables = list(alias_map.values())
            found_in = [t for t in referenced_tables if t in table_columns and col_name in table_columns[t]]
            if not found_in:
                blocking.append(f"Column '{col_name}' (unqualified) does not exist in any referenced table.")
            elif len(found_in) > 1:
                warnings.append(
                    f"Column '{col_name}' is ambiguous across {found_in} -- "
                    f"could not verify which table it actually belongs to."
                )

    for join_expr in parsed.find_all(exp.Join):
        on_clause = join_expr.args.get("on")
        if isinstance(on_clause, exp.EQ):
            left = on_clause.this
            right = on_clause.args.get("expression")
            if isinstance(left, exp.Column) and isinstance(right, exp.Column):
                lt = _resolve_table(left.table, alias_map)
                rt = _resolve_table(right.table, alias_map)
                pair = frozenset([(lt, left.name), (rt, right.name)])
                if pair not in fk_pairs:
                    warnings.append(
                        f"Join condition {lt}.{left.name} = {rt}.{right.name} is not "
                        f"confirmed by any inferred FK in the schema profile. May still "
                        f"be valid -- FK inference has known false negatives -- flagged "
                        f"for manual verification, not blocked."
                    )

    def _flag_if_unreliable(col_expr):
        real_table = _resolve_table(col_expr.table, alias_map) if col_expr.table else None
        candidates = [real_table] if real_table else list(alias_map.values())
        for t in candidates:
            if (t, col_expr.name) in unreliable:
                blocking.append(
                    f"CARDINALITY GUARD: query claims a distinct-value count involving "
                    f"'{t}.{col_expr.name}', which is marked cardinality_reliable=False. "
                    f"True cardinality is unknown, only bounded by a clamp -- this claim "
                    f"cannot be trusted and this query is blocked."
                )

    for count_node in parsed.find_all(exp.Count):
        if isinstance(count_node.this, exp.Distinct):
            for col in count_node.this.find_all(exp.Column):
                _flag_if_unreliable(col)

    for approx_node in parsed.find_all(exp.ApproxDistinct):
        for col in approx_node.find_all(exp.Column):
            _flag_if_unreliable(col)

    if parsed.args.get("distinct"):
        for col in parsed.find_all(exp.Column):
            _flag_if_unreliable(col)

    return blocking, warnings


def validate_questions_grounding(questions: list, schema_profile: list) -> tuple:
    """
    Returns (blocking_violations: list[str], warnings: list[str]).

    Deliberately weaker than validate_sql_grounding(). There's no SQL AST
    for suggest_questions mode -- just prose text. This does a keyword
    heuristic scan instead of structural verification, which will miss
    things an AST check would catch and may occasionally over-flag. This
    is the documented Phase 2 known limitation, now given an actual
    (imperfect) check rather than none.
    """
    blocking = []
    warnings = []
    known_tables = {t["table"] for t in schema_profile}
    unreliable = _get_unreliable_columns(schema_profile)

    cardinality_language = re.compile(
        r"\b(how many (distinct|unique)|unique (count|users?|values?|ids?)|"
        r"distinct count|number of (distinct|unique)|count of (distinct|unique))\b",
        re.IGNORECASE,
    )

    for i, q in enumerate(questions):
        for table in q.get("relevant_tables", []):
            if table not in known_tables:
                blocking.append(f"Question {i}: references unknown table '{table}' not in the profiled schema.")

        text = f"{q.get('question', '')} {q.get('rationale', '')}".lower()
        if cardinality_language.search(text):
            for table, column in unreliable:
                if table.lower() in text or column.lower() in text:
                    warnings.append(
                        f"Question {i} uses cardinality-implying language and its text "
                        f"mentions '{table}' or '{column}' (cardinality_reliable=False). "
                        f"Heuristic text match, not AST-verified -- flagged for manual "
                        f"review, not auto-blocked."
                    )

    return blocking, warnings


DATASET = "bigquery-public-data.thelook_ecommerce"  # must match phase1/profile_schema.py's DATASET


def dry_run_cost_check(sql: str, project_id: str, max_bytes: int = 500_000_000) -> tuple:
    """
    Returns (bytes_processed: int|None, blocking: list[str], warnings: list[str]).
    Only call after validate_sql_grounding() passes with zero blocking
    violations -- no reason to pay an API round-trip validating cost on a
    query already known to reference something that doesn't exist.

    Uses default_dataset so unqualified table names (e.g. `FROM users`,
    matching what schema_profile.json calls tables) resolve correctly.
    Deliberate design choice: the agent's schema context shows it bare
    table names, matching the profile -- fully-qualifying every generated
    query would push an execution-environment detail (which dataset this
    happens to live in) into the LLM's output, when it's really a property
    of where the query runs, not what it means. Keep that concern here,
    in the execution config, not in the prompt.
    """
    from google.cloud import bigquery

    blocking = []
    warnings = []
    client = bigquery.Client(project=project_id)

    try:
        job_config = bigquery.QueryJobConfig(
            dry_run=True,
            use_query_cache=False,
            default_dataset=DATASET,
        )
        job = client.query(sql, job_config=job_config)
        bytes_processed = job.total_bytes_processed or 0
    except Exception as e:
        blocking.append(
            f"BigQuery rejected this query at dry-run: {e}. This is independent of the "
            f"schema-profile checks above -- BigQuery itself found something invalid."
        )
        return None, blocking, warnings

    if bytes_processed > max_bytes:
        blocking.append(
            f"Dry-run estimates {bytes_processed:,} bytes processed, exceeding the "
            f"{max_bytes:,} byte budget. Blocked to prevent an unexpectedly expensive query."
        )

    return bytes_processed, blocking, warnings


def run_grounding_gate(
    agent_output: dict,
    mode: str,
    schema_profile: list,
    project_id: str = None,
    max_bytes: int = 500_000_000,
    run_dry_run: bool = True,
) -> GateResult:
    blocking = []
    warnings = []
    dry_run_bytes = None

    if mode == "answer_question":
        sql = agent_output.get("sql", "")
        if not sql:
            return GateResult(False, ["No SQL present in agent output."], [], None)

        sql_blocking, sql_warnings = validate_sql_grounding(sql, schema_profile)
        blocking.extend(sql_blocking)
        warnings.extend(sql_warnings)

        if not blocking and run_dry_run and project_id:
            dry_run_bytes, dry_blocking, dry_warnings = dry_run_cost_check(sql, project_id, max_bytes)
            blocking.extend(dry_blocking)
            warnings.extend(dry_warnings)

    elif mode == "suggest_questions":
        q_blocking, q_warnings = validate_questions_grounding(agent_output.get("questions", []), schema_profile)
        blocking.extend(q_blocking)
        warnings.extend(q_warnings)

    else:
        blocking.append(f"Unknown mode: '{mode}'")

    return GateResult(passed=(len(blocking) == 0), blocking_violations=blocking, warnings=warnings, dry_run_bytes=dry_run_bytes)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Path to a saved agent_core.py JSON output file")
    parser.add_argument("--mode", choices=["answer", "suggest"], required=True)
    parser.add_argument("--project", help="GCP project ID for dry-run cost check (omit to skip)")
    parser.add_argument("--max-bytes", type=int, default=500_000_000)
    args = parser.parse_args()

    with open(args.input) as f:
        agent_output = json.load(f)

    schema_profile = load_schema_profile()
    internal_mode = "answer_question" if args.mode == "answer" else "suggest_questions"

    result = run_grounding_gate(
        agent_output,
        internal_mode,
        schema_profile,
        project_id=args.project,
        max_bytes=args.max_bytes,
        run_dry_run=bool(args.project),
    )

    print(f"PASSED: {result.passed}")
    if result.dry_run_bytes is not None:
        print(f"Dry-run bytes: {result.dry_run_bytes:,}")
    if result.blocking_violations:
        print("\nBLOCKING VIOLATIONS:")
        for v in result.blocking_violations:
            print(f"  - {v}")
    if result.warnings:
        print("\nWARNINGS:")
        for w in result.warnings:
            print(f"  - {w}")


if __name__ == "__main__":
    main()


# --- Integration note for agent_core.py (Phase 2) ---
# Replace node_lightweight_check's cardinality logic with a call into this
# module's run_grounding_gate(), since this supersedes it (blocking instead
# of warning, plus table/column/join checks Phase 2 never had). Not wired
# in automatically here -- deliberate integration step to do once this
# module is tested standalone against real Phase 2 output, same discipline
# as Phase 1's profiler being verified before Phase 2 consumed it.