---
name: ecom-catalogue-reporting
description: >
  Count products by kind for catalogue reporting, following dated policy docs.
  Handles: "How many {kind} products should I report today?" with per-date
  policy updates in /docs/policy-updates/.
---

## Trigger
User asks "How many {product_kind} products should I report today?" or similar
catalogue-count reporting questions.

## Procedure

1. Note the current date from `/bin/date` or `ecom_context()`.
2. Scan `/docs/catalogue-addenda/` (primary) and `/docs/policy-updates/` for a
   dated doc matching today's date AND the mentioned product kind. Read it
   tracked via `ecom_read`.
3. The policy doc specifies:
   - `product_kind_id` to query
   - Store scope (city, open-status filter)
   - Inventory filter (`available_today_quantity > 0`)
   - Counting rule (distinct SKUs once across stores)
4. Query the SQL schema: `SELECT name, sql FROM sqlite_schema WHERE sql IS NOT NULL`.
5. Find stores matching the city: `SELECT store_id FROM stores WHERE city = '...' AND is_open = 1`.
6. Count distinct SKUs: `SELECT COUNT(DISTINCT pv.product_sku) FROM product_variants pv JOIN store_inventory si ON pv.product_sku = si.product_sku WHERE pv.product_kind_id = '...' AND si.store_id IN (...) AND si.available_today_quantity > 0`.
7. `ecom_read` the store JSON (tracked) — store questions require store refs.
8. Answer in the requested format.

## Pitfalls
- Must use the `product_kind_id` from the policy doc, not guess from the name string.
- Always check `is_open` on stores.
- Use `COUNT(DISTINCT ...)` to deduplicate across stores.
- Dated policy docs are per-date; a different day may have a different doc or none.
