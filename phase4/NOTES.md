# Phase 4 Notes

Query execution against real BigQuery data, grounded natural-language
answer generation, and a hallucination cross-check against actual
returned rows.

## Finding: a hallucination check passing doesn't mean the answer is good

First real-data test ("average order value by state," 228 rows returned)
produced an answer that was technically perfect and practically useless:
every cited value genuinely appeared in the row data (hallucination check
passed), but the "answer" was all 50 sampled rows dumped as a
comma-separated wall of raw floats -- not a synthesized response to the
question asked.

Root cause: the original system instruction said "list every value your
answer relies on" without ever rewarding *relying on fewer values*.
Combined with "never state an ungrounded fact," the model's safest
strategy was exhaustive citation rather than synthesis -- grounding and
usefulness are different axes, and optimizing only the one that's easy to
check in code (is every cited value real?) said nothing about the one
that actually matters to a user (is this a good answer?).

Fixed by rewriting the instruction to explicitly require synthesis
(highlight extremes/range/pattern, not a row-by-row recitation) and to
constrain `cited_values` to only what the synthesized prose actually
references. Re-ran the identical question against the identical data:
answer dropped from a 50-entry list to two sentences citing exactly 4
values (the min and max state/value pairs), hallucination check still
passed clean. Confirms the fix addressed the real problem rather than
just suppressing the symptom.

## Finding: BigQuery result caching produces a real, expected billing discrepancy

Re-running the identical query (same SQL text) after the instruction fix
showed `bytes billed: 0`, versus 31,457,280 bytes on the first run. Not a
bug -- BigQuery's query result cache returns free, cached results for
identical query text run within its cache window. `execute_query()`
deliberately disables caching only on the dry-run re-check
(`use_query_cache=False`), so that cost estimate always reflects a fresh
scan; the real execution allows caching by default, which is the correct
choice for actual runs (you want cost savings on repeated queries).
Documented here rather than left as an unexplained discrepancy in the
commit history.

## Design: defense in depth, not redundant work

`execute_query()` re-runs a dry-run cost check immediately before
executing, even though Phase 3's grounding gate already dry-ran the same
SQL. Deliberate: the gate and this execution are separate calls that
could, in principle, see a changed table between them (a table that grows
between gate approval and execution). Re-checking is cheap; skipping it
and being wrong about cost is not.

## Known limitation, documented rather than solved

`check_hallucination()` is a naive string-containment match, not semantic
verification. Reformatted values (added currency symbols, rounding,
thousands separators) can false-positive as hallucinations even when
genuinely grounded -- demonstrated deliberately in local testing before
ever running against live data (a cited `"$87.34"` doesn't literally
match a raw row value of `87.34`). This trades some false positives for
zero false negatives on exact-match hallucinations, the safer failure
direction for a grounding check, but it's a real limitation, not
something more sophisticated fuzzy matching would eliminate without
adding false-negative risk.

This phase also does NOT retry or auto-correct on a failed hallucination
check -- it detects, it doesn't remediate. Regenerating with the
violation as feedback is a reasonable future enhancement, not built here.

## Verification approach

Could not test `execute_query()` or the live Gemini call from the build
sandbox (no BigQuery/Vertex AI network access there) -- only
`check_hallucination()`'s pure-Python logic was verified locally before
handoff, deliberately including a test that demonstrates its known
false-positive limitation rather than only testing the happy path.
`execute_query()` and `generate_grounded_answer()` were verified for the
first time against real APIs, by the user, not pre-tested the way Phase
3's sqlglot logic was.

## Not yet wired into agent_core.py

Same discipline as Phase 3: built and verified standalone before
touching the LangGraph integration. Wiring this in as a node after the
grounding gate (only running for answer_question mode, only when the
gate passed) is the next concrete step.
