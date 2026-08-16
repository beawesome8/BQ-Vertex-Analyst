# Phase 3 Notes

Grounding/validation gate: AST-based table/column existence checks
(sqlglot), join-path verification against Phase 1's inferred FKs,
cardinality guard (now blocking, not just warning), DDL/DML block, and a
real BigQuery dry-run cost check.

## Built and verified against real behavior, not memory

Before writing the SQL-parsing logic, tested sqlglot's actual AST shapes
directly rather than assuming an API from memory: confirmed
`COUNT(DISTINCT x)` parses as `Count(this=Distinct(...))`, join `ON`
clauses use `.this` / `.args['expression']` (not `.left`/`.right` as
initially assumed), and `APPROX_COUNT_DISTINCT` gets its own
`ApproxDistinct` node rather than a generic function call. Writing
against confirmed behavior instead of guessed behavior avoided at least
two wrong assumptions that would have silently broken the gate.

## Verified: catches the exact proven Phase 2 failure

`SELECT COUNT(DISTINCT user_id) FROM events` (the query that slipped
through Phase 2's system instruction) now returns `PASSED: False` with a
blocking `CARDINALITY GUARD` violation when run through this gate against
the real `schema_profile.json` -- not a synthetic test, the actual
project data.

## Finding: dataset qualification is an execution-environment concern, not a generation concern

First dry-run attempt on a clean query failed: BigQuery rejected
`FROM order_items` (no dataset prefix) as invalid, since the client had
no default dataset configured. The offline schema checks had all passed
correctly -- this was BigQuery itself rejecting the query, independent of
the schema-profile logic.

Considered fixing this by having the agent generate fully-qualified table
names (`bigquery-public-data.thelook_ecommerce.order_items`) instead.
Rejected: that would push an execution-environment detail (which dataset
this happens to live in) into the LLM's output, when it's actually a
property of where the query runs, not what it means. Fixed instead by
setting `default_dataset` on the dry-run `QueryJobConfig`, so unqualified
table names (matching what the schema profile and the agent's prompt both
use) resolve correctly. Kept the concern in the execution layer, not the
prompt.

## Design: fail-fast ordering matters

Checks run cheapest-first: statement type, table existence, column
existence, join-path (warning only), cardinality guard, and only then --
if everything else passed -- an actual BigQuery API call for the dry-run
cost estimate. Verified this ordering works as intended: the blocked
cardinality-violation case never reached the dry-run stage (no
`Dry-run bytes` printed), avoiding a wasted API call on a query already
known to be untrustworthy.

## Known limitation carried forward, not resolved

`validate_questions_grounding()` (for `suggest_questions` mode) is a
keyword/text heuristic scan, not an AST-based structural check -- there's
no SQL to parse in that mode, just prose. This is weaker than the SQL-mode
check and documented as such, not claimed to be equivalent. Table
existence checks in that mode ARE structural (checks `relevant_tables`
against the real table list), but the cardinality-language detection is
pattern-matching on question/rationale text, which will miss things an
AST check would catch.

## Integration note

Not yet wired into `agent_core.py`'s LangGraph as a node -- built and
verified standalone first, same discipline as Phase 1's profiler being
verified before Phase 2 consumed its output. Wiring `run_grounding_gate()`
into the graph (replacing Phase 2's now-superseded narrow cardinality
check) is the next concrete step, either closing out Phase 3 or opening
Phase 4.
