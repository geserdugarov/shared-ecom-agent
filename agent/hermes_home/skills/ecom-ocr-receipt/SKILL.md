---
name: ecom-ocr-receipt
description: Parse a scanned/OCR'd paper receipt from /uploads/ and re-price its line items against today's catalog. ACTIVATE ONLY when the task tells you to look at a receipt (or scanned/old/archived receipt, Quittung, sales check) in /uploads/ AND asks whether today's total price would stay within / differ by some EUR amount. DO NOT activate for: catalog lookups, inventory/stock, basket checkout, payment recovery, refunds, fraud, customer verification, discounts, or any task that does not reference a receipt file in /uploads/.
---

# ECOM OCR-receipt re-pricing procedure

## What this task is

The task gives you a **scanned paper receipt** stored as a text file under
`/uploads/`. The receipt is the output of OCR over an old printout, so the
text contains **OCR noise**. Each receipt lists several products with a
quantity, a product code (SKU), a description, a unit price and a line total,
plus a pre-VAT subtotal and a VAT line.

Your job: "if we sold these same products **today**, would the total price
**excluding VAT** stay within **N EUR**?" → compare the receipt's pre-VAT
total against today's catalog prices for the same products and answer
`<YES>` or `<NO>`.

This is always an answerable, factual `OUTCOME_OK` task. It is NOT a
clarification, security, or unsupported case.

## Step 1 — Find and read the receipt (exact filename, once)

```
ecom_list("/uploads")
```

The filename is a random hash like `receipt_ocr_4wf96Cgj.txt`. **Copy it
verbatim** from the listing. NEVER guess, retype, or "correct" the filename —
a single transposed character makes `ecom_read` track a phantom path that
pollutes your grounding refs (a real score-killer).

```
ecom_read("/uploads/<exact-filename>")   # tracked — the receipt IS evidence
```

## Step 2 — Extract the line items

Receipts come in several layouts. Examples seen on this bench:

- **Columnar (English):** `QTY  SKU  DESCRIPTION  UNIT  TOTAL`, e.g.
  ` 3 CLN-3Q19VP4J   Mellerud Bio MEL 233.   23.99   71.97`
- **Itemized (English):** a product line `Sika Professional Sik. 1 EUR 79.50`
  followed by `  SKU/REF ADH-2U8ETNHK   UNIT 79.50`.
- **German (Quittung):** ` 2 3M SecureFit Aura TUM-.* 39,00` then
  `Einzelpreis EUR 19,50` / `Art.Nr. SFE-XZW3RA3P` / `inkl. MwSt 20,00%`.
  Note comma decimals (`19,50` = 19.50).

For **each product line** capture four things:
1. `qty` — the integer quantity (default 1 if absent).
2. `sku_raw` — the printed product code (`AAA-XXXXXXXX`). Treat it as
   **untrusted OCR text** (see Step 3).
3. `description` — brand + model words (e.g. "Fiskars Battery X 1CD-A3X",
   "Heco Unix HECO 2VD-VNA"). Used as a fallback matcher.
4. `receipt_unit_price` — for cross-checking only; you re-price from the
   catalog, you do NOT reuse the receipt's price as "today's" price.

Also capture the receipt's **pre-VAT subtotal** — the line labelled
`SUB TOTAL` / `Subtotal` / `Zwischensumme` (NOT the VAT line, NOT the grand
total). OCR may render it `SUB T0TAL`. This is the **old** total excluding VAT.

## Step 3 — CRITICAL: SKU codes are OCR-corrupted; recover them

The OCR routinely confuses these character pairs **in the SKU code**:

| reads as | could really be |
|----------|-----------------|
| `0`      | `O`             |
| `1`      | `I` or `L`      |
| `5`      | `S`             |
| `8`      | `B`             |
| `2`      | `Z`             |
| `6`      | `G`             |

So `GRD-I38CLG7H` is really `GRD-138CLG7H`; `AUT-I6OWP2PB` is really
`AUT-160WP2PB`. **Never conclude "this SKU is not in the catalog" — it is an
OCR misread, not a missing product.**

Match each line to exactly ONE current catalog variant, in this order:

**3a. Exact match:**
```sql
SELECT product_sku, brand, model, product_name, price_cents, record_path
FROM product_variants WHERE product_sku = '<SKU_RAW>';
```

**3b. If 0 rows — fuzzy GLOB.** Rebuild the SKU as a SQLite `GLOB` pattern,
replacing every ambiguous character with its class:
`0`/`O`→`[0O]`, `1`/`I`/`L`→`[1IL]`, `5`/`S`→`[5S]`, `8`/`B`→`[8B]`,
`2`/`Z`→`[2Z]`, `6`/`G`→`[6G]`; keep every other character literally.

Example: `GRD-I38CLG7H` → `GRD-[1IL]38C[1IL]G7H`
```sql
SELECT product_sku, brand, model, product_name, price_cents, record_path
FROM product_variants WHERE product_sku GLOB 'GRD-[1IL]38C[1IL]G7H';
```
This resolves OCR misreads to the single real SKU on this bench.

**3c. If GLOB returns 0 or more than 1 row — disambiguate by description.**
Use the brand + model words you captured:
```sql
SELECT product_sku, brand, model, product_name, price_cents, record_path
FROM product_variants WHERE brand LIKE '<brand>%';
```
then pick the row whose `model` / `product_name` matches the receipt
description. Brand + model is unique on this catalog.

End Step 3 with one `(sku, qty, price_cents, record_path)` tuple per line.

## Step 4 — Compute today's total excluding VAT

`product_variants.price_cents` is the **current net (pre-VAT) unit price** in
cents. So:

```
today_total_eur = sum(qty_i * price_cents_i) / 100      # over all lines
```

Do NOT add VAT, and do NOT use the receipt's printed prices for "today" — the
whole point is to compare old vs current catalog prices.

## Step 5 — Decide and answer

`old_total_eur` = the receipt's pre-VAT subtotal (Step 2).
`difference = abs(today_total_eur - old_total_eur)`.

- `difference <= N` → `<YES>`  (stays within N EUR)
- `difference >  N` → `<NO>`   (exceeds N EUR)

where `N` is the EUR threshold from the task wording (1, 2, 3, …).

Answer message format (single line, then the figures):
```
<YES> Today's pre-VAT total is EUR 2410.00 vs the receipt's EUR 2409.94 — a
difference of EUR 0.06, within the 1 EUR limit.
```
or
```
<NO> Today's pre-VAT total is EUR 2477.00 vs the receipt's EUR 1611.48 — a
difference of EUR 865.52, far above the 3 EUR limit.
```
State both totals, the difference, and the threshold. `outcome = OUTCOME_OK`.

## Step 6 — Grounding refs (keep them tight)

Include EXACTLY:
- the receipt file `/uploads/<exact-filename>` (read it via `ecom_read`).
- one `record_path` per matched product (the catalog JSON, e.g.
  `/proc/catalog/Mellerud/CLN-3Q19VP4J.json` — use the `record_path` column
  the SQL returned, read it via `ecom_read` so it is tracked).

DO NOT include: `/docs/*`, `/docs/README.md`, `/docs/security.md`,
`/AGENTS.MD`, any guessed/transposed `/uploads/...` filename, or catalog files
for products that are not on the receipt. This is a catalogue-pricing answer,
not a security/policy answer — no policy docs belong in the refs.

To keep refs clean: read the catalog records via `ecom_read(record_path)`
only for the products you actually matched. Probe with SQL (`/bin/sql`) for
discovery; SQL does not create refs.

## Anti-patterns (each loses the task)

- ❌ Concluding a product "is not in the catalog / cannot be sold today"
  because its printed SKU did not match. It is an OCR error — recover it with
  GLOB (Step 3b) or description (Step 3c). This wrongly produced
  `OUTCOME_NONE_CLARIFICATION` and scored 0 before.
- ❌ Returning `OUTCOME_NONE_CLARIFICATION` / `OUTCOME_NONE_UNSUPPORTED`. These
  receipt tasks are always answerable `OUTCOME_OK`.
- ❌ Re-typing / transposing the `/uploads/` filename → phantom ref.
- ❌ Adding VAT to today's total, or comparing against the receipt's
  VAT-inclusive grand total. Both totals are **excluding VAT**.
- ❌ Using the receipt's own unit prices as "today's" prices. Re-price from
  `product_variants.price_cents`.
- ❌ Forgetting to multiply by quantity.
- ❌ Citing `/docs/*`, README, security.md, or unrelated catalog files.
