---
name: competitor-analysis
description: |
  Searches live product listings on the target marketplace and extracts a complete
  competitive landscape including price range, top sellers, review counts, trend
  direction, seasonality signals, and high-volume keywords. Triggers when the
  Orchestrator Agent needs market intelligence for a specific product and marketplace
  combination, always after supplier research completes.

  Trigger phrases: "analyze competitors", "check competition", "search marketplace listings",
  "how competitive is", "competitor prices", "market analysis", "what are others selling for",
  "top sellers on", "product trend", "market saturation".

  Do NOT use for: supplier research, fee research, margin calculation, risk scoring,
  or general web research not tied to a specific product on a specific marketplace.
version: 1.0.0
allowed-tools: Read, Bash
---

# Competitor Analysis

## Purpose

This skill searches live marketplace listings on the seller's target platform and extracts
structured competitive intelligence. It tells the seller what they are walking into: how many
competitors exist, what price range they are competing in, how strong the top sellers are,
whether the market is growing or shrinking, whether the product is seasonal, and which
keywords drive rankings. This data feeds directly into the Margin Calculator (for realistic
selling price) and the Risk Assessor (for saturation and competition scores).

**Note:** In DropSmart's current implementation, this skill is invoked
directly by the Orchestrator (agents/orchestrator.py) as one fixed step
in a sequential pipeline, not via dynamic trigger-phrase matching. The
trigger phrases below document the skill's intended scope and are not
an active routing mechanism in this version.

## When to Use

- Supplier Research has already completed and results are in session context
- Orchestrator Agent needs market data for the pipeline to proceed to fee research
- Session context contains `product`, `marketplace`, and `region` fields
- User asks about pricing, competition, trends, or keyword data for a specific marketplace

## When NOT to Use

- Supplier research has not yet run (competitor analysis must follow supplier research)
- Task is to find suppliers (use supplier-research)
- Task is to find marketplace fees (use fee-structure-research)
- Task is to calculate margin (use margin-calculator)
- Marketplace is not one of: `daraz_pk`, `walmart_us`, `amazon_us`, `etsy_us`
- User is asking about general industry trends not tied to a specific marketplace listing search

## Step-by-Step Workflow

1. **Read session context** — extract `product`, `marketplace`, and `region` from the
   Orchestrator's session context dict.

2. **Search live marketplace listings** for the product on the target marketplace. Search must
   be specific to that marketplace's domain — not generic web results:
   - Daraz: search daraz.pk listings
   - Walmart: search walmart.com marketplace listings
   - Amazon: search amazon.com listings
   - Etsy: search etsy.com listings

3. **Extract total active listing count.** This is the primary Market Saturation input.
   Under 200 listings = low saturation. 200–1,000 = moderate. Over 1,000 = high.

4. **Extract price range** across top visible listings:
   - Minimum listed price
   - Maximum listed price
   - Median/most common price point (the "sweet spot" buyers cluster around)

5. **Extract top 3 competitor listings** with all of these fields:
   - Listing title
   - Current price
   - Average rating (out of 5)
   - Total review count
   - Seller name
   - Estimated monthly sales (if determinable from rank/review velocity signals)

6. **Check for marketplace-owned or high-authority competitor presence:**
   - Daraz: is a Daraz Mall seller in the top 5? (Mall sellers have visibility advantage)
   - Amazon: is Amazon's own brand or an Amazon Choice product in the top 5?
   - Walmart: is a Walmart private label or Walmart-fulfilled listing dominating?
   - Etsy: are "Star Seller" badges present among top results?
   If yes — flag this as a competitive barrier in output.

7. **Assess trend direction** from available signals (search volume trends, review recency,
   listing creation dates):
   - **Growing:** new listings appearing frequently, recent reviews accelerating
   - **Stable:** consistent review pace, steady pricing, no dramatic listing count changes
   - **Declining:** older listing dates, slowing review counts, price compression

8. **Identify seasonality signals:**
   - Are reviews and sales spiking at certain times of year?
   - Does the product category have known seasonal peaks (Q4 for gifts, summer for outdoor)?
   - Assign seasonality risk: Low / Medium / High

9. **Extract top 5 high-volume keywords** from titles and tags of top-ranking listings.
   These must be actual terms found in top listings — not invented. These keywords are passed
   to the Report Agent for use in the listing draft.

10. **Identify "sweet spot" price range** — the price band where the majority of
    competitive, well-reviewed listings sit. This becomes the recommended selling price input
    for the Margin Calculator.

11. **Return structured competitive landscape** with all fields populated.

## Output Format

```
COMPETITOR ANALYSIS RESULTS
Product: [product name]
Marketplace: [marketplace_id]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

MARKET OVERVIEW
Total Active Listings:    [number]
Saturation Level:         [Low / Moderate / High]
Price Range:              [currency] [min] – [max]
Sweet Spot Price:         [currency] [amount] (most competitive listings)
Trend Direction:          [Growing / Stable / Declining]
Seasonality Risk:         [Low / Medium / High] — [peak period if applicable]

──────────────────────────────────────────

TOP COMPETITORS

#1  [Listing Title]
    Price:          [currency] [amount]
    Rating:         [X.X / 5.0]
    Reviews:        [number]
    Seller:         [name]
    Monthly Sales:  [estimate or "not determinable"]

#2  [Listing Title]
    Price:          [currency] [amount]
    Rating:         [X.X / 5.0]
    Reviews:        [number]
    Seller:         [name]
    Monthly Sales:  [estimate or "not determinable"]

#3  [Listing Title]
    Price:          [currency] [amount]
    Rating:         [X.X / 5.0]
    Reviews:        [number]
    Seller:         [name]
    Monthly Sales:  [estimate or "not determinable"]

──────────────────────────────────────────

HIGH-AUTHORITY COMPETITOR WARNING
[Daraz Mall / Amazon Choice / Walmart Private Label / Etsy Star Seller present: Yes/No]
[If Yes: name and impact explanation]

──────────────────────────────────────────

TOP 5 HIGH-VOLUME KEYWORDS
1. [keyword]
2. [keyword]
3. [keyword]
4. [keyword]
5. [keyword]

──────────────────────────────────────────
RECOMMENDED SELLING PRICE FOR MARGIN CALC: [currency] [sweet spot amount]
```

## Important Rules

- **DO** search the target marketplace specifically — not general web results
- **DO** return at least 5 keywords — if fewer than 5 are found, note this as a data gap
- **DO** flag Daraz Mall / Amazon Choice / Walmart private label / Etsy Star Seller presence
  if any are in the top 5 results — this is a material competitive risk
- **DO** pass the sweet spot price to the Orchestrator for the Margin Calculator
- **DO** pass the keyword list to the Orchestrator for the Report Agent
- **DO NOT** return generic Google search results — searches must be marketplace-specific
- **DO NOT** invent or estimate keywords — only use terms extracted from actual top listings
- **DO NOT** skip the seasonality assessment — it feeds directly into Risk Assessor's
  Seasonality Risk score
- **DO NOT** run this skill if the marketplace is not in the supported list
  (`daraz_pk`, `walmart_us`, `amazon_us`, `etsy_us`)
