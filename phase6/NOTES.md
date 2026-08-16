# Phase 6 Notes

Eval harness against the live pipeline: 8-case golden set, four check
types (adversarial guard robustness, scalar accuracy against
independently-computed reference queries, structural pass, suggest-mode
sanity).

## Design choice: no hardcoded answer key

`scalar_accuracy` cases don't compare against a static expected value
written into `golden_set.json`. The harness runs its own small,
hand-written reference query directly against BigQuery at eval time and
compares the agent's cited value against that live result. A hardcoded
answer key would have been a number I couldn't verify from a sandbox
with no BigQuery access -- a live-computed reference can't go stale and
isn't a guess.

## Results (first real run)

```
TOTAL: 8/8 passed (100%)
Adversarial guard catch rate: 1.0
Scalar accuracy rate: 1.0
Average latency: 14.911s
Average bytes billed per query: 18,874,368
```

## What this result actually supports, and what it doesn't

100% on 8 cases is a real, honest signal -- not a statistical claim.
Only 2 cases per adversarial and scalar-accuracy category. The correct
framing for a CV or interview is "100% on an 8-case golden set including
adversarial paraphrase testing," not "the system is 100% accurate."
Overclaiming from a small n is a bigger credibility risk than the eval
having a gap -- flagged here explicitly so it's never accidentally
overstated later.

What the result DOES support: `blocked_events_user_id_rephrased` uses
different wording from the original proven Phase 2 failure
(`blocked_events_user_id_direct`) and was caught identically -- real
evidence the cardinality guard enforces the actual semantic violation
(a distinct-value claim on a column marked unreliable), not just
pattern-matching the one sentence that originally exposed the bug.

Two honest, non-flattering numbers worth keeping visible, not just the
pass rate:
- **Average latency 14.9s**, with the suggest-mode case at 28s. Slow for
  an interactive demo -- not a defect, a real characteristic worth
  naming before an interviewer notices it live.
- **~18.9MB average bytes billed per query.** Modest, but non-zero, real
  cost -- not free.

## Verification approach

Scoring logic (`numeric_match`, `run_case`, aggregation) tested offline
against synthetic pipeline states before ever running live -- eight edge
cases for the numeric matcher (currency formatting, thousands
separators, zero-value comparison, non-numeric values mixed into
cited_values) and six case-runner scenarios, including confirming a
pipeline exception on one case fails that case cleanly rather than
crashing the whole eval run. `run_reference_query()` and the full live
pipeline were unverified until this run -- same boundary as every phase
since Phase 2's live-API dependency began.
