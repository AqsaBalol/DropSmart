---
name: fee-structure-research
description: |
  Dynamically searches and retrieves the complete, current fee structure for a specific
  marketplace and business model combination. Uses multi-source verification, tiered fee
  lookup, date freshness checking, and missing fee detection per the five risk mitigation
  rules in specs/dropsmart_spec.md Section 7. Triggers after competitor analysis completes
  and before the Margin Calculator runs.

  Trigger phrases: "find fees", "search fees", "marketplace fees", "what are the fees",
  "fee structure", "commission rate", "how much does [marketplace] charge", "FBA fees",
  "Daraz commission", "Walmart referral fee", "Etsy fees".

  Do NOT use for: supplier research, competitor analysis, margin calculation, risk scoring,
  or any task not directly related to finding platform fee structures.
version: 1.0.0
allowed-tools: Read, Bash
---

# Fee Structure Research

## Purpose

This skill fetches the complete, current fee structure for the seller's target marketplace
and business model. It never hardcodes fee values — every figure is searched fresh on every
run because marketplace fees change without notice and vary by product category. The skill
enforces five risk mitigation rules: multi-source verification, source URL requirement, date
freshness checking, no flat rate assumption for tiered fees, and missing fee detection against
the platform YAML config. The output feeds directly into the Margin Calculator Agent, which
requires a complete, named, sourced fee breakdown before it can produce its line-by-line
deduction table.

## When to Use

- Competitor Analysis has already completed and selling price is known
- Pipeline is at Step 3 (Fee Research always follows Competitor Analysis)
- Session context contains `marketplace`, `business_model`, `product_category`, and
  optionally `seller_province` (for Daraz)
- User asks about platform fees, commission rates, or what a marketplace charges sellers

## When NOT to Use

- Competitor analysis has not yet run (selling price is needed to calculate tiered fees correctly)
- Task is to find suppliers (use supplier-research)
- Task is to analyze competitors (use competitor-analysis)
- Task is to calculate margin from already-known fees (use margin-calculator)
- Fees were already fetched in this session and are less than 24 hours old (reuse from context)

## Step-by-Step Workflow

1. **Read session context** — extract `marketplace`, `business_model`, `product_category`,
   `selling_price` (from competitor analysis sweet spot), and `seller_province` (Daraz only)
   from the Orchestrator's session context dict.

2. **Load platform config YAML** — read the `fee_categories` list from the relevant file in
   `platform_configs/`. This list defines every fee that MUST appear in the final output.
   Missing any fee from this list is a spec violation.

3. **For each fee category in the YAML list, run the research loop:**

   a. **Multi-source verification (Rule 1):** Run at least 3 different search queries using
      the `fee_search_queries` templates from the platform YAML. Substitute `{product_category}`
      with the actual category at runtime. Record which query returned which result.

   b. **Source URL capture (Rule 2):** Extract the source URL from every result. If a fee
      value is found but no URL can be confirmed, mark the fee as `UNVERIFIED` and attach
      the warning: *"UNVERIFIED FEE: [Fee Name] could not be confirmed with a source URL.
      Verify manually before listing."* This warning must appear at the HITL checkpoint.

   c. **Date freshness check (Rule 3):** Note the publication or update date of each source.
      If no results dated within the last 12 months are found, attach the warning:
      *"POTENTIALLY OUTDATED: Fee data for [Fee Name] could not be confirmed within the last
      12 months. Verify manually before listing."*

   d. **Conflict resolution:** If multiple sources return different values for the same fee,
      use the most conservative (highest) value. Log the conflict and which value was chosen.

