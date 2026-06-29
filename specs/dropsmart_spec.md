# DropSmart — Technical Specification

**Version:** 1.0.0
**Last Updated:** 2026-06-27
**Status:** Active — single source of truth for all development

This document is the authoritative technical specification for DropSmart.
Every agent, every file, and every design decision must conform to what is written here.
When this spec conflicts with any other document, this spec wins.

---

## Table of Contents

1. [Problem Statement](#1-problem-statement)
2. [Solution Overview](#2-solution-overview)
3. [Supported Platforms and Business Models](#3-supported-platforms-and-business-models)
4. [User Inputs Required](#4-user-inputs-required)
5. [BDD Scenarios](#5-bdd-scenarios)
6. [Architecture Diagram](#6-architecture-diagram)
7. [MCP Server Specification](#7-mcp-server-specification)
8. [Platform Config Structure](#8-platform-config-structure)
9. [Security Requirements](#9-security-requirements)
10. [Technology Versions](#10-technology-versions)

---

## 1. Problem Statement

E-commerce sellers manually research products before listing them on a marketplace.
A single product research session involves all of the following steps — done by hand, often incorrectly:

**Step 1 — Supplier Search**
The seller searches Alibaba, AliExpress, or niche supplier directories. They visit multiple
websites, compare pricing, check MOQs, and try to assess supplier reliability — all manually.
This alone takes 1–2 hours per product.

**Step 2 — Competitor Price Research**
The seller browses the target marketplace searching for similar products. They manually note
prices, review counts, and try to estimate whether the market is too crowded. No structured data,
no trend analysis — just browsing.

**Step 3 — Fee Calculation**
This is where most sellers lose money. Sellers routinely forget or miscalculate:
- VAT applied on top of commission (not just commission itself)
- VAT applied on top of payment processing fees
- Handling fees charged separately from commission
- FBA fulfillment fees that vary by product size and weight
- Seasonal storage fee increases for Amazon
- Offsite advertising fees on Etsy

A seller who calculates only the headline commission percentage and ignores the rest can
easily overestimate their margin by 15–25 percentage points.

**Step 4 — Trend and Seasonality Assessment**
Most sellers skip this entirely, or rely on gut feel. Products that appear profitable in
November may be dead stock by February. Without trend data, sellers make timing mistakes.

**Step 5 — Go/No-Go Decision**
After hours of fragmented research, the seller makes a gut-feel decision with no structured
risk scoring and no systematic verdict framework.

### The Core Problem

Manual product research is slow, error-prone, and structurally incomplete. Sellers consistently
underestimate fees, overestimate margins, miss competitive signals, and ignore risk dimensions
they did not think to check. The result is wasted inventory investment, margin erosion, and
failed listings.

**DropSmart automates this entire workflow with 7 specialized agents.**

---

## 2. Solution Overview

DropSmart is a sequential multi-agent pipeline built on Google ADK. The seller provides one
product idea and a few inputs. The system runs all research automatically, presents a full
summary for human review, and — after explicit approval — generates a final Go/No-Go verdict
with a ready-to-use marketplace listing draft.

### What the Seller Experiences

1. Seller starts DropSmart and answers a short series of questions (marketplace, business model, product, costs)
2. System runs all 7 agents in sequence — typically completing in under 2 minutes
3. System presents a consolidated pre-report summary covering suppliers, competitors, fees, margin, and risk
4. Seller reviews the summary and types `APPROVE` or `REJECT` at the HITL checkpoint
5. If approved, the Report Agent generates the final verdict and listing draft
6. Seller receives a structured, actionable intelligence report

### What the System Does

- **Supplier Research** — finds real suppliers matched to the seller's business model
- **Competitor Analysis** — pulls live listing data from the target marketplace
- **Fee Research** — dynamically fetches the current, complete fee structure — no hardcoded values
- **Margin Calculation** — computes exact profit with every deduction shown line by line
- **Risk Assessment** — scores six dimensions and issues an overall risk level
- **Report Generation** — produces a verdict and a marketplace-optimized listing draft

### Human-in-the-Loop (HITL) Checkpoint

The HITL checkpoint sits between the Risk Assessor Agent and the Report Generator Agent.
The pipeline pauses and presents the full summary to the seller. The Report Agent is
**never called** unless the seller explicitly approves. This is a hard architectural
constraint — not a preference.

---

## 3. Supported Platforms and Business Models

| Marketplace | Region | Currency | Dropshipping | FBS | FBM / FBA |
|---|---|---|---|---|---|
| Daraz | Pakistan | PKR | Yes | Yes | Yes (FBD) |
| Walmart | USA | USD | Yes | Yes | Yes |
| Amazon | USA | USD | Yes | Yes | Yes (FBA) |
| Etsy | USA | USD | Yes | Yes | No ¹ |

¹ Etsy has no in-house fulfillment. Sellers use FBS or third-party 3PL only.

**FBD (Fulfilled by Daraz):** Daraz's own fulfillment program. Seller sends bulk stock to
Daraz warehouse. Daraz handles storage, packing, and shipping. First 30 days storage is free.
Additional FBD fees apply on top of standard commission and VAT fees.

### Business Model Definitions

**Dropshipping**
Seller lists the product without holding inventory. When an order is placed, the supplier
ships directly to the customer. Seller never touches the product. Key concerns: supplier
reliability, direct shipping speed, blind/white-label capability.

**FBS — Fulfilled by Seller**
Seller purchases inventory from the supplier and holds it themselves. When an order is placed,
the seller ships to the customer. Key concerns: MOQ, storage cost, courier cost, lead time.

**FBM — Fulfilled by Marketplace (FBA on Amazon)**
Seller sends bulk inventory to the marketplace's fulfillment center. The marketplace handles
all picking, packing, and shipping. Key concerns: FBA/WFS fulfillment fees, storage fees,
prep requirements, labeling compliance.

---

## 4. User Inputs Required

The Orchestrator Agent collects these inputs sequentially before running any specialist agent.
All inputs are validated before the pipeline starts.

| # | Input | Type | Validation Rule | Required |
|---|---|---|---|---|
| 1 | Marketplace | String (enum) | Must be one of: `daraz_pk`, `walmart_us`, `amazon_us`, `etsy_us` | Yes |
| 2 | Region | Derived | Auto-derived from marketplace selection — not asked separately | Auto |
| 3 | Business Model | String (enum) | Must be one of: `dropshipping`, `fbs`, `fbm` — validated against platform support matrix | Yes |
| 4 | Product name or idea | String | Non-empty, max 200 characters, no special characters that break search queries | Yes |
| 5 | Packaging cost per unit | Float | Non-negative number; 0 is valid (seller may include it in supplier cost) | Yes |
| 6 | Courier cost per unit | Float | Required only for `fbs` and `dropshipping`; set to 0 for `fbm` | Conditional |

### Validation Failure Behavior

If any required input fails validation, the Orchestrator must:
1. Tell the seller which input failed and why
2. Re-prompt for that specific input only
3. Not proceed to any specialist agent until all inputs pass validation

---

## 5. BDD Scenarios

These scenarios define expected system behavior. They are the acceptance criteria for the
complete pipeline. All three must pass before the system is considered working.

---

### Scenario 1: Daraz FBS — Successful Go Verdict

```gherkin
Feature: Full pipeline run for Daraz Pakistan FBS

  Scenario: Seller researches wireless earbuds for Daraz FBS and receives a Go verdict

    Given the user selects Daraz Pakistan as the target marketplace
    And the user selects FBS as the business model
    And the user enters "wireless earbuds" as the product
    And the user enters PKR 50 as packaging cost per unit
    And the user enters PKR 200 as courier cost per unit

    When the Orchestrator begins the pipeline

    Then the Supplier Research Agent runs first
    And it returns at least 3 supplier options
    And each supplier entry includes name, estimated unit cost, MOQ, and shipping time
    And all suppliers are flagged as FBS-compatible (have MOQ and bulk pricing)

    And the Competitor Analysis Agent runs next
    And it returns competitor listings sourced from daraz.pk
    And it returns a price range (minimum, median, maximum)
    And it returns at least 5 high-volume keywords from top-ranking titles
    And it returns a trend direction value of growing, stable, or declining

    And the Fee Structure Research Agent runs next
    And it dynamically searches for and returns all 6 Daraz fee components:
      | Fee Component              |
      | Commission rate            |
      | VAT on commission          |
      | Payment processing fee     |
      | VAT on payment processing  |
      | Handling fee               |
      | VAT on handling fee        |
    And every fee entry includes the source URL where it was found

    And the Margin Calculator Agent runs next
    And it shows every deduction on its own line
    And no fees are grouped or hidden
    And it shows net profit per unit, margin percentage, and break-even price

    And the Risk Assessor Agent runs next
    And it scores all 6 risk dimensions individually:
      | Dimension             |
      | Market Saturation     |
      | Margin Adequacy       |
      | Supplier Reliability  |
      | Competition Level     |
      | Trend Direction       |
      | Seasonality Risk      |
    And it returns an overall risk level of LOW or MEDIUM

    And the HITL checkpoint appears
    And the system displays the full summary to the seller
    And the pipeline is paused — the Report Agent has not run

    When the user types APPROVE

    Then the Report Generator Agent runs
    And it returns a verdict of "Go"
    And it returns a listing title within Daraz character limits
    And the listing title includes at least 2 high-volume keywords from the Competitor Agent
    And it returns a listing description optimized for daraz.pk conventions
```

---

### Scenario 2: Amazon FBA — High Saturation, Do Not Proceed

```gherkin
Feature: Pipeline correctly identifies oversaturated Amazon product

  Scenario: Seller researches yoga mats for Amazon FBA and receives a Do Not Proceed verdict

    Given the user selects Amazon USA as the target marketplace
    And the user selects FBM (FBA) as the business model
    And the user enters "yoga mat" as the product

    When all agents run in sequence

    Then the Competitor Analysis Agent returns a high review count on top listings
    And it returns a high number of established sellers with 1000+ reviews

    And the Risk Assessor Agent flags Market Saturation with a score of 8 or higher
    And the Risk Assessor Agent flags Competition Level with a score of 8 or higher
    And the overall risk level returned is HIGH

    And the HITL checkpoint appears with the full summary
    And the summary clearly shows HIGH risk with the flagged dimensions

    When the user types APPROVE

    Then the Report Generator Agent runs
    And the verdict is "Do Not Proceed"
    And the report includes reasoning that references the saturation and competition scores
    And the report includes at least 2 suggested niche alternatives or differentiators
    And the listing draft is not generated (verdict precludes it) or is clearly marked as conditional
```

---

### Scenario 3: Walmart Dropshipping — Strong Go

```gherkin
Feature: Pipeline identifies a strong dropshipping opportunity on Walmart

  Scenario: Seller researches LED desk lamps for Walmart Dropshipping and receives a Strong Go

    Given the user selects Walmart USA as the target marketplace
    And the user selects Dropshipping as the business model
    And the user enters "LED desk lamp" as the product
    And packaging cost is 0 (dropshipping — seller does not package)
    And courier cost is 0 (dropshipping — supplier ships directly)

    When all agents run in sequence

    Then the Supplier Research Agent returns at least 1 supplier
    And that supplier is flagged as dropship-compatible
    And the supplier entry confirms direct-to-customer shipping capability
    And the supplier entry confirms no MOQ requirement

    And the Margin Calculator Agent shows a net margin of 40% or higher
    And every fee deduction is shown line by line
    And the break-even price is shown

    And the Risk Assessor Agent returns an overall risk level of LOW or MEDIUM

    And the HITL checkpoint appears

    When the user types APPROVE

    Then the Report Generator Agent runs
    And the verdict is "Go" or "Strong Go"
    And the listing title is within Walmart character limits
    And the listing uses keywords from the Competitor Agent output
    And the report includes a recommended dropshipping strategy
```

---

## 6. Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                         USER INPUT                              │
│  Marketplace · Region · Business Model · Product · Costs        │
└─────────────────────────┬───────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                    ORCHESTRATOR AGENT                           │
│  • Validates all inputs                                         │
│  • Manages session context dict                                 │
│  • Calls each agent in sequence                                 │
│  • Enforces HITL checkpoint                                     │
│  • Passes results forward between agents                        │
└──────┬──────────────────────────────────────────────────────────┘
       │
       │  Step 1
       ▼
┌──────────────────────────────────────────────────────┐
│              SUPPLIER RESEARCH AGENT                 │
│  Tool: search_supplier_prices() via MCP              │
│  Input: product, business_model, region              │
│  Output: list of suppliers with cost, MOQ, shipping  │
└──────┬───────────────────────────────────────────────┘
       │
       │  Step 2
       ▼
┌──────────────────────────────────────────────────────┐
│             COMPETITOR ANALYSIS AGENT                │
│  Tool: search_competitor_listings() via MCP          │
│  Input: product, marketplace, region                 │
│  Output: prices, reviews, keywords, trends           │
└──────┬───────────────────────────────────────────────┘
       │
       │  Step 3
       ▼
┌──────────────────────────────────────────────────────┐
│           FEE STRUCTURE RESEARCH AGENT               │
│  Tool: search_marketplace_fees() via MCP             │
│  Input: marketplace, region, business_model          │
│  Output: named fees with amounts and source URLs     │
│  Rule: NEVER hardcode — always search fresh          │
└──────┬───────────────────────────────────────────────┘
       │
       │  Step 4
       ▼
┌──────────────────────────────────────────────────────┐
│             MARGIN CALCULATOR AGENT                  │
│  No external tools — pure calculation                │
│  Input: supplier cost + fees + seller costs          │
│  Output: line-by-line deduction table + margin %     │
└──────┬───────────────────────────────────────────────┘
       │
       │  Step 5
       ▼
┌──────────────────────────────────────────────────────┐
│               RISK ASSESSOR AGENT                    │
│  Tool: web_search() via MCP (optional, for trends)   │
│  Input: all prior agent outputs                      │
│  Output: 6 dimension scores + overall risk level     │
└──────┬───────────────────────────────────────────────┘
       │
       │
       ▼
╔══════════════════════════════════════════════════════╗
║           ⚠  HITL CHECKPOINT  ⚠                     ║
║                                                      ║
║  Pipeline pauses. Full summary shown to seller.      ║
║  Seller types APPROVE or REJECT.                     ║
║                                                      ║
║  REJECT → pipeline ends, no report generated.        ║
║  APPROVE → Report Agent is called.                   ║
║                                                      ║
║  Report Agent MUST NOT run without this approval.    ║
╚══════╦═══════════════════════════════════════════════╝
       ║
       ║  Step 6 (only after APPROVE)
       ▼
┌──────────────────────────────────────────────────────┐
│              REPORT GENERATOR AGENT                  │
│  Input: all prior outputs + human approval flag      │
│  Output: verdict + reasoning + listing draft         │
│  Rule: check approval flag before executing          │
└──────┬───────────────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────────────────────────────┐
│                       FINAL REPORT                              │
│  Go / Proceed with Caution / Do Not Proceed                     │
│  Reasoning · Strategy · Listing Draft                           │
└─────────────────────────────────────────────────────────────────┘

External Dependency:
  All agents that need web access call through:

  ┌──────────────────────────────────────┐
  │           MCP SERVER                 │
  │  search_mcp.py                       │
  │  Single point of contact for         │
  │  all Serper.dev API calls            │
  │  Logs every tool call for audit      │
  └──────────────────────────────────────┘
```

---

## 7. MCP Server Specification

**File:** `mcp_server/search_mcp.py`

The MCP server is the only component in DropSmart that communicates with external APIs.
No agent calls Serper.dev or any other external service directly. All external calls go
through MCP tools. This centralizes rate limiting, error handling, logging, and API key management.

### Tools Exposed

---

#### `web_search`

General-purpose web search. Used by the Risk Assessor for trend data and by any agent
that needs information not covered by a specialized tool.

```
web_search(
    query: str,         # The search query string
    num_results: int    # Number of results to return (default: 5, max: 10)
) -> list[dict]
```

**Returns:** List of result objects, each containing:
- `title` — page title
- `url` — source URL
- `snippet` — text excerpt from the result

---

#### `search_marketplace_fees`

Searches for the current fee structure for a specific marketplace, region, and business model.
Uses query templates from the platform's YAML config file to construct targeted searches.

```
search_marketplace_fees(
    marketplace: str,       # e.g. "daraz_pk", "amazon_us", "walmart_us", "etsy_us"
    region: str,            # e.g. "PK", "US"
    business_model: str,    # e.g. "dropshipping", "fbs", "fbm"
    product_category: str   # Optional — some fees vary by category
) -> dict
```

**Returns:** Structured fee breakdown:
```python
{
    "marketplace": "daraz_pk",
    "business_model": "fbs",
    "fees": [
        {
            "name": "Commission Rate",
            "type": "percentage",
            "value": 8.0,
            "applies_to": "selling_price",
            "source_url": "https://..."
        },
        {
            "name": "VAT on Commission",
            "type": "percentage",
            "value": 15.0,          # 16.0 for Punjab sellers — Fee Agent must resolve province
            "applies_to": "commission_amount",
            "source_url": "https://...",
            "province_note": "Defaulted to 15% — Punjab sellers should use 16%"
        }
        # ... all remaining fee components
    ],
    "fetched_at": "2026-06-27T10:30:00Z"
}
```

---

#### `search_supplier_prices`

Searches for suppliers and estimated wholesale prices for a given product.
Adapts search strategy based on business model.

```
search_supplier_prices(
    product: str,           # Product name or description
    business_model: str,    # "dropshipping", "fbs", or "fbm"
    region: str             # Target region for shipping context
) -> list[dict]
```

**Returns:** List of supplier objects:
```python
[
    {
        "supplier_name": "Example Supplier Co.",
        "website": "https://...",
        "estimated_unit_cost_min": 5.50,
        "estimated_unit_cost_max": 8.00,
        "currency": "USD",
        "moq": 10,
        "shipping_time_days": "7-14",
        "dropship_compatible": True,
        "source_url": "https://...",
        "reliability_notes": "4.8/5 on Alibaba, 5 years trading"
    }
]
```

---

#### `search_competitor_listings`

Searches for live product listings on the target marketplace.
Extracts pricing, review, and keyword data from results.

```
search_competitor_listings(
    product: str,           # Product name
    marketplace: str,       # "daraz_pk", "amazon_us", "walmart_us", "etsy_us"
    region: str             # "PK" or "US"
) -> dict
```

**Returns:** Competitive landscape object:
```python
{
    "marketplace": "amazon_us",
    "product": "yoga mat",
    "price_range": {
        "min": 12.99,
        "max": 89.99,
        "median": 28.00,
        "currency": "USD"
    },
    "top_listings": [
        {
            "title": "...",
            "price": 24.99,
            "review_count": 14820,
            "avg_rating": 4.6,
            "seller_name": "..."
        }
    ],
    "monthly_sales_estimate": "5,000–10,000 units",
    "trend_direction": "stable",
    "seasonality_signal": "low",
    "high_volume_keywords": ["non slip yoga mat", "thick yoga mat", "exercise mat"],
    "fetched_at": "2026-06-27T10:30:00Z"
}
```

### Fee Research Risk Mitigation

These five rules apply to the Fee Structure Research Agent on **all platforms** — Daraz,
Walmart, Amazon, and Etsy. No exception is permitted for any marketplace or business model.

---

#### Rule 1 — Multi-Source Verification

The Fee Agent must run at least **3 different search queries** per marketplace fee structure.
A single search result is never sufficient.

If results conflict across sources, the agent must use the **most conservative (highest) fee
estimate** for each disputed fee. Protecting the seller from margin overestimation takes
priority over optimistic accuracy.

The MCP server must record which query produced which result so conflicts can be traced.

---

#### Rule 2 — Source URL Required

Every fee value returned must include the URL of the page where it was found.

If a fee value cannot be tied to a source URL, it must be marked **`UNVERIFIED`** in the
fee output and displayed with an explicit warning to the user at the HITL checkpoint:

> **⚠ UNVERIFIED FEE:** [Fee Name] could not be confirmed with a source URL.
> This value is an estimate. Verify manually before listing.

The pipeline must not silently pass an unverified fee into the Margin Calculator.

---

#### Rule 3 — Date Freshness Check

The Fee Agent must record the search date on all fee results.

If no search results dated within the **last 12 months** are found for a fee category, the
agent must attach this warning to that fee in the output:

> **⚠ POTENTIALLY OUTDATED:** Fee data for [Fee Name] could not be confirmed within the
> last 12 months. Verify manually before listing.

This warning must be visible to the seller at the HITL checkpoint — not suppressed or buried.

---

#### Rule 4 — No Flat Rate Assumption

The Fee Agent must never assume a single flat rate for any fee on any platform. Many platforms
use tiered or category-specific fee structures that cannot be collapsed to one number.

Known tiered structures the agent must handle correctly:

| Platform | Fee | Structure Type |
|---|---|---|
| Daraz | Handling Fee | Tiered by selling price (PKR 0–500 = 10, 501–1,000 = 15, 1,001–2,000 = 20, 2,001+ = 60) |
| Amazon | FBA Fulfillment Fee | Tiered by product size and weight |
| Walmart | Referral Fee | Varies by product category |
| Etsy | Offsite Ads Fee | Only applies above USD 10,000 annual sales threshold |

The agent must search for the specific tier structure applicable to the product being
researched — never substitute a single representative number for a tiered fee.

---

#### Rule 5 — Missing Fee Detection

Each platform config YAML defines the required `fee_categories` for that platform. These are
the fee names that **must** appear in the Fee Agent's output for the result to be considered
complete.

If any fee category listed in the platform YAML is absent from search results, the agent must:

1. **Flag the missing fee explicitly by name** in the intermediate output
2. **Retry with a different search query** — using an alternative query from `fee_search_queries` in the YAML
3. **If still missing after retry** — pause the pipeline and notify the user before continuing:

> **⚠ INCOMPLETE FEE DATA:** [Fee Name] could not be found for [Marketplace].
> The margin calculation may be understated. Confirm this fee manually before approving.

No platform's fee list may be treated as complete until every category defined in its YAML
config has been accounted for. Partial fee data that silently reaches the Margin Calculator
is a spec violation.

---

### MCP Server Logging

Every tool call must be logged before the call executes and after the response is received.

**Log entry format:**
```
[2026-06-27T10:30:00Z] TOOL_CALL | agent=fee_agent | tool=search_marketplace_fees | query={"marketplace": "daraz_pk", ...}
[2026-06-27T10:30:01Z] TOOL_RESP | agent=fee_agent | tool=search_marketplace_fees | status=success | result_count=6
```

---

## 8. Platform Config Structure

Each platform has one YAML config file in `platform_configs/`. These files are the only
place where platform-specific values are defined. Agent code must never hardcode these values.

**File naming convention:** `{marketplace_id}.yaml`

### Full YAML Schema

```yaml
# Metadata
marketplace_name: string           # Human-readable name (e.g. "Daraz Pakistan")
marketplace_id: string             # Machine ID (e.g. "daraz_pk")
region: string                     # ISO region code (e.g. "PK", "US")
currency: string                   # ISO currency code (e.g. "PKR", "USD")

# Business models this platform supports
supported_business_models:
  - dropshipping
  - fbs
  # - fbm  (only if supported)

# Fee categories the Fee Agent must search for
# These are the fee names that must appear in the output
fee_categories:
  - name: string                   # Display name of this fee
    type: string                   # "percentage", "flat", or "tiered"
    applies_to: string             # What the fee is calculated against
    tiers:                         # Only present when type is "tiered"
      - max_price: integer | null  # Upper bound of this tier (null = no ceiling)
        fee: integer               # Flat fee amount for this tier

# Search query templates for the Fee Agent
# {product_category} is replaced at runtime
fee_search_queries:
  - string                         # e.g. "Daraz Pakistan seller commission fees 2024 {product_category}"
  - string                         # Multiple queries increase coverage

# Listing constraints for the Report Agent
listing_constraints:
  title_max_chars: integer         # Maximum title length
  bullet_count: integer            # Number of bullet points allowed
  description_max_chars: integer   # Maximum description length (if applicable)
  title_style: string              # e.g. "keyword-dense", "conversational", "artisan"

# Supplier source hints for the Supplier Agent
# These guide search query construction — not hardcoded supplier lists
supplier_sources:
  - string                         # e.g. "Alibaba", "AliExpress", "local Pakistan suppliers"
```

### Example — `daraz_pk.yaml`

```yaml
marketplace_name: "Daraz Pakistan"
marketplace_id: "daraz_pk"
region: "PK"
currency: "PKR"

supported_business_models:
  - dropshipping
  - fbs

fee_categories:
  - name: "Commission Rate"
    type: "percentage"
    applies_to: "selling_price"
  - name: "VAT on Commission"
    type: "percentage"
    applies_to: "commission_amount"
  - name: "Payment Processing Fee"
    type: "percentage"
    applies_to: "selling_price"
  - name: "VAT on Payment Processing Fee"
    type: "percentage"
    applies_to: "payment_processing_amount"
  - name: "Handling Fee"
    type: "tiered"
    applies_to: "per_order"
    tiers:
      - max_price: 500
        fee: 10
      - max_price: 1000
        fee: 15
      - max_price: 2000
        fee: 20
      - max_price: null        # 2001+ (no upper bound)
        fee: 60
  - name: "VAT on Handling Fee"
    type: "percentage"
    applies_to: "handling_fee_amount"
    # VAT rate is province-dependent: Punjab = 16%, all other provinces = 15%
    # Fee Agent must search for seller's province or default to 15% with a Punjab warning

fee_search_queries:
  - "Daraz Pakistan seller commission fees 2024 {product_category}"
  - "Daraz seller center fee structure Pakistan {product_category}"
  - "site:seller.daraz.pk fees commission"

listing_constraints:
  title_max_chars: 255
  bullet_count: 5
  description_max_chars: 5000
  title_style: "keyword-dense"

supplier_sources:
  - "Alibaba"
  - "AliExpress"
  - "local Pakistan wholesale markets"
  - "China wholesale suppliers"
```

---

## 9. Security Requirements

### API Key Management

- All API keys and secrets must be stored in `.env` only
- `.env` is listed in `.gitignore` — it must never be committed
- `.env.example` lists all required key names with placeholder values only — no real values
- Keys are loaded at application startup using `python-dotenv`
- If a required key is missing from `.env`, the application must raise a clear error and exit before running any agent

### Required `.env` Keys

```
SERPER_API_KEY=your_serper_api_key_here
GOOGLE_API_KEY=your_google_api_key_here
```

### Input Validation

All user inputs are validated by the Orchestrator before any agent runs.
Validation rules are defined in Section 4. Failures prompt re-entry, not pipeline continuation.

### HITL Enforcement

The HITL checkpoint is enforced at two levels:
1. **Orchestrator level** — the Orchestrator must not call the Report Agent without a recorded approval
2. **Report Agent level** — the Report Agent must check the session context for an `hitl_approved: true` flag and raise an error if it is absent or false

Both checks must exist in code. Documentation alone is not sufficient.

### External Call Isolation

The MCP server (`mcp_server/search_mcp.py`) is the only file permitted to make HTTP requests
to external services. No agent file may import `requests`, `httpx`, `aiohttp`, or any HTTP
client directly. All external calls go through MCP tools.

### Audit Logging

The MCP server logs every tool call as specified in Section 7. Logs must include:
- Timestamp (ISO 8601)
- Calling agent name
- Tool name
- Input parameters (sanitized — no API keys in logs)
- Response status
- Result count or summary

---

## 10. Technology Versions

| Package | Version | Purpose |
|---|---|---|
| Python | 3.11+ | Runtime |
| google-adk | latest | Agent framework, Gemini model access |
| mcp | latest | Model Context Protocol server implementation |
| pydantic | latest (v2) | Input validation and data models |
| python-dotenv | latest | `.env` file loading |
| pyyaml | latest | Platform config YAML parsing |
| requests | latest | HTTP client (used only inside MCP server) |

### `requirements.txt` Format

```
google-adk
mcp
pydantic>=2.0
python-dotenv
pyyaml
requests
```

Pinned versions are added once the project stabilizes. During development, use latest.

---

## Appendix A — Daraz Fee Calculation Example

This example shows the correct way to calculate Daraz fees — including all VAT layers,
the tiered handling fee, and province-dependent VAT rates.

### VAT Rate by Province

VAT on fees is not a single national rate in Pakistan. It is province-dependent:

| Province | VAT Rate on Fees |
|---|---|
| Punjab | 16% |
| Sindh | 15% |
| KPK | 15% |
| Balochistan | 15% |

The Fee Agent must search for the seller's province and apply the correct rate.
If the province cannot be determined, default to **15%** and display this warning in the output:

> **Note:** VAT rate defaulted to 15%. If you are registered in Punjab, use 16% —
> this will reduce your net profit by approximately PKR [calculated difference].

### Daraz Handling Fee Tiers

The handling fee is not a flat rate. It is tiered by selling price:

| Selling Price Range (PKR) | Handling Fee (PKR) |
|---|---|
| PKR 0 – 500 | PKR 10 |
| PKR 501 – 1,000 | PKR 15 |
| PKR 1,001 – 2,000 | PKR 20 |
| PKR 2,001 and above | PKR 60 |

The Margin Calculator Agent must select the correct tier based on the product's selling price
before applying VAT on the handling fee. A flat rate must never be used.

### Worked Example (Selling Price PKR 2,500 — non-Punjab seller, VAT 15%)

```
Selling Price:                    PKR 2,500.00
──────────────────────────────────────────────
Supplier Cost:                   - PKR   800.00
Packaging Cost:                  - PKR    50.00
Courier Cost:                    - PKR   200.00
──────────────────────────────────────────────
Commission (8%):                 - PKR   200.00
VAT on Commission (15%):         - PKR    30.00
Payment Processing Fee (2%):     - PKR    50.00
VAT on Payment Proc. (15%):      - PKR     7.50
Handling Fee (tier: 2,001+):     - PKR    60.00
VAT on Handling Fee (15%):       - PKR     9.00
──────────────────────────────────────────────
Net Profit per Unit:               PKR 1,093.50
Margin %:                                43.7%
Break-Even Price:                  PKR 1,406.50
──────────────────────────────────────────────
Monthly Projection (100 units):    PKR 109,350
```

### Punjab Seller Difference (same product, VAT 16%)

```
Commission (8%):                 - PKR   200.00
VAT on Commission (16%):         - PKR    32.00      ← +PKR 2.00 vs 15%
Payment Processing Fee (2%):     - PKR    50.00
VAT on Payment Proc. (16%):      - PKR     8.00      ← +PKR 0.50 vs 15%
Handling Fee (tier: 2,001+):     - PKR    60.00
VAT on Handling Fee (16%):       - PKR     9.60      ← +PKR 0.60 vs 15%
──────────────────────────────────────────────
Net Profit per Unit (Punjab):      PKR 1,090.40      ← PKR 3.10 less than non-Punjab
Margin % (Punjab):                       43.6%
```

A seller who only accounts for the headline 8% commission would calculate a margin of 47.6%
instead of 43.7% — a 3.9 percentage point error that compounds significantly at volume.
At 100 units per month, the miscalculation costs PKR 9,750 in unexpected deductions.

---

*This specification is the single source of truth for DropSmart.*
*All agents, all files, and all implementation decisions must conform to it.*
