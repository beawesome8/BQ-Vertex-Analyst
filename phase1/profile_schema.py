"""
BQ-Vertex-Analyst — Phase 1: Schema Profiler (v2 — TheLook eCommerce)
=======================================================================

Dataset changed from GA4 (single wide event table) to
bigquery-public-data.thelook_ecommerce (multi-table relational schema:
users, orders, order_items, products, inventory_items,
distribution_centers, events).

Why this replaces the GA4 version
----------------------------------
GA4 tested nested-field flattening (ARRAY<STRUCT<...>>). TheLook tests
join-path reasoning across foreign keys -- a more common and more
corporate-relevant failure mode for LLM-generated SQL: joining on the
wrong key, dropping a join silently, producing a plausible-but-wrong
result. The grounding gate in Phase 3 will validate join paths against
actual FK relationships, not just column existence.

Cost discipline
----------------
- Table-level structure and row counts come from INFORMATION_SCHEMA and
  __TABLES__ (metadata only, free).
- Per-column cardinality/null-rate sampling uses a bounded TABLESAMPLE
  with a dry-run cost check printed before every real query.
- FK relationship detection is heuristic (naming convention: `<table>_id`)
  since these public tables don't have declared foreign key constraints --
  flagged clearly in the output as "inferred", not "declared", so Phase 2
  doesn't treat a guess as ground truth.

Prerequisites (run yourself -- not executable from this environment)
---------------------------------------------------------------------
    pip install google-cloud-bigquery
    gcloud auth application-default login

Usage
-----
    python profile_schema.py --project YOUR_GCP_PROJECT_ID
"""

import argparse
import json
import re
from dataclasses import dataclass, asdict, field
from google.cloud import bigquery

DATASET = "bigquery-public-data.thelook_ecommerce"
TABLES = [
    "users",
    "orders",
    "order_items",
    "products",
    "inventory_items",
    "distribution_centers",
    "events",
]


@dataclass
class ColumnProfile:
    table: str
    name: str
    field_type: str
    mode: str
    approx_distinct: int | None = None
    null_rate: float | None = None
    inferred_fk_target: str | None = None  # e.g. "users.id" -- heuristic, not declared


@dataclass
class TableProfile:
    table: str
    row_count: int
    columns: list = field(default_factory=list)


def get_client(project: str) -> bigquery.Client:
    return bigquery.Client(project=project)


def get_row_counts(client: bigquery.Client) -> dict:
    """Free -- pulls approximate row counts from table metadata, no scan."""
    query = f"""
        SELECT table_id, row_count
        FROM `{DATASET}.__TABLES__`
        WHERE table_id IN ({", ".join(f"'{t}'" for t in TABLES)})
    """
    job = client.query(query)
    return {row.table_id: row.row_count for row in job.result()}


def list_columns(client: bigquery.Client, table: str) -> list:
    query = f"""
        SELECT column_name, data_type, is_nullable
        FROM `{DATASET}.INFORMATION_SCHEMA.COLUMNS`
        WHERE table_name = '{table}'
        ORDER BY ordinal_position
    """
    job = client.query(query)
    columns = []
    for row in job.result():
        columns.append(
            ColumnProfile(
                table=table,
                name=row.column_name,
                field_type=row.data_type,
                mode="NULLABLE" if row.is_nullable == "YES" else "REQUIRED",
            )
        )
    return columns


def infer_fk(col: ColumnProfile, known_tables: list) -> str:
    """
    Heuristic FK detection: a column named e.g. 'user_id' on the 'orders'
    table is inferred to reference 'users.id'. This is a guess based on
    naming convention -- these public tables have no declared FK constraints.
    Phase 3's grounding gate must treat this as a hint to validate against
    actual data, not as ground truth to trust blindly.
    """
    match = re.match(r"^(\w+)_id$", col.name)
    if not match:
        return None
    referenced = match.group(1)
    candidates = [f"{referenced}s", referenced]  # naive pluralization
    for c in candidates:
        if c in known_tables and c != col.table:
            return f"{c}.id"
    return None


def sample_column_stats(
    client: bigquery.Client, col: ColumnProfile, row_count: int, sample_fraction: float = 0.1
) -> None:
    """Skip sampling on tiny tables -- TABLESAMPLE on <10k rows isn't meaningful."""
    if row_count < 10_000:
        query = f"""
            SELECT
                APPROX_COUNT_DISTINCT({col.name}) AS approx_distinct,
                COUNTIF({col.name} IS NULL) / COUNT(*) AS null_rate
            FROM `{DATASET}.{col.table}`
        """
    else:
        query = f"""
            SELECT
                APPROX_COUNT_DISTINCT({col.name}) AS approx_distinct,
                COUNTIF({col.name} IS NULL) / COUNT(*) AS null_rate
            FROM `{DATASET}.{col.table}` TABLESAMPLE SYSTEM ({sample_fraction * 100} PERCENT)
        """

    dry = client.query(query, job_config=bigquery.QueryJobConfig(dry_run=True))
    est_bytes = dry.total_bytes_processed or 0
    if est_bytes > 200_000_000:  # 200MB guardrail per column
        print(f"  [{col.table}.{col.name}] SKIPPED -- est. {est_bytes} bytes exceeds budget")
        return

    job = client.query(query)
    row = list(job.result())[0]
    col.approx_distinct = row.approx_distinct
    col.null_rate = round(row.null_rate, 4) if row.null_rate is not None else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--out", default="schema_profile.json")
    args = parser.parse_args()

    client = get_client(args.project)

    print(f"Profiling {DATASET} ({len(TABLES)} tables) ...")
    row_counts = get_row_counts(client)

    table_profiles = []
    for table in TABLES:
        print(f"\n-- {table} ({row_counts.get(table, '?')} rows)")
        columns = list_columns(client, table)
        for col in columns:
            col.inferred_fk_target = infer_fk(col, TABLES)
            if col.inferred_fk_target:
                print(f"  [{col.name}] inferred FK -> {col.inferred_fk_target}")
            sample_column_stats(client, col, row_counts.get(table, 0))
        table_profiles.append(TableProfile(table=table, row_count=row_counts.get(table, 0), columns=columns))

    output = [
        {**asdict(tp), "columns": [asdict(c) for c in tp.columns]} for tp in table_profiles
    ]
    with open(args.out, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nWrote schema profile to {args.out}")
    print("Next: check the inferred FK list by eye. Any wrong guesses there")
    print("become exactly the join-path errors Phase 3's grounding gate needs")
    print("to catch later -- so it's worth being skeptical of this output now,")
    print("not trusting it blindly.")


if __name__ == "__main__":
    main()
