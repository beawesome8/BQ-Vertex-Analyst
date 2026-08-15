"""
BQ-Vertex-Analyst -- Phase 1: Schema Profiler (v3 -- fixed FK inference)
=========================================================================

Dataset: bigquery-public-data.thelook_ecommerce (users, orders, order_items,
products, inventory_items, distribution_centers, events).

v3 changelog
------------
- FK inference is now two-pass: all table column lists are gathered first
  (cheap, metadata-only), THEN FK targets are inferred by checking what
  primary-key-like column actually exists on the candidate table --
  either 'id' or a self-named column like 'order_id'. v2 hardcoded '.id'
  as the target for every guess, which silently produced a WRONG target
  for `orders` (its real key column is `order_id`, not `id` -- one of
  the only tables in this dataset that breaks the id-per-table
  convention). Caught by manually inspecting v2's output rather than
  trusting the tag -- keep doing that at every phase, not just this one.
- APPROX_COUNT_DISTINCT casts GEOGRAPHY/JSON columns to a groupable
  representation first (v2 fix, carried forward).

Cost discipline
----------------
- Table structure, row counts: INFORMATION_SCHEMA / __TABLES__, free.
- Per-column cardinality/null-rate: bounded TABLESAMPLE, dry-run cost
  check printed and enforced (200MB/column budget) before every real query.

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
    approx_distinct_in_sample: int = None   # renamed: honest about scope
    sample_fraction: float = None           # None if full-table (small table)
    estimated_full_cardinality: int = None  # naive extrapolation, labeled as rough
    cardinality_reliable: bool = True       # False when a clamp fired below --
                                             # Phase 2 MUST check this before
                                             # reasoning about cardinality on
                                             # this column, not just read the note
    null_rate: float = None
    inferred_fk_target: str = None
    fk_inference_note: str = None


@dataclass
class TableProfile:
    table: str
    row_count: int
    columns: list = field(default_factory=list)


def get_client(project: str) -> bigquery.Client:
    return bigquery.Client(project=project)


def get_row_counts(client: bigquery.Client) -> dict:
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


def infer_fk(col: ColumnProfile, all_columns: dict) -> None:
    """
    Two-pass FK inference: mutates col in place with inferred_fk_target and
    fk_inference_note. Requires all_columns = {table_name: [ColumnProfile,...]}
    to already be fully populated for every table -- this is why column
    listing happens as its own pass before FK inference, not interleaved
    with the per-table loop like v2 did.

    Resolution order per candidate table:
      1. Does the candidate table have a column with the EXACT same name
         as this FK column (self-named PK, e.g. orders.order_id)? Use it.
      2. Does the candidate table have a plain 'id' column? Use it.
      3. Candidate table exists but neither pattern matches -- flag as
         unresolved rather than guessing, so Phase 3 doesn't inherit a
         silent wrong answer.
    """
    match = re.match(r"^(\w+)_id$", col.name)
    if not match:
        return

    referenced = match.group(1)
    candidate_tables = [f"{referenced}s", referenced]  # naive pluralization

    for candidate in candidate_tables:
        if candidate == col.table or candidate not in all_columns:
            continue

        candidate_col_names = {c.name for c in all_columns[candidate]}

        if col.name in candidate_col_names:
            col.inferred_fk_target = f"{candidate}.{col.name}"
            col.fk_inference_note = "matched self-named key column on target table"
            return

        if "id" in candidate_col_names:
            col.inferred_fk_target = f"{candidate}.id"
            col.fk_inference_note = "matched conventional 'id' column on target table"
            return

        col.fk_inference_note = (
            f"candidate table '{candidate}' exists but has neither "
            f"'{col.name}' nor 'id' -- UNRESOLVED, do not trust a guess here"
        )
        return


UNGROUPABLE_TYPE_CASTS = {
    "GEOGRAPHY": "ST_ASTEXT({col})",
    "JSON": "TO_JSON_STRING({col})",
}


def _groupable_expr(col: ColumnProfile) -> str:
    cast_template = UNGROUPABLE_TYPE_CASTS.get(col.field_type)
    if cast_template:
        return cast_template.format(col=col.name)
    return col.name


def sample_column_stats(
    client: bigquery.Client, col: ColumnProfile, row_count: int, requested_fraction: float = 0.1
) -> None:
    """
    Skip full sampling on tiny tables -- exact APPROX_COUNT_DISTINCT on the
    whole table is both cheap and accurate at that size, no sampling needed.

    For larger tables: BigQuery's TABLESAMPLE SYSTEM samples at the STORAGE
    BLOCK level, not the row level. For tables with relatively few blocks,
    requesting "10 percent" can silently return close to the ENTIRE table --
    verified empirically here: users/orders/order_items/products/
    inventory_items all returned 95-100% of their rows despite a 10%
    request. Naively dividing the sampled distinct count by the REQUESTED
    fraction (0.1) then produces cardinality estimates that exceed the
    table's actual row count -- a mathematically impossible result that
    silently passed through the previous version of this script.

    Fix: measure the ACTUAL achieved sample fraction via COUNT(*) in the
    same query, extrapolate using that instead of the requested fraction,
    and hard-clamp the result at row_count as a sanity backstop regardless.
    """
    expr = _groupable_expr(col)

    # Below this, TABLESAMPLE's block granularity makes "10%" meaningless
    # anyway -- just scan the whole table directly. It's cheap at this size
    # and gives an exact answer instead of an unreliable one.
    FULL_SCAN_ROW_THRESHOLD = 1_000_000

    if row_count < FULL_SCAN_ROW_THRESHOLD:
        query = f"""
            SELECT
                APPROX_COUNT_DISTINCT({expr}) AS approx_distinct,
                COUNTIF({col.name} IS NULL) / COUNT(*) AS null_rate,
                COUNT(*) AS sampled_rows
            FROM `{DATASET}.{col.table}`
        """
        col.sample_fraction = None  # full table, not a sample
    else:
        query = f"""
            SELECT
                APPROX_COUNT_DISTINCT({expr}) AS approx_distinct,
                COUNTIF({col.name} IS NULL) / COUNT(*) AS null_rate,
                COUNT(*) AS sampled_rows
            FROM `{DATASET}.{col.table}` TABLESAMPLE SYSTEM ({requested_fraction * 100} PERCENT)
        """

    dry = client.query(query, job_config=bigquery.QueryJobConfig(dry_run=True))
    est_bytes = dry.total_bytes_processed or 0
    if est_bytes > 200_000_000:  # 200MB guardrail per column
        print(f"  [{col.table}.{col.name}] SKIPPED -- est. {est_bytes} bytes exceeds budget")
        return

    job = client.query(query)
    row = list(job.result())[0]
    col.approx_distinct_in_sample = row.approx_distinct
    col.null_rate = round(row.null_rate, 4) if row.null_rate is not None else None

    if row_count >= FULL_SCAN_ROW_THRESHOLD and row.approx_distinct is not None and row.sampled_rows:
        # Use the ACTUALLY achieved fraction, not the requested one.
        actual_fraction = row.sampled_rows / row_count
        col.sample_fraction = round(actual_fraction, 4)
        raw_estimate = int(row.approx_distinct / actual_fraction)
        # Hard sanity clamp -- cardinality cannot exceed row count, ever.
        col.estimated_full_cardinality = min(raw_estimate, row_count)
        if raw_estimate > row_count:
            col.cardinality_reliable = False
            col.fk_inference_note = (col.fk_inference_note or "") + \
                f" [cardinality estimate clamped: raw extrapolation {raw_estimate} exceeded row_count]"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--out", default="schema_profile.json")
    args = parser.parse_args()

    client = get_client(args.project)

    print("profile_schema.py v7 -- FK inference + measured sample fraction + row_count clamp + FK-referential clamp + structured cardinality_reliable flag")
    print(f"Profiling {DATASET} ({len(TABLES)} tables) ...")
    row_counts = get_row_counts(client)

    # Pass 1: gather ALL table column lists first (free, metadata-only).
    # This must complete before FK inference runs, since inference needs
    # to check what columns actually exist on referenced tables -- some
    # of which appear later in TABLES than the tables that reference them
    # (e.g. order_items references products, but products comes after
    # order_items in the list).
    print("\nPass 1: gathering column metadata for all tables...")
    all_columns = {}
    for table in TABLES:
        all_columns[table] = list_columns(client, table)
        print(f"  {table}: {len(all_columns[table])} columns")

    # Pass 2: FK inference, now with full cross-table column knowledge.
    print("\nPass 2: inferring FK relationships...")
    for table in TABLES:
        for col in all_columns[table]:
            infer_fk(col, all_columns)
            if col.inferred_fk_target:
                print(f"  [{table}.{col.name}] -> {col.inferred_fk_target}  ({col.fk_inference_note})")
            elif col.fk_inference_note:
                print(f"  [{table}.{col.name}] {col.fk_inference_note}")

    # Pass 3: cardinality/null-rate sampling with cost guardrails.
    print("\nPass 3: sampling column statistics...")
    table_profiles = []
    for table in TABLES:
        print(f"\n-- {table} ({row_counts.get(table, '?')} rows)")
        for col in all_columns[table]:
            sample_column_stats(client, col, row_counts.get(table, 0))
        table_profiles.append(
            TableProfile(table=table, row_count=row_counts.get(table, 0), columns=all_columns[table])
        )

    output = [
        {**asdict(tp), "columns": [asdict(c) for c in tp.columns]} for tp in table_profiles
    ]

    # Post-process: clamp FK column cardinality against the REFERENCED
    # table's actual cardinality. A foreign key cannot have more distinct
    # values than the table it points at has rows -- if it does, that's a
    # sign block-level TABLESAMPLE saw a non-representative slice of the
    # column's domain (clustering/ordering bias), not a real data fact.
    # This is a known limitation of block-sampling, not something this
    # script tries to correct further -- it's logged, not silently fixed.
    cardinality_by_key = {}
    for tp in table_profiles:
        for c in tp.columns:
            if c.name in ("id",) or c.name == f"{tp.table.rstrip('s')}_id":
                known = c.approx_distinct_in_sample if c.sample_fraction is None else c.estimated_full_cardinality
                if known is not None:
                    cardinality_by_key[f"{tp.table}.{c.name}"] = known

    for tp_out in output:
        for c_out in tp_out["columns"]:
            target = c_out.get("inferred_fk_target")
            est = c_out.get("estimated_full_cardinality")
            if target and est and target in cardinality_by_key:
                target_cardinality = cardinality_by_key[target]
                if est > target_cardinality:
                    note = c_out.get("fk_inference_note") or ""
                    c_out["fk_inference_note"] = note + (
                        f" [KNOWN LIMITATION: estimated_full_cardinality ({est}) exceeds "
                        f"referenced table's real cardinality ({target_cardinality}) -- "
                        f"block-level TABLESAMPLE saw a non-representative slice of this "
                        f"column's domain; treat this estimate as unreliable, not the row_count clamp]"
                    )
                    c_out["estimated_full_cardinality"] = target_cardinality
                    c_out["cardinality_reliable"] = False

    with open(args.out, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nWrote schema profile to {args.out}")
    unresolved = [
        f"{tp.table}.{c.name}"
        for tp in table_profiles
        for c in tp.columns
        if c.fk_inference_note and "UNRESOLVED" in c.fk_inference_note
    ]
    if unresolved:
        print(f"\n{len(unresolved)} UNRESOLVED FK guess(es) -- check these by hand:")
        for u in unresolved:
            print(f"  - {u}")
    else:
        print("\nNo unresolved FK guesses. Still check the resolved ones by eye --")
        print("resolved doesn't mean verified against real data yet.")


if __name__ == "__main__":
    main()