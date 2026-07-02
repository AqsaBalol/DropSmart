---
name: supplier-research
description: |
  Finds and ranks suppliers for a product based on the seller's chosen business model
  and target marketplace. Triggers when the Orchestrator Agent needs supplier options
  before the pipeline can proceed to competitor analysis.

  Trigger phrases: "find suppliers", "search suppliers", "who supplies", "where to source",
  "find wholesaler", "supplier research", "find dropshipping supplier", "find bulk supplier".

  Do NOT use for: competitor analysis, fee research, margin calculation, risk assessment,
  or any task not directly related to finding product suppliers.
version: 1.0.0
allowed-tools: Read, Bash
---

# Supplier Research

## Purpose

This skill finds real supplier options for a given product by searching across multiple
supplier directories and databases. It adapts its search strategy entirely based on the
seller's business model — dropshipping requires completely different supplier criteria than
FBS bulk purchasing or FBM warehouse shipping. The skill returns a ranked list of 3 suppliers
with full details and a clear recommended pick, giving the Margin Calculator Agent the
supplier cost data it needs to run calculations.

**Note:** In DropSmart's current implementation, this skill is invoked
directly by the Orchestrator (agents/orchestrator.py) as one fixed step
in a sequential pipeline, not via dynamic trigger-phrase matching. The
trigger phrases below document the skill's intended scope and are not
an active routing mechanism in this version.

## When to Use

- Orchestrator Agent has collected product name and business model from seller
- Pipeline is at Step 1 (Supplier Research is always the first specialist agent to run)
- Session context contains `product`, `business_model`, and `marketplace` fields
- User asks directly about where to source a product or who supplies it

## When NOT to Use

- Business model or product has not yet been collected by Orchestrator
- Task is to find competitor prices on the marketplace (use competitor-analysis)
- Task is to find platform fees (use fee-structure-research)
- Task involves calculating margin or profit (use margin-calculator)
- Supplier has already been researched in this session and context is still valid

## Step-by-Step Workflow

1. **Read session context** — extract `product`, `business_model`, and `region` from the
   Orchestrator's session context dict before running any search.

2. **Select search strategy based on business model:**
   - **Dropshipping:** prioritize suppliers offering direct-to-customer shipping, no MOQ
     required, blind/white-label shipping capability, fast international delivery (under
     15 days), real-time inventory feeds or stock confirmation.
   - **FBS (Fulfilled by Seller):** prioritize suppliers with stated MOQ and bulk pricing
     tiers, reliable lead times (under 30 days), quality inspection options, stable
     long-term stock availability.
   - **FBM (Fulfilled by Marketplace / FBD / WFS / FBA):** prioritize suppliers able to
     ship bulk inventory to marketplace warehouse addresses, FNSKU/barcode labeling
     compliance, bulk poly-bag or box packaging options, and competitive per-unit cost
     at warehouse quantities (typically 100–500 units minimum).

3. **Run at least 3 different searches across different supplier sources.** Do not rely on
   a single search result. Sources to include based on region:
   - Pakistan (PK): Alibaba, AliExpress, CJDropshipping, local wholesale markets
   - USA (US): Alibaba, AliExpress, CJDropshipping, US domestic wholesalers, Global Sources

4. **For each supplier found, extract all of the following fields.** If a field cannot be
   found, mark it as `"not confirmed"` — do not estimate or fabricate:
   - Supplier name
   - Website / source URL (required — see Rules)
   - Estimated unit cost range (min and max)
   - MOQ (minimum order quantity)
   - Shipping method and estimated transit time in days
   - Dropship compatibility (yes/no with explanation)
   - Reliability signals: rating score, number of reviews/transactions, years in operation,
     any certifications or trade assurance status

5. **Assess business model compatibility** for each supplier:
   - Dropshipping: confirm direct-to-customer shipping is explicitly offered, not just stated
   - FBS: confirm MOQ is reasonable for a first bulk order (typically under 100 units)
   - FBM: confirm supplier can prepare items to marketplace labeling standards

6. **Rank suppliers** by two criteria in order of priority:
   - Primary: business model compatibility (incompatible suppliers are excluded entirely)
   - Secondary: reliability signals (higher rating, more reviews, more years active = higher rank)

7. **Select the recommended supplier** — the top-ranked after compatibility and reliability
   scoring. Mark clearly as `RECOMMENDED` in output.

8. **Return structured output** with all 3 suppliers and the recommended one flagged.

## Output Format

```
SUPPLIER RESEARCH RESULTS
Product: [product name]
Business Model: [dropshipping / fbs / fbm]
Marketplace: [marketplace_id]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

⭐ RECOMMENDED SUPPLIER
Name:              [Supplier Name]
Website:           [URL]
Unit Cost Range:   [currency] [min] – [max]
MOQ:               [quantity] units
Shipping Time:     [X–Y days] via [method]
Dropship Ready:    [Yes / No]
Reliability:       [rating] | [N] transactions | [N] years active
Compatibility:     [explanation of why this supplier fits the business model]

──────────────────────────────────────────

SUPPLIER 2
Name:              [Supplier Name]
Website:           [URL]
Unit Cost Range:   [currency] [min] – [max]
MOQ:               [quantity] units
Shipping Time:     [X–Y days] via [method]
Dropship Ready:    [Yes / No]
Reliability:       [rating] | [N] transactions | [N] years active
Compatibility:     [explanation]

──────────────────────────────────────────

SUPPLIER 3
Name:              [Supplier Name]
Website:           [URL]
Unit Cost Range:   [currency] [min] – [max]
MOQ:               [quantity] units
Shipping Time:     [X–Y days] via [method]
Dropship Ready:    [Yes / No]
Reliability:       [rating] | [N] transactions | [N] years active
Compatibility:     [explanation]

──────────────────────────────────────────
RECOMMENDED FOR MARGIN CALC: Use [Supplier Name] unit cost [amount] as baseline.
```

## Important Rules

- **DO** adapt search queries entirely to the business model — one search strategy does not
  fit all three models
- **DO** search at least 3 different sources before ranking
- **DO** require a source URL for every supplier — if no URL is available, the supplier
  cannot be included in the output
- **DO** mark incompatible suppliers as excluded with an explanation rather than silently
  dropping them
- **DO** pass the recommended supplier's unit cost range to the Orchestrator for the
  Margin Calculator Agent
- **DO NOT** recommend a dropshipping supplier that only offers bulk shipping to a warehouse
- **DO NOT** recommend an FBS supplier that cannot confirm MOQ
- **DO NOT** fabricate or estimate fields that could not be confirmed — mark as
  `"not confirmed"` and note the gap
- **DO NOT** run this skill more than once per session unless the product or business model
  changes — use cached results from session context