4. **Apply tiered fee logic where required (Rule 4 — no flat rate assumption):**

   - **Daraz Handling Fee:** Look up the selling price from session context and apply the
     correct tier from the platform YAML:
     - PKR 0–500 → PKR 10
     - PKR 501–1,000 → PKR 15
     - PKR 1,001–2,000 → PKR 20
     - PKR 2,001+ → PKR 60
     Never use a single flat amount. Verify current tiers from search results.
   - **Amazon FBA Fulfillment Fee:** Apply size-tier logic based on product dimensions —
     do not assume a single rate.
   - **Walmart Referral Fee:** Apply category-specific rate — do not assume 15%.
   - **Etsy Offsite Ads Fee:** Apply 15% only if seller is below $10,000 annual sales, and
     note it can be opted out. Apply 12% mandatory flag if seller is above threshold.

5. **Apply province-dependent VAT for Daraz (all three VAT fees):**
   - If `seller_province` is `punjab` in session context → use 16%
   - If any other province → use 15%
   - If province not provided → default to 15% and attach warning:
     *"VAT rate defaulted to 15%. Punjab sellers should use 16% — recalculate if applicable."*

6. **Apply business model filters:**
   - FBD-specific fees (Daraz FBD Storage Fee, FBD Fulfillment Fee) — only include if
     `business_model` is `fbm`
   - WFS fees (Walmart) — only include if `business_model` is `fbm`
   - FBA fees (Amazon) — only include if `business_model` is `fbm`
   - Skip these fees entirely for `dropshipping` and `fbs` models

7. **Missing fee detection (Rule 5):**
   - After completing the research loop, compare results against the `fee_categories` list
     from the YAML
   - If any fee category is missing from results:
     a. Flag it explicitly by name: *"MISSING FEE: [Fee Name] not found in search results"*
     b. Retry with a different search query from the `fee_search_queries` list
     c. If still missing after retry: pause and notify seller before continuing:
        *"INCOMPLETE FEE DATA: [Fee Name] could not be found for [Marketplace]. The margin
        calculation may be understated. Confirm this fee manually before approving."*

8. **Assemble final fee breakdown** — one row per fee with name, type, value, applies-to
   basis, source URL, and any warnings.

9. **Return structured fee breakdown** to session context for Margin Calculator Agent.

## Output Format

```
FEE STRUCTURE RESEARCH RESULTS
Marketplace: [marketplace_id]
Business Model: [dropshipping / fbs / fbm]
Product Category: [category]
Fetched: [ISO 8601 timestamp]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

FEE BREAKDOWN

[Fee Name]
  Type:       [percentage / flat / tiered]
  Value:      [amount or %]
  Applies To: [selling_price / commission_amount / per_order / etc.]
  Source:     [URL]
  Note:       [any warnings: UNVERIFIED / OUTDATED / conflict resolved / tier selected]

[Fee Name]
  Type:       [...]
  Value:      [...]
  Applies To: [...]
  Source:     [URL]
  Note:       [...]

[repeat for every fee in platform YAML fee_categories]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

WARNINGS
[List any UNVERIFIED, OUTDATED, MISSING, or CONFLICT warnings here]
[If no warnings: "All fees verified with source URLs dated within 12 months."]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STATUS: [COMPLETE — all [N] fees found] OR [INCOMPLETE — [N] fees missing, see warnings]
```

## Important Rules

- **DO** run at least 3 search queries per fee — single-source results are not accepted
- **DO** include a source URL for every fee — mark as UNVERIFIED if none found
- **DO** check date freshness and warn if no results within 12 months
- **DO** use the most conservative (highest) value when sources conflict
- **DO** apply tiered logic for Daraz handling fee — never a flat amount
- **DO** apply province-dependent VAT for Daraz — ask for province or default to 15% with warning
- **DO** filter FBD/WFS/FBA fees by business model — only include when `fbm`
- **DO** check all YAML `fee_categories` are present in output — retry missing ones
- **DO NOT** hardcode any fee value anywhere
- **DO NOT** assume a flat rate for any fee known to be tiered or category-variable
- **DO NOT** pass incomplete fee data to the Margin Calculator — flag missing fees first
- **DO NOT** suppress warnings — every UNVERIFIED, OUTDATED, or MISSING flag must be
  visible to the seller at the HITL checkpoint
