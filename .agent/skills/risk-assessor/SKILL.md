---
name: risk-assessor
description: |
  Synthesizes outputs from all four preceding agents (supplier, competitor, fee, margin)
  and scores the product opportunity across six risk dimensions. Returns a scorecard with
  per-dimension scores and reasoning, an overall risk level (LOW / MEDIUM / HIGH), and the
  top three risks the seller should address. Triggers after margin calculation completes
  and immediately before the HITL checkpoint.

  Trigger phrases: "assess risk", "risk score", "how risky is this", "risk assessment",
  "is this product safe to sell", "risk level", "should I sell this", "market saturation score",
  "risk scorecard", "evaluate this product".

  Do NOT use for: research tasks, fee searching, margin calculation, or generating the
  final report. This skill scores and summarizes — it does not gather new data.
version: 1.0.0
allowed-tools: Read, Bash
---

# Risk Assessor

## Purpose

This skill synthesizes data from all preceding agents and produces a structured risk scorecard
for the product opportunity. It scores six dimensions on a 1–10 scale (where 10 is highest
risk), provides one-line reasoning for each score tied to actual data, and issues an overall
risk level of LOW, MEDIUM, or HIGH. Every score must be grounded in real data from earlier
agents — no assumptions or gut-feel scoring is permitted. The scorecard is the final input
the seller sees before the HITL checkpoint, where they decide whether to approve the report.

**Note:** In DropSmart's current implementation, this skill is invoked
directly by the Orchestrator (agents/orchestrator.py) as one fixed step
in a sequential pipeline, not via dynamic trigger-phrase matching. The
trigger phrases below document the skill's intended scope and are not
an active routing mechanism in this version.

## When to Use

- Margin Calculation has completed and `margin_pct` is in session context
- All four preceding agent outputs are available: supplier research, competitor analysis,
  fee research, and margin calculation
- Pipeline is at Step 5 (Risk Assessor always runs immediately before HITL checkpoint)
- User asks for a risk assessment or overall verdict on a product opportunity

## When NOT to Use

- Any preceding agent has not yet completed — all four prior outputs are required
- Task requires searching for new external data (use appropriate research skill instead)
- Task is to generate the final report (use report-generator, only after HITL approval)
- HITL checkpoint has already passed — risk scoring belongs before approval, not after

## Step-by-Step Workflow

1. **Read all required agent outputs from session context:**
   - From Supplier Research: supplier count, reliability signals, business model compatibility
   - From Competitor Analysis: total listing count, top competitor review counts, trend
     direction, seasonality signals
   - From Fee Research: any UNVERIFIED or MISSING fee warnings
   - From Margin Calculator: margin percentage, break-even price, LOW MARGIN flag if present

2. **Score each of the six dimensions on a scale of 1–10, where 10 = highest risk:**

   **Dimension 1 — Market Saturation**
   Based on: total active listing count from Competitor Analysis
   - 1–3: under 100 listings (low saturation, good entry opportunity)
   - 4–6: 100–500 listings (moderate, enterable with differentiation)
   - 7–8: 500–1,000 listings (high, difficult without strong USP or price advantage)
   - 9–10: over 1,000 listings (very high, near-impossible for new seller without niche angle)
   Hard rule: over 1,000 listings always scores 7 or above.

   **Dimension 2 — Margin Adequacy**
   Based on: margin percentage from Margin Calculator
   - 1–3: margin above 30% (strong buffer against price pressure)
   - 4–6: margin 20–30% (acceptable, some exposure to fee increases)
   - 7–8: margin 15–20% (thin, vulnerable to any fee or cost change)
   - 9–10: margin below 15% (dangerous, one fee change eliminates profit)
   Hard rule: margin below 15% always scores 8 or above.

   **Dimension 3 — Supplier Reliability**
   Based on: supplier rating, review count, years active, business model compatibility from
   Supplier Research output
   - 1–3: top-rated supplier with 4.8+ rating, 1,000+ transactions, 5+ years active
   - 4–6: good supplier with 4.5+ rating, moderate transaction history
   - 7–8: limited signals — few reviews, new supplier, or unconfirmed fields
   - 9–10: no source URL, "not confirmed" fields, or compatibility mismatch

   **Dimension 4 — Competition Level**
   Based on: top competitor review counts and marketplace authority signals from Competitor Analysis
   - 1–3: top competitors under 200 reviews, no marketplace-owned brand in top 5
   - 4–6: competitors with 200–1,000 reviews, moderate authority presence
   - 7–8: competitors with 1,000+ reviews, or Daraz Mall / Amazon Choice in top 5
   - 9–10: dominant competitors with 5,000+ reviews and marketplace-owned brand present

   **Dimension 5 — Trend Direction**
   Based on: trend direction from Competitor Analysis
   - 1–3: Growing trend (clear demand growth signals)
   - 4–6: Stable trend (consistent demand, no growth or decline)
   - 7–8: Unclear trend (mixed signals, inconclusive data)
   - 9–10: Declining trend (falling demand, price compression, slowing reviews)

   **Dimension 6 — Seasonality Risk**
   Based on: seasonality signals from Competitor Analysis
   - 1–3: Low seasonality (product sells year-round consistently)
   - 4–6: Medium seasonality (1–2 clear peak periods, manageable inventory)
   - 7–8: High seasonality (narrow peak window, risk of dead stock off-season)
   - 9–10: Extreme seasonality (single-month peak, e.g. Eid gifts, Christmas only)

