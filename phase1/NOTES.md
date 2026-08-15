# Phase 1 Notes

Schema profiler for TheLook eCommerce, four iterations to get right.

1. FK inference initially assumed every table's key column is named `id`.
   `orders` breaks that convention (`order_id`), which silently pointed
   `order_items.order_id` at a nonexistent column. Fixed by looking up
   each candidate table's actual columns instead of hardcoding `.id`.

2. Cardinality sampling used `TABLESAMPLE SYSTEM` and assumed the
   requested fraction (10%) was the fraction actually achieved. Block-
   level sampling on smaller tables silently grabbed 95-100% of rows,
   producing extrapolated cardinality estimates up to 10x higher than
   the table's own row count -- a mathematically impossible result.
   Fixed by measuring the actual achieved fraction via COUNT(*) instead
   of trusting the request, and hard-clamping at row_count as a backstop.

3. Even with a correct fraction, block-level (not row-level) sampling can
   see a non-representative slice of a column's value domain. Caught via
   a cross-table check: `events.user_id`'s estimated cardinality exceeded
   the actual number of rows in `users`, which is impossible for a valid
   foreign key. Clamped against the referenced table's real cardinality.

4. The clamp in (3) fixed the number but not the underlying trust problem
   -- a downstream consumer reading only estimated_full_cardinality had
   no way to know it had been capped. Added a structured
   cardinality_reliable boolean so Phase 2 can gate on this
   programmatically instead of parsing free-text notes. As of this run,
   events.user_id is the only column in the dataset flagged unreliable;
   its true cardinality remains unknown, only bounded.
