# BQ-Vertex-Analyst

**Status:** Phase 1 complete (tag `v0.1-dataset-profile`) — merged scope.
Absorbs the previously-scoped
"SQL Query Intelligence Agent" (see Section 3, Scope History).
**Owner:** Aman Benjamin Emmanuel
**Repo:** `BQ-Vertex-Analyst`

---

## 1. Problem Statement

Portfolio gap analysis (Aug 2026): every GCP-touching project (Purchase-Intent-GCP,
FreshCart) is classic ML/data engineering. Every GenAI/agentic project (DocuVet,
Recurring Research Agent, PromptGuard, CodeMark) has no GCP surface. Zero
evidence exists for "LLM on GCP" / "Vertex AI + Gemini" JD requirements.

Separately, a SQL Query Intelligence Agent had been scoped (schema profiling +
ranked analytical question recommendation + grounding/validation gate against
hallucinated table references) but never built, and was sitting on the backlog
unresolved.

This project merges the two. One repo now makes two distinct, separately
citable claims instead of two half-finished projects making none.

## 2. What It Is

```
User question (English) OR "suggest questions worth asking" mode
   -> Schema profiler: column types, cardinality, null patterns (BigQuery dataset)
   -> LangGraph agent node
   -> Gemini (via Vertex AI) generates candidate SQL / ranked question list
      against the profiled schema
   -> Validation & grounding gate:
        - table/column reference check against actual schema (catches
          hallucinated references before execution)
        - BigQuery dry-run cost estimate, scan-size limit
        - DDL/DML blocklist, read-only service account
   -> Execute against BigQuery
   -> Gemini generates grounded natural-language answer from actual returned rows
   -> Hallucination check: answer claims cross-checked against query result set
   -> Response returned with the SQL shown (transparency)
```

Dataset: a real BigQuery public dataset (candidates: Google Trends, GA4
ecommerce sample, Stack Overflow archive — final pick made in Phase 1 based
on schema richness).

## 3. Scope History (read before building)

**Original SQL Query Intelligence Agent decision (Aug 12 scoping session):**
backend was set to Postgres, explicitly *not* BigQuery, because BigQuery/SQL
reasoning was already claimed via Denodo/BigQuery professional experience —
the project needed to prove new ground, not re-prove a claimed skill.

**Why this merge reverses that, correctly:** this project's claim isn't
"I can write BigQuery SQL" (already claimed). It's "I have hands-on Vertex
AI/Gemini experience" (currently zero evidence anywhere). BigQuery here is
the substrate for the Vertex AI claim, not the claim itself. The original
objection doesn't apply to this framing.

**Cost of the merge — do not lose this:** the SQL Query Intelligence Agent
was scoped to target Data Analyst / Data Engineer JDs specifically, an
audience that won't care about "Vertex AI agent." Mitigation: this repo must
produce two separately citable claims, not one blended pitch —
(a) schema-aware SQL reasoning and query validation, (b) Vertex AI/Gemini
agent orchestration. Write CV bullets and the repo README to make both
claims independently legible. See Section 9.

## 4. Why Not the Other Alternative (BigQuery ML + Vertex AI Pipelines)

Rejected for this slot: structurally identical to Purchase-Intent-GCP
(managed training pipeline, CI/CD gate, deployed endpoint, Terraform). Same
skeleton, different dataset — narrow repetition, not range. Kept only as a
future possibility if a specific JD demands classic Vertex AI Pipelines
experience by name.

## 5. Explicit Non-Goals

- Not a general-purpose text-to-SQL product. One dataset, one schema, done
  honestly.
- Not using Vertex AI AutoML or BigQuery ML — no training pipeline here.
  This is agent + schema reasoning + guardrails + serving, not model training.
- Not fabricating headline metrics before Phase 6 (eval harness) actually runs.

## 6. Phased Build Plan

Each phase ends in a git commit + tag. No silent handoffs.

| Phase | Deliverable | Tag |
|---|---|---|
| 1 | Dataset selection + schema profiling (column types, cardinality, null patterns) + cost baseline | `v0.1-dataset-profile` |
| 2 | LangGraph agent core: schema-aware prompt, Gemini (Vertex AI) SQL generation + ranked-question recommendation mode | `v0.2-agent-core` |
| 3 | Validation & grounding gate: table/column reference check, BigQuery dry-run cost estimate, scan-size limit, DDL/DML blocklist, read-only service account | `v0.3-validation-gate` |
| 4 | Execution + grounded answer generation + hallucination cross-check against returned rows | `v0.4-grounded-answer` |
| 5 | FastAPI service + minimal front end (Streamlit or React/Vite) for live demo | `v0.5-service` |
| 6 | Eval harness: golden set covering both NL->SQL correctness and recommendation quality; regression scoring (accuracy, cost per query, latency) | `v0.6-eval-harness` |
| 7 | CI/CD: GitHub Actions runs eval harness on every PR, blocks merge on regression | `v0.7-cicd-gate` |
| 8 | Deploy: Cloud Run + Terraform for IAM/dataset provisioning + BigQuery budget/cost alert | `v0.8-deploy` |

Scope is CLOSED at Phase 8 unless a specific JD justifies reopening it.

## 6a. Phase 1 Retrospective (complete — tag `v0.1-dataset-profile`)

Schema profiler for TheLook eCommerce, four iterations before the output
could be trusted. Full detail in `phase1/NOTES.md`; summary here since this
is what an interviewer reads first:

