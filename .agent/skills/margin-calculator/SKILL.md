---
name: margin-calculator
description: |
  Calculates net profit margin using supplier cost, all platform fees from fee-structure-research,
  and the seller's own packaging and courier costs. Produces a complete line-by-line deduction
  table with no grouped or hidden fees. Every fee from the fee research output appears as its
  own named line. Triggers after fee research completes and before the Risk Assessor runs.

  Trigger phrases: "calculate margin", "what is my profit", "margin calculation", "net profit",
  "break-even price", "how much will I make", "profit per unit", "calculate fees", "margin %".

  Do NOT use for: searching fees, finding suppliers, competitor research, risk scoring,
  or any task that requires external data gathering. This skill does pure calculation only.
version: 1.0.0
allowed-tools: Read, Bash
---

# Margin Calculator

## Purpose

This skill takes all cost and fee data collected by previous agents and produces a precise,
transparent margin calculation. Every single deduction appears on its own named line — no
fees are grouped, bundled, or hidden. The seller must be able to trace exactly where every
unit of money goes. This transparency is a core design principle of DropSmart: sellers who
see every deduction are less likely to be surprised by real-world results. The output feeds
directly into the Risk Assessor Agent, which uses margin percentage to score Margin Adequacy.

**Note:** In DropSmart's current implementation, this skill is invoked
directly by the Orchestrator (agents/orchestrator.py) as one fixed step
in a sequential pipeline, not via dynamic trigger-phrase matching. The
trigger phrases below document the skill's intended scope and are not
an active routing mechanism in this version.

## When to Use

- Fee Structure Research has completed and all fees are in session context
- Supplier Research has returned a unit cost range and recommended supplier
- Selling price is known from Competitor Analysis sweet spot (or user-provided override)
- Seller's own costs (packaging, courier) have been collected by Orchestrator
- Pipeline is at Step 4 (Margin Calculator always follows Fee Research)

## When NOT to Use

- Fee structure is incomplete — wait for all fees to be confirmed before calculating
- Supplier cost is unknown — do not proceed without at minimum a cost estimate
- Task requires searching for any external data (this skill does pure calculation only)
- Session context does not contain a `fees` object from fee-structure-research output

## Step-by-Step Workflow

1. **Read all required inputs from session context:**
   - `selling_price` — from Competitor Analysis sweet spot, or user-provided override
   - `supplier_unit_cost` — from Supplier Research recommended supplier (use midpoint of range)
   - `packaging_cost` — from Orchestrator user input collection
   - `courier_cost` — from Orchestrator user input (0 for `fbm`, required for `fbs` and
     `dropshipping`)
   - `fees` — complete structured fee object from Fee Structure Research output

2. **Validate completeness before calculating:**
   - Confirm no fees are flagged as MISSING in the fee research output
   - If any fees are flagged MISSING, do not proceed — return:
     *"Cannot calculate margin: incomplete fee data. Resolve missing fees first."*
   - If fees are flagged UNVERIFIED or OUTDATED, proceed but carry those warnings forward
     into the margin output so they are visible at HITL checkpoint

3. **Build the deduction table by iterating through the fee object.** For each fee:
   - **Percentage fees:** multiply the fee rate by the `applies_to` base amount
     (e.g., commission = selling_price × commission_rate)
   - **Flat fees:** use the fee amount directly
   - **Tiered fees:** confirm the correct tier was already selected in fee research output —
     use that flat amount. Do not re-select the tier here.
   - **Compound fees (percentage on a fee amount):** calculate the base fee first, then apply
     the percentage to that result (e.g., VAT on commission = commission_amount × vat_rate)

4. **Calculate net profit per unit:**
   ```
   net_profit = selling_price
              - supplier_unit_cost
              - packaging_cost
              - courier_cost
              - sum(all_fees)
   ```

5. **Calculate margin percentage:**
   ```
   margin_pct = (net_profit / selling_price) × 100
   ```

6. **Calculate break-even selling price** — the minimum price where net profit equals zero:
   ```
   break_even = supplier_unit_cost + packaging_cost + courier_cost + sum(all_fees)
   ```
   Note: for percentage-based fees, break-even requires iterative calculation since fees
   depend on selling price. Solve iteratively or algebraically.

7. **Calculate monthly profit projections** at three volume levels:
   - 50 units per month
   - 100 units per month
   - 200 units per month

8. **Apply the low margin flag:** if `margin_pct` is below 15%, attach this warning to output:
   *"⚠ LOW MARGIN: Net margin of [X]% is below the 15% minimum threshold. This product
   carries significant margin risk — small fee increases or price drops could eliminate profit."*

9. **Return formatted deduction table** with all figures and warnings.

## Output Format

```
MARGIN CALCULATION
Product: [product name]
Marketplace: [marketplace_id]
Business Model: [dropshipping / fbs / fbm]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Selling Price:                    [currency]  [amount]
─────────────────────────────────────────────────────
- Supplier Cost:                             [amount]
- Packaging Cost:                            [amount]
- Courier / Shipping:                        [amount]
- [Fee Name — e.g. Commission (8%)]:         [amount]
- [Fee Name — e.g. VAT on Commission (15%)]: [amount]
- [Fee Name — e.g. Payment Processing (2%)]: [amount]
- [Fee Name — e.g. VAT on Payment Proc.]:    [amount]
- [Fee Name — e.g. Handling Fee (tier 20)]:  [amount]
- [Fee Name — e.g. VAT on Handling Fee]:     [amount]
- [every remaining fee on its own line]
─────────────────────────────────────────────────────
Net Profit per Unit:              [currency]  [amount]
Margin %:                                    [X.X%]
Break-Even Price:                 [currency]  [amount]
─────────────────────────────────────────────────────
Monthly Projection (50 units):    [currency]  [amount]
Monthly Projection (100 units):   [currency]  [amount]
Monthly Projection (200 units):   [currency]  [amount]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WARNINGS
[LOW MARGIN warning if margin < 15%]
[Any UNVERIFIED or OUTDATED fee warnings carried forward from Fee Research]
[If no warnings: "No warnings — all inputs verified."]
```

## Important Rules

- **DO** show every single fee from the fee research output as its own named line — no exceptions
- **DO** carry forward UNVERIFIED and OUTDATED warnings from fee research — do not suppress them
- **DO** flag margin below 15% explicitly with the LOW MARGIN warning label
- **DO** refuse to calculate if fee data is flagged as MISSING — incomplete input means
  incomplete output which will mislead the seller
- **DO** use the midpoint of the supplier cost range if only a range is available, and note
  this assumption explicitly in output
- **DO NOT** group any fees — every line from the fee object is its own row in the table
- **DO NOT** perform this calculation without a complete fee object in session context
- **DO NOT** invent or estimate any cost that was not provided — mark as `"not provided"` and
  note the impact on accuracy
- **DO NOT** round intermediate calculations — only round the final display values to 2
  decimal places to avoid compounding rounding errors in the breakdown
