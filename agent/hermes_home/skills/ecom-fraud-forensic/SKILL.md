---
name: ecom-fraud-forensic
description: Behavioral fraud detection over archived payment records. ACTIVATE ONLY when the task instruction text contains one of the literal words "fraud", "fraudulent", "Risk Ops", or "chargeback" AND asks you to identify specific payment records. DO NOT activate for: ordinary payment recovery (3DS / card-security), refunds, basket checkout, catalog lookups, customer verification, manager checks, store availability, count reports, or any task that does not literally include one of those four trigger words. If unsure, do NOT activate.
---

# ECOM fraud-forensic procedure

## Core principle: fraud is BEHAVIORAL, not labeled

The ECOM workspace **does NOT carry a `fraud=true` field** on payment records, has no `/docs/fraud.md`, no `chargeback` column, no "Risk Ops" report file. **This is expected**, not an obstacle.

**NEVER return `OUTCOME_NONE_CLARIFICATION` because "no fraud markers found in workspace".** That is misreading the task. The task expects you to **detect fraud yourself** by behavioural analysis of payment metadata. Always return `OUTCOME_OK` with the clusters you identify. Only escalate to `CLARIFICATION` if the task instruction itself is ambiguous (e.g. "find fraud" with no scope at all).

## Core principle: fraud is MULTI-CLUSTER, not single-cluster

A single confirmed fraud "incident" in ECOM data typically spans **multiple disjoint clusters** detected by **different patterns** OR by the same pattern on different days. Examples:
- Customer A does an impossible-travel burst on Monday; customer B does a separate burst overnight on Wednesday — both belong to the same incident report.
- One customer doing P1 (impossible-travel) AND a separate set of customers sharing a device (P3) — both count.
- "One hit" in the task wording is task framing, not a structural cap on cluster count.

The grader scores total EUR fraud recovered + per-record precision. Missing a secondary cluster costs as much as missing the largest one. **Always seek the union; let the grader collapse it.**

**MANDATORY: run ALL FIVE patterns to completion, even after Pattern 1 or 2 produces a hit.**

## What you have to work with

Payment records (`/proc/payments/pay_*.json` and archive TSV files) carry these fields you can correlate:
- `customer_id`
- `payment_method_id` (`pm_*`) — the tokenised card / wallet
- `device_id` (`dev_*`) — the device fingerprint
- `location` / `lat` / `lon` — geo coordinates of the transaction
- `store_id` — the physical store
- `created_at` / `timestamp` — when the payment posted
- `amount_cents`
- `status` (`paid`, `pending`, `failed`)
- `basket_archived` flag — distinguishes "archived" vs current
- `pm` + `dev` fingerprint pair

## CRITICAL: how to write a P1 query that actually works

Most "long-history" customers in the bench have **20+ archived payments scattered over many months**, with a **tight fraud burst inside that history**. If you `GROUP BY customer_id` and aggregate `MIN/MAX(created_at)` over their ENTIRE archive, the burst window (3-60 min) is diluted by the multi-month outer span and `HAVING span_min < 30` excludes everyone — including the burst clusters that are the actual fraud.

This is a real failure mode observed on this bench: customers with multi-month archive history have their bursts masked. The fix is **to bucket by day**, so a burst on a single date stays intact regardless of how spread out the rest of the customer's archive is.

**MANDATORY: every P1 query must include `date(created_at)` (or equivalent day-bucket) in the GROUP BY.** Never aggregate `MIN/MAX(created_at)` across days within a single customer.

## Behavioural fraud patterns — independent detectors

Run **every one** of these. Each is a standalone classifier producing zero or more candidate clusters. Collect them all; dedupe at the end.

### Pattern 1 — Impossible-travel velocity burst (DAY-BUCKETED, CITY-based)

Impossible travel is measured in **DISTINCT CITIES, not distinct stores.** Two stores in the SAME city minutes apart is ordinary errand shopping; two different *cities* minutes apart is physically impossible. A customer is a fraud cluster when, on one calendar day, they transact in:
- **≥3 distinct cities within ≤120 min** (classic rapid-fire / overnight multi-city burst), OR
- **≥2 distinct cities within ≤30 min** (a tight inter-city hop — already impossible; this is how each mule in a coordinated ring behaves, and a LONE one counts too).

Day-bucketing keeps a tight burst from being diluted by the customer's multi-month archive history.

**City key.** In the live SQL DB the city is the token embedded in `store_id` right after `store_` (so `store_graz_jakomini` and `store_graz_lend` are BOTH `graz` = one city). In an `/archive/*.tsv` export use the `store_city` column directly.

MANDATORY P1 SQL (live DB — extracts the city from `store_id`):

```sql
SELECT customer_id, date(created_at) AS day, COUNT(*) AS n,
       MIN(created_at) AS first_at, MAX(created_at) AS last_at,
       (julianday(MAX(created_at)) - julianday(MIN(created_at))) * 24 * 60 AS span_min,
       COUNT(DISTINCT substr(store_id, 7, instr(substr(store_id,7)||'_','_') - 1)) AS cities
FROM payments
WHERE basket_archived = 1
GROUP BY customer_id, date(created_at)
HAVING (cities >= 3 AND span_min <= 120)   -- multi-city burst
    OR (cities >= 2 AND span_min <= 30)     -- tight inter-city hop (lone or ring member)
ORDER BY span_min ASC;
```
(`substr(store_id,7,…)` strips the `store_` prefix and takes the leading city token. On the TSV, group on `store_city` instead.)

Why CITIES and these caps:
- **Cities, not stores:** counting stores over-flags a shopper who visits two branches in ONE city; counting cities does not. This is the single worst precision killer on the archive-export task — fixed by switching to cities.
- **`cities >= 2 AND span <= 30`:** two different cities within half an hour is impossible travel on its own. A lone customer's tight inter-city hop IS fraud — do not require a cohort.
- **The ≤30 / ≤120 caps protect precision:** two cities spread over 1-2 h (e.g. Vienna→Linz in 140 min) is a legitimate trip, NOT fraud — both tiers exclude it.
- **Single-city guard:** any number of same-day payments in ONE city (`cities = 1`) is a legitimate repeat buyer — never flagged.

**Coordinated same-day ring:** a `day` on which several different customers each light up the tight-hop tier is one coordinated mule ring (4-6 accounts hitting the same cities on the same date). The per-customer rule above already flags every member; just describe the cohort as one incident in your answer.

### Pattern 2 — Rapid-fire payments from one device

Same `device_id` produces N≥5 payments within K≤5 minutes, often with **alternating `pm_*` tokens** (card cycling to evade per-card limits). Geo may be identical (one location, one device, many cards = stolen-card testing).

```sql
-- P2: rapid-fire single-device burst
SELECT device_id, COUNT(*) AS n,
       (julianday(MAX(created_at)) - julianday(MIN(created_at))) * 24 * 60 AS span_min,
       COUNT(DISTINCT payment_method_id) AS pm_count,
       COUNT(DISTINCT customer_id) AS customers
FROM payments
WHERE basket_archived = 1
GROUP BY device_id
HAVING n >= 5 AND span_min <= 5;
```

### Pattern 3 — Cross-customer device sharing

A single `device_id` appears under multiple distinct `customer_id`s within a short window (≤24 h). Legitimate device-sharing inside a household is rare in ECOM data and usually doesn't span 4+ customers.

```sql
SELECT device_id, COUNT(DISTINCT customer_id) AS customers
FROM payments
WHERE basket_archived = 1
GROUP BY device_id
HAVING customers >= 4;
```

### Pattern 4 — Cross-customer payment-method sharing

A single `payment_method_id` (`pm_*`) appears under multiple `customer_id`s. Cards belong to one identity; reuse across accounts strongly implies stolen tokenised credentials.

### Pattern 5 — Identical fingerprint-pair across customers

Same `(pm_id, dev_id)` pair under different `customer_id` — strongest single signal of credential / device theft.

## Procedure

1. **Read the task wording carefully.** Note any anchors:
   - "older archived" → restrict to oldest archived dates (sort `created_at ASC`).
   - "archive export" / "/archive/*.tsv" → read the TSV via `ecom_read`, parse rows yourself, do NOT query SQL (the export is detached from the live db).
   - Specific date range → honor it strictly.
   - "one hit" / "the incident" → task framing, NOT a structural cap. Run all 5 patterns; report the union.

2. **Run every pattern.** For each of P1, P2–P5:
   - Execute the city-based P1 SQL (it catches both lone fraudsters and every member of a coordinated ring).
   - Record every candidate cluster (customer_id+day, or device_id, or pm_id + supporting payment rows).
   - Do NOT skip a pattern because an earlier one already produced a hit.
   - **For P1, if 0 rows: re-run once relaxing the tight tier to `cities >= 2 AND span_min <= 60`.** Bench-tagged fraud tasks almost always have at least one P1 hit.
   - **On an `/archive/*.tsv` export: apply the SAME P1 (city-based, using the `store_city` column) and P2–P5 logic to the parsed TSV rows in one pass** (e.g. group rows in a single `python3` script). Do NOT re-read the file repeatedly — one parse, then compute all patterns, to avoid timing out on large exports.

3. **Union & dedupe.** Once all patterns have run:
   - Take the union of all payment_ids flagged by ANY pattern.
   - Dedupe (same payment_id can be flagged by P1 and P3 — count it once).
   - This union set is your fraud-record answer.

4. **Compute the total amount** as the sum of `amount_cents` over the union set → convert to EUR.

5. **Format the answer per the task instructions.** Many fraud tasks specify "EUR %d.%02d" or "<YES> ..." prefixes — follow them exactly. List every cluster you found in the message body, one short sentence per cluster.

## Refs hygiene

`grounding_refs` for a fraud answer:
- **DO include:** every `pay_XXX.json` in your union set (use `ecom_read`, not `ecom_read_silent`). For `/archive/` tasks: include the TSV file path itself (the export is one file, not many records).
- **DO include:** `/docs/security.md` (fraud is a security-flavoured outcome).
- **DO NOT include:** baseline `pay_001.json` … `pay_080.json` you read silently to learn the schema. Always inspect them via `ecom_read_silent` if you only need the structure. **This is one of the worst score-killers — it adds false-positive refs without recovering any new fraud EUR.**
- **DO NOT include:** `/proc/payments/README.md` unless your answer literally quotes a convention from it.
- **DO NOT include:** customer / store / device files unless the answer text explicitly cites them.

## Worked example (FICTIONAL — not in your data)

The following walkthrough uses synthetic IDs (`cust_X_fictional`, `pay_FICTIONAL_*`, `dev_FICTIONAL_*`) to demonstrate **procedure shape**, NOT real data. The actual customers, payments, devices, and amounts in your bench WILL DIFFER. Do not copy the IDs or amounts; copy the *steps*.

**Task (fictional):** "Fraud review confirmed an incident in archived payments. Identify the fraudulent records. Do not modify files."

**Procedure walkthrough:**
1. Run P1 day-bucketed city-based (MANDATORY skeleton above) → 2 rows:
   - `cust_X_fictional, 2099-01-15, 8 payments, 4 min, 8 cities` (rapid-fire impossible travel — multi-city tier)
   - `cust_Y_fictional, 2099-02-22, 2 payments, 11 min, 2 cities` (tight inter-city hop — caught by the `cities>=2 AND span<=30` tier, even though it is a LONE 2-city hop)
2. P2 (rapid-fire device) → 1 row: `dev_FICTIONAL_001, 8 payments, 4 min` — already part of cluster A (cust_X). No new IDs.
3. P3 (cross-customer device) → 1 row: `dev_FICTIONAL_999 shared across 5 customers`. Fetch the payment rows → cluster C (6 payments under different customers, disjoint from A and B).
4. P4 (cross-customer pm) → 0 rows.
5. P5 (cross-customer pm+dev) → 0 rows.
6. **Union** = cluster A (8) ∪ cluster B (2) ∪ cluster C (6) = 16 unique payment_ids.
7. For each of the 16 payment IDs: `ecom_read("/proc/payments/<id>.json")` (tracked).
8. `ecom_read("/docs/security.md")` (tracked).
9. Answer:
   `<YES> Three disjoint fraud clusters: (A) cust_X — 8 rapid-fire payments across 8 cities in 4 min on 2099-01-15. (B) cust_Y — 2 payments across 2 cities in 11 min on 2099-02-22 (tight inter-city hop). (C) dev_FICTIONAL_999 — 6 payments across 5 customers sharing one device. Total EUR <sum>.`
10. Refs: every pay file in the union + `/docs/security.md`.

**Critical observation from the example:** cluster B (a LONE 2-city hop) would have been MISSED by a `stores>=3` / `cities>=3` floor — the `cities>=2 AND span<=30` tier is what surfaces single mule hops and coordinated-ring members. Cluster C is invisible to P1/P2 entirely — only P3 surfaces it. **This is why you must measure CITIES (not stores), keep the two-tier HAVING, AND run all 5 patterns.**

## Anti-patterns (each = score 0 or heavy partial credit loss)

- ❌ **`GROUP BY customer_id` without day-bucketing in P1.** A customer with a 22-payment archive over 66 days has a huge outer span, so any whole-archive aggregate hides a tight 3-min burst inside it. ALWAYS use `GROUP BY customer_id, date(created_at)`.
- ❌ **Counting `DISTINCT store_id` instead of DISTINCT CITY in P1.** Two branches in ONE city minutes apart is ordinary shopping; flagging it floods false positives (the worst precision killer on the archive-export task). Always reduce `store_id` to its city token (or use `store_city` on the TSV).
- ❌ **Requiring `cities >= 3` (or `stores >= 3`) as the only tier.** That silently drops coordinated mule rings, where each member does only a tight 2-city hop. Keep the `cities >= 2 AND span <= 30` tier.
- ❌ **Flagging a 2-city hop spread over more than ~30 min.** Two cities over 1-2 h (e.g. Vienna→Linz in 140 min) is a legitimate trip, not fraud. The `span <= 30` cap on the 2-city tier is mandatory.
- ❌ Stopping after the first pattern produces a hit. Multi-cluster incidents are the norm, not the exception. Run all 5 patterns to completion every time.
- ❌ "No P1 hit → conclude no fraud." If P1 returns 0 rows, re-run ONCE relaxing the tight tier to `cities >= 2 AND span_min <= 60`. Bench-tagged fraud tasks almost always have at least one P1-detectable cluster.
- ❌ "The task says 'one hit', so I'll only report one cluster." — Run all 5 patterns; report the union; let the grader collapse it.
- ❌ Citing `pay_001.json` … `pay_080.json` as refs because you sampled them silently to learn the schema. Those are PROBES, not findings. ALWAYS use `ecom_read_silent` for schema probes. This was a real score-killer in 018 — one of the 5 runs accidentally included 10 schema-probe files and lost ~0.08 in partial credit.
- ❌ "No fraud column found → CLARIFICATION." — see Core principle.
- ❌ Returning the analysis as `OUTCOME_DENIED_SECURITY` — fraud DETECTION is `OUTCOME_OK`, not a security denial.
- ❌ Copying IDs/amounts from the Worked example into your answer. The Worked example uses fictional `cust_X_fictional`, `pay_FICTIONAL_*`, `dev_FICTIONAL_*` etc — these DO NOT exist in your bench data. Always derive your own IDs and totals from real SQL/TSV results.