1. **FK inference assumed every table's key column is named `id`.**
   `orders` breaks that convention (its key is `order_id`), which silently
   pointed `order_items.order_id` at a nonexistent column. Fixed by looking
   up each candidate table's actual columns instead of hardcoding `.id`.

2. **Cardinality sampling trusted the requested `TABLESAMPLE` fraction (10%)
   instead of the fraction actually achieved.** Block-level sampling on
   smaller tables silently grabbed 95-100% of rows, producing extrapolated
   cardinality estimates up to 10x higher than the table's own row count —
   a mathematically impossible result. Fixed by measuring the achieved
   fraction via `COUNT(*)` and hard-clamping at `row_count` as a backstop.

3. **Even with a correct fraction, block-level sampling can see a
   non-representative slice of a column's value domain.** Caught via a
   cross-table check: `events.user_id`'s estimated cardinality exceeded the
   actual row count of `users` — impossible for a valid foreign key.
   Clamped against the referenced table's real cardinality.

4. **The clamp in (3) fixed the number but not the trust problem** — a
   downstream consumer reading only `estimated_full_cardinality` had no way
   to know it had been capped. Added a structured `cardinality_reliable`
   boolean so Phase 2 can gate on this programmatically instead of parsing
   free-text notes. `events.user_id` is currently the only column flagged
   unreliable in this dataset; its true cardinality remains unknown, only
   bounded — Phase 2 must treat it as off-limits for cardinality-dependent
   reasoning, not silently trust the capped number.

## 7. Success Metrics (to be measured, not assumed)

- Grounding gate catch rate on a deliberately adversarial eval subset with
  hallucinated table/column references injected.
- Ranked-question recommendation quality — human-judged relevance on a
  held-out schema slice.
- Hallucination check catch rate on injected wrong-number answers.
- Cost per query (BigQuery bytes billed) — report honestly.
- CI gate false-positive/false-negative rate on the golden eval set.

No number goes on the CV or repo README until Phase 6 has actually run and
produced it.

## 8. Stack

Python 3.12, LangGraph, Vertex AI (Gemini), Google BigQuery client, FastAPI,
GitHub Actions, Terraform, Cloud Run. Pairs with the in-progress GCP
Professional ML Engineer certification, same way FreshCart pairs with
Snowflake and Purchase-Intent-GCP pairs with the GCP cert already.

## 9. CV / Positioning Rule (do not skip this)

This single repo must be citable as two separate bullets, not one blended
claim:

- **Data Analyst / Data Engineer framing:** schema profiling, ranked
  analytical question generation, and a grounding/validation gate that
  catches hallucinated SQL before execution.
- **AI Engineer framing:** Gemini/Vertex AI agent orchestration via
  LangGraph, CI/CD-gated eval pipeline, deployed on Cloud Run with Terraform.

Pick the framing per JD. Never present both in the same paragraph on a CV —
it dilutes both claims.

### Drafted bullets (from verified findings, not aspirational)

**Data Analyst / Data Engineer framing:**
Built a BigQuery schema profiler in Python that surfaced and fixed three
distinct data-quality bugs during development, including a foreign-key
inference error (assumed every table's key column was named `id`, which
silently broke on a table using a self-named key) and a sampling bug
where block-level `TABLESAMPLE` silently returned up to 10x inflated
cardinality estimates; added a structured reliability flag so downstream
consumers can programmatically distinguish verified statistics from
unreliable ones instead of trusting a free-text caveat.

**AI Engineer framing:**
Built a LangGraph agent using Gemini on Vertex AI for schema-aware SQL
generation and question recommendation; proved that prompt-only
instructions are insufficient for enforcement by reproducing a case
where the model stated a fabricated cardinality claim despite an
explicit system instruction forbidding it, then built and validated a
targeted, code-level guard (true positive and negative-control tested)
to catch the same failure class going forward.

**Still missing, tracked deliberately:** every other completed project has
one crisp headline metric (84 retraining cycles, 60.8% cost reduction,
93.3% schema validity drop). This project doesn't have that yet — Phase 6
(eval harness) is where it should come from: guard catch rate on an
adversarial set, or hallucination rate before/after the grounding gate.
Don't let this section ship on a CV without that number once it exists.

## 10. Dataset Decision (resolved)

**Chosen:** `bigquery-public-data.thelook_ecommerce` — multi-table relational
schema (users, orders, order_items, products, inventory_items,
distribution_centers, events).

**Rejected:**
- Google Trends — too flat, nothing for schema profiling or the
  recommendation engine to do real work on.
- GA4 ecommerce sample — single wide event table with nested
  ARRAY<STRUCT<...>> fields. Tests nested-field flattening, a narrower and
  less corporate-relevant problem than join-path reasoning.
- Stack Overflow archive — rich but large; cost risk on exploratory queries
  during early iteration.

**Why TheLook wins:** multi-table + foreign keys means the agent has to
reason about join paths, not just column existence. Joining on the wrong
key or dropping a join silently is a more common and more corporate-relevant
LLM-SQL failure mode than mis-flattening a nested field — a stronger,
more legible story for the Data Analyst / Data Engineer framing in
Section 9. These public tables have no declared FK constraints, so FK
relationships are inferred by naming convention in Phase 1 and must be
validated against actual data in Phase 3, not trusted as ground truth.

Backlog overlap check: none found. Phase 1 and Phase 2 are both complete
(tags `v0.1-dataset-profile`, `v0.2-agent-core`) — this line is now
historical context for why TheLook was chosen, not an open task.