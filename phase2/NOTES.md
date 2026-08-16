# Phase 2 Notes

LangGraph agent core: schema-aware SQL generation (`answer_question` mode)
and ranked analytical question recommendation (`suggest_questions` mode),
via Gemini on Vertex AI through the current `google-genai` SDK (the older
`vertexai.generative_models` module was deprecated and its removal date
has already passed as of this build).

## Finding: prompt instructions are not enforcement

The system instruction for `answer_question` mode explicitly stated:
"Never state or imply how many distinct values a column has... for any
column marked CARDINALITY UNKNOWN." Asked directly "How many unique users
have triggered events in the events table?", the model generated
`SELECT COUNT(DISTINCT user_id) FROM events` anyway -- with zero caveats,
presented as a normal trustworthy query -- despite `events.user_id` being
explicitly marked `cardinality_reliable: false` in the schema context it
was given.

This is not a code bug. It's proof that an LLM told "don't do X" in a
system prompt can still do X. That's the actual justification for Phase 3
existing as a separate, code-based validation layer rather than trusting
the model to self-police -- this is the concrete, reproduced example that
motivates it, not an abstract argument.

## Fix: narrow, targeted, code-level guard (not the full Phase 3 gate)

Added `_check_cardinality_violations()`: regex-scans generated SQL for
`COUNT(DISTINCT ...)`, `APPROX_COUNT_DISTINCT(...)`, and bare
`SELECT DISTINCT ...` over any column flagged `cardinality_reliable=False`,
cross-referenced against the schema profile. Runs inside
`node_lightweight_check`, flags via `warnings` and a
`cardinality_guard_violation: true` field in the result -- does not block
generation, since blocking/allow decisions are explicitly Phase 3's scope,
not Phase 2's.

Verified against three cases:
1. **True positive** -- the exact reproduced failure above: correctly
   flagged, `cardinality_guard_violation: true`.
2. **Negative control** -- an unrelated aggregation query (average order
   value by state): correctly passed with no false positive.
3. **Regression check** -- `suggest_questions` mode re-run, unaffected
   (this check doesn't run in that mode -- see known limitation below).

## Known limitation, documented rather than chased

`node_lightweight_check`'s cardinality guard only runs for
`answer_question` mode's generated SQL. `suggest_questions` mode has NO
code-level enforcement -- if the model proposed a question requiring
cardinality reasoning on an unreliable column, nothing would catch it
except the same prompt instruction already proven insufficient once.
Two `suggest` runs avoided the `events` table entirely, which is weak
evidence of correct behavior, not proof -- it's equally consistent with
the model just not prioritizing that table for unrelated reasons.

Deliberately not building a second narrow patch for this: the SQL-mode
fix was built against a concrete, reproduced failure. There is no
equivalent reproduced failure for `suggest` mode yet, and writing a check
against an untested hypothesis (scanning prose for column names, which is
noisier and more false-positive-prone than regexing structured SQL) risks
a guard that feels safe without being validated. Left for Phase 3's
proper grounding gate, which should cover both modes correctly rather
than accumulating narrow prompt-mode-specific patches.

## SDK / environment notes

- Vertex AI's generative AI surface was rebranded "Gemini Enterprise Agent
  Platform" during this build. `vertexai.generative_models` (the module
  used by most existing tutorials) was deprecated June 24, 2025 and its
  removal date has already passed -- used `google-genai`
  (`from google import genai`) instead.
- Model ID (`gemini-2.5-flash`) chosen as the most consistently-documented
  stable option at build time; Gemini model naming was in visible flux
  (2.5, 3.5, 3.6, 3.7 variants all appearing in docs of different ages)
  -- worth re-checking Model Garden before assuming this stays current.
- The "AFC" (automatic function calling) warning printed on every run is
  cosmetic -- irrelevant since no tools are passed to `generate_content`.
