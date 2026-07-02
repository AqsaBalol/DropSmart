---
name: report-generator
description: |
  Generates the final DropSmart intelligence report — verdict, reasoning, recommended
  strategy, and marketplace-optimized listing draft — but ONLY after explicit human
  approval at the HITL checkpoint. This skill must check for hitl_approved: true in
  session context as its very first action. If the flag is absent or false, it stops
  immediately and returns an error. It never fires automatically.

  Trigger phrases: "generate report", "final report", "create listing", "write listing",
  "Go or No-Go", "give me the verdict", "create the listing draft", "what should I do".
  These phrases only trigger this skill AFTER HITL approval — if approval is not present
  in session context, the skill returns the HITL error, not a report.

  Do NOT use for: any task before HITL approval. Do NOT use for research, fee searching,
  competitor analysis, or margin calculation. Do NOT use to generate partial or draft reports
  before the seller has reviewed the full summary.
version: 1.0.0
allowed-tools: Read, Bash
---

# Report Generator

## Purpose

This skill generates the final DropSmart intelligence report after the seller has reviewed
the full pipeline summary and explicitly approved at the HITL checkpoint. It synthesizes all
prior agent outputs into three deliverables: a Go / Proceed with Caution / Do Not Proceed
verdict with point-by-point reasoning, a recommended business strategy, and a ready-to-use
marketplace listing draft optimized for the target platform's character limits, style guide,
and high-volume keywords discovered by the Competitor Agent. This is the only skill in
DropSmart that requires a human gate before execution.

**Note:** In DropSmart's current implementation, this skill is invoked
directly by the Orchestrator (agents/orchestrator.py) as one fixed step
in a sequential pipeline, not via dynamic trigger-phrase matching. The
trigger phrases below document the skill's intended scope and are not
an active routing mechanism in this version.

## When to Use

- Seller has reviewed the pre-report summary (supplier, competitor, fee, margin, risk results)
- Seller has explicitly typed `APPROVE` at the HITL checkpoint
- Session context contains `hitl_approved: true` flag set by the Orchestrator
- All five prior agent outputs are available in session context
- Pipeline is at Step 6 (Report Generator is always the final step)

## When NOT to Use

- `hitl_approved` flag is absent from session context — stop immediately, return error
- `hitl_approved` flag is `false` — stop immediately, return error
- Any prior agent output is missing from session context — report cannot be generated
  without the full data set
- User asks for a "quick summary" or "preview" before approving — this skill does not
  generate previews; the HITL summary shown before approval serves that purpose
- Task is to search for data, calculate fees, or analyze competitors (use appropriate skills)

## Step-by-Step Workflow

1. **CHECK HITL APPROVAL FIRST — before any other action:**
   Read `hitl_approved` from session context.
   - If `hitl_approved` is `true` → proceed to Step 2
   - If `hitl_approved` is `false` or missing → stop immediately and return:
     ```
     ERROR: HITL approval required before report generation.
     The seller must review the full summary and type APPROVE at the checkpoint.
     Report generation is blocked until approval is recorded in session context.
     ```
   This check must happen before any other processing. No exceptions.

2. **Read all prior agent outputs from session context:**
   - Supplier Research: recommended supplier, unit cost, reliability
   - Competitor Analysis: price range, top competitors, keywords, trend, seasonality
   - Fee Research: complete fee breakdown, any warnings (UNVERIFIED, OUTDATED, MISSING)
   - Margin Calculator: net profit, margin %, break-even price, LOW MARGIN flag if present
   - Risk Assessor: dimension scores, overall risk level, top 3 risks

3. **Load platform config YAML** for the target marketplace — read `listing_constraints`
   including `title_max_chars`, `bullet_count`, `description_max_chars`, and `title_style`.
   These values are never hardcoded — always loaded from YAML.

4. **Determine the verdict** using this decision logic:
   - **Go:** overall risk level is LOW, margin is above 20%, and no MISSING fee warnings
   - **Proceed with Caution:** overall risk level is MEDIUM, OR margin is 15–20%, OR any
     UNVERIFIED or OUTDATED fee warnings are present
   - **Do Not Proceed:** overall risk level is HIGH, OR margin is below 15%, OR any dimension
     scores 9–10, OR MISSING fees were not resolved before HITL