3. **For each dimension, write one line of reasoning** that references the specific data
   point driving that score. Do not write generic statements — cite actual numbers or facts:
   - Good: *"Score 8 — 1,247 active listings found on amazon.com, above 1,000 threshold"*
   - Bad: *"Score 8 — market is very saturated"* (too vague — rejected)

4. **Calculate overall risk level** using this logic:
   - **LOW:** four or more dimensions score 4 or below, and no single dimension scores 8+
   - **HIGH:** two or more dimensions score 7 or above, OR any single dimension scores 9–10
   - **MEDIUM:** all remaining combinations

5. **Identify the top 3 specific risks** the seller must address. Select the three highest-scoring
   dimensions and translate each into an actionable risk statement for the seller:
   - Not just *"high saturation"* — but *"1,247 competitors on Amazon means you need 200+
     reviews and a price within $2 of the median before you'll rank organically."*

6. **Return the complete risk scorecard** to session context for the HITL checkpoint display.

## Output Format

```
RISK ASSESSMENT SCORECARD
Product: [product name]
Marketplace: [marketplace_id]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DIMENSION SCORES  (1 = lowest risk, 10 = highest risk)

Market Saturation:      [score] / 10
  "[one-line reasoning with specific data]"

Margin Adequacy:        [score] / 10
  "[one-line reasoning with specific data]"

Supplier Reliability:   [score] / 10
  "[one-line reasoning with specific data]"

Competition Level:      [score] / 10
  "[one-line reasoning with specific data]"

Trend Direction:        [score] / 10
  "[one-line reasoning with specific data]"

Seasonality Risk:       [score] / 10
  "[one-line reasoning with specific data]"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

OVERALL RISK LEVEL:  [ LOW / MEDIUM / HIGH ]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TOP 3 RISKS TO ADDRESS

1. [Specific, actionable risk statement with data]

2. [Specific, actionable risk statement with data]

3. [Specific, actionable risk statement with data]
```

## Important Rules

- **DO** ground every score in actual data from previous agent outputs — cite specific numbers
- **DO** apply the hard scoring rules: over 1,000 listings → Saturation 7+; margin below
  15% → Margin Adequacy 8+
- **DO** write actionable risk statements in the top 3 — not vague summaries
- **DO** carry forward any UNVERIFIED or MISSING fee warnings from Fee Research into the
  scorecard warnings section — these are risk factors
- **DO NOT** score any dimension based on assumptions if data is available from agents
- **DO NOT** run this skill if margin calculation has not completed — margin percentage is
  required for Dimension 2
- **DO NOT** generate the final report here — this skill scores only; report generation
  requires HITL approval and is a separate skill
- **DO NOT** alter scores based on what verdict would be "nicer" for the seller — scores
  must reflect the data honestly