5. **Write verdict reasoning** as 3–5 bullet points. Each bullet must reference a specific
   data point from a named agent output:
   - Good: *"Margin of 44.2% (Margin Calculator) provides strong buffer against Daraz fee
     increases or competitor price drops."*
   - Bad: *"Margin is good."* (too vague — rejected)

6. **Write recommended strategy** — a short paragraph (3–5 sentences) advising on:
   - Which business model to use (and why, based on supplier and fee data)
   - Pricing approach (where to position within the competitor sweet spot)
   - Key risks to monitor from the Risk Assessor top 3
   - Any conditions that must be met if verdict is "Proceed with Caution"

7. **Generate listing draft** using:
   - Keywords: use only the keyword list from Competitor Agent output — never invent keywords
   - Title: write within `title_max_chars` from platform YAML, in `title_style` from YAML:
     - `keyword-dense` (Daraz, Walmart): lead with product type and key feature, pack keywords
     - `feature-dense` (Amazon): brand + key features + spec in first 80 chars
     - `artisan-conversational` (Etsy): natural language, unique/handmade tone
   - Bullets: write exactly `bullet_count` bullets from YAML (0 for Etsy — use description)
   - Description: write within `description_max_chars` from YAML
   - Verify title length against `title_max_chars` — if over limit, trim and note the cut

8. **Return the complete report** with all three sections: verdict, strategy, listing draft.

## Output Format

```
DROPSMART INTELLIGENCE REPORT
Product: [product name]
Marketplace: [marketplace_id]
Business Model: [dropshipping / fbs / fbm]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

VERDICT:  [ ✅ GO  /  ⚠ PROCEED WITH CAUTION  /  ❌ DO NOT PROCEED ]

REASONING
• [Data-referenced bullet point from specific agent output]
• [Data-referenced bullet point from specific agent output]
• [Data-referenced bullet point from specific agent output]
• [Additional bullet if warranted]
• [Additional bullet if warranted]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

RECOMMENDED STRATEGY
[3–5 sentence strategy paragraph covering business model, pricing, risks to monitor,
and any conditions for "Proceed with Caution" verdicts]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

LISTING DRAFT

TITLE ([N] / [title_max_chars] characters):
[listing title — within character limit, in platform style, with keywords]

BULLET POINTS:  (omit this section if bullet_count is 0)
• [Bullet 1 — key feature with keyword]
• [Bullet 2 — key feature with keyword]
• [Bullet 3 — key feature with keyword]
• [Additional bullets per platform bullet_count]

DESCRIPTION:
[Full listing description — within description_max_chars, in platform tone,
incorporating high-volume keywords from Competitor Analysis naturally]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ACTIVE WARNINGS  (carried forward from pipeline)
[Any UNVERIFIED, OUTDATED, or LOW MARGIN warnings from prior agents]
[If none: "No active warnings."]
```

## Important Rules

- **DO** check `hitl_approved: true` as the absolute first action — no exceptions
- **DO** stop and return the HITL error if the flag is absent or false — never proceed silently
- **DO** cite the specific agent source for every verdict reasoning bullet
- **DO** use only keywords extracted by the Competitor Agent — never invent keywords for the listing
- **DO** load all listing constraints from the platform YAML — never hardcode character limits
- **DO** verify title length before returning — trim if over limit and note the trim
- **DO** carry forward all pipeline warnings (UNVERIFIED, OUTDATED, LOW MARGIN) into the
  report's warnings section — do not suppress them in the final output
- **DO** omit the bullet points section entirely for Etsy (bullet_count is 0 in etsy_us.yaml)
- **DO NOT** generate any report content before verifying the HITL flag
- **DO NOT** invent keywords, supplier names, competitor data, or fee values in the report —
  every claim must trace back to a prior agent's output
- **DO NOT** generate a "Do Not Proceed" report without listing niche alternatives or
  differentiators — the seller needs actionable direction even when the verdict is negative
