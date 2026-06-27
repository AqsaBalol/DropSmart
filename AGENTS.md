# AGENTS.md — DropSmart

This file is the authoritative guide for every AI agent working on this codebase.
Read it in full before touching any file.

---

## Project Overview

**DropSmart** is a multi-agent e-commerce product intelligence system built with Google ADK.

### Problem It Solves

E-commerce sellers on Daraz Pakistan, Walmart USA, Amazon USA, and Etsy make costly listing
mistakes because they research products manually, miscalculate fees, and skip risk assessment.
They either lose money on thin margins or waste time on oversaturated products.

### What DropSmart Does

DropSmart automates the full pre-listing research workflow. A seller inputs a product idea and
target marketplace. Seven specialized agents then work in sequence — researching suppliers,
analyzing competitors, fetching live fee structures, calculating margins, assessing risk, and
generating a final verdict with a ready-to-use listing draft.

### Value

- Prevents margin mistakes by calculating every fee deduction line by line
- Surfaces competitor data from live marketplace listings — not stale databases
- Adapts to all three business models: Dropshipping, Fulfilled by Seller (FBS), Fulfilled by Marketplace (FBM)
- Enforces a Human-in-the-Loop checkpoint before generating any final report
- Covers four major marketplaces across two regions (Pakistan, USA)

---

## Tech Stack

| Component | Technology |
|---|---|
| Language | Python 3.11+ |
| Agent Framework | Google ADK (Agent Development Kit) |
| External Tool Protocol | MCP (Model Context Protocol) |
| Web Search API | Serper.dev API — dynamic web search only |
| AI Model | Gemini via Google ADK |
| Environment Variables | python-dotenv |
| Platform Configs | PyYAML — loaded from `platform_configs/` |

---

## Architecture

DropSmart uses a sequential multi-agent pipeline with one Human-in-the-Loop (HITL) checkpoint.
The Orchestrator is the single entry point. It collects user inputs, calls specialist agents in
order, enforces the HITL gate, and returns the final report.

```
User Input
    │
    ▼
Orchestrator Agent
    │
    ├──▶ Supplier Research Agent
    │
    ├──▶ Competitor Analysis Agent
    │
    ├──▶ Fee Structure Research Agent
    │
    ├──▶ Margin Calculator Agent
    │
    ├──▶ Risk Assessor Agent
    │
    ├──▶ [HITL CHECKPOINT — human must approve before continuing]
    │
    └──▶ Report Generator Agent
```

All external calls (web search, marketplace lookups) go through the MCP server only.
No agent calls external APIs directly.

---

## Agent Specifications

### 1. Orchestrator Agent
**File:** `agents/orchestrator.py`

Entry point for the entire system. Collects all required inputs from the user sequentially
before dispatching any specialist agent. Coordinates the pipeline, passes outputs between
agents, and enforces the HITL checkpoint before the Report Agent runs.

**Inputs collected from user:**
- Product name or idea
- Target marketplace (`daraz_pk`, `walmart_us`, `amazon_us`, `etsy_us`)
- Business model (`dropshipping`, `fbs`, `fbm`)
- Supplier cost per unit (optional at start — can be filled by Supplier Agent findings)
- Own costs: packaging, courier, any other seller-side costs

**Responsibilities:**
- Validate all user inputs before passing downstream
- Call each specialist agent in the correct sequence
- Aggregate all agent outputs into a single context object
- Present a summary to the human and pause at the HITL checkpoint
- Only call Report Generator Agent after receiving explicit human approval
- Never skip or bypass the HITL checkpoint under any circumstance

---

### 2. Supplier Research Agent
**File:** `agents/supplier_agent.py`

Finds real supplier options using web search via MCP. Adapts search strategy based on the
business model provided by the Orchestrator.

**Business model awareness:**
- **Dropshipping:** searches for suppliers offering direct-to-customer shipping, no MOQ,
  fast international shipping, and blind shipping or white-label options
- **FBS (Fulfilled by Seller):** searches for suppliers with low MOQ, reliable bulk pricing,
  and lead times compatible with seller-held inventory
- **FBM (Fulfilled by Marketplace):** searches for suppliers who can ship to marketplace
  fulfillment centers, with bulk packaging compliance and labeling requirements

**Output per supplier:**
- Supplier name and website
- Estimated unit cost range
- MOQ (minimum order quantity)
- Shipping time and method
- Business model compatibility rating
- Notes on reliability signals (years in business, reviews, certifications)

---

### 3. Competitor Analysis Agent
**File:** `agents/competitor_agent.py`

Searches live marketplace listings on the target platform. Extracts competitive landscape data
to help the seller understand what they are entering.

**Searches for and extracts:**
- Price range across top listings (min, median, max)
- Top sellers and their listing strategies
- Review counts and average ratings
- Monthly sales estimates where available
- Trend direction (growing / stable / declining)
- Seasonality signals (is this product seasonal?)
- High-volume keywords used in top-ranking titles and descriptions

**Platform-specific behavior:**
- Adapts search queries to each marketplace's naming conventions and category structure
- Uses marketplace-specific search signals defined in `platform_configs/`

---

### 4. Fee Structure Research Agent
**File:** `agents/fee_agent.py`

Dynamically searches and retrieves the current fee structure for the target marketplace and
business model combination. **Never hardcodes, assumes, or caches fee values.**
Fees change frequently and vary by product category — every run must fetch fresh data.

**Daraz Pakistan fees to find:**
- Commission rate (varies by category)
- VAT on commission
- Payment processing fee
- VAT on payment processing fee
- Handling fee
- VAT on handling fee

**Amazon USA (FBA) fees to find:**
- Referral fee (percentage, varies by category)
- FBA fulfillment fee (per unit, varies by size/weight)
- FBA storage fee (monthly, varies by season)

**Walmart USA fees to find:**
- Referral fee (varies by category)
- Fulfillment fee if using Walmart Fulfillment Services (WFS)

**Etsy USA fees to find:**
- Listing fee
- Transaction fee
- Payment processing fee
- Offsite ads fee (if applicable)

**Output:** A structured fee breakdown with every fee named, percentage or flat amount,
and the source URL where it was found. This output feeds directly into the Margin Calculator.

---

### 5. Margin Calculator Agent
**File:** `agents/margin_agent.py`

Takes supplier cost and all fees from the Fee Agent, adds the seller's own costs, and
produces a complete margin analysis. Every deduction must appear on its own line.
No black-box totals — the seller must be able to see exactly where money goes.

**Inputs:**
- Supplier cost per unit
- All fees from Fee Structure Research Agent (passed as structured data)
- Seller's packaging cost
- Seller's courier or shipping cost (for FBS/dropshipping)
- Target selling price (from competitor analysis or user input)

**Output — line by line:**
```
Selling Price:              PKR / USD [amount]
─────────────────────────────────────
- Supplier Cost:            [amount]
- Packaging Cost:           [amount]
- Courier / Shipping:       [amount]
- [Fee Name 1]:             [amount]
- [Fee Name 2]:             [amount]
- [Fee Name N]:             [amount]
─────────────────────────────────────
Net Profit per Unit:        [amount]
Margin %:                   [%]
Break-Even Price:           [amount]
─────────────────────────────────────
Monthly Projection (X units): [amount]
```

No fee may be grouped or hidden. Every item from the Fee Agent output appears as its own row.

---

### 6. Risk Assessor Agent
**File:** `agents/risk_agent.py`

Scores the product opportunity across six risk dimensions. Synthesizes data from all
preceding agents to produce an overall risk verdict.

**Six dimensions scored (each 1–10, where 10 = highest risk):**

| Dimension | What It Measures |
|---|---|
| Market Saturation | How crowded is this product category on the target marketplace |
| Margin Adequacy | Is the margin wide enough to survive price competition |
| Supplier Reliability | How dependable are the available suppliers |
| Competition Level | Strength of top competitors (reviews, ratings, market share) |
| Trend Direction | Is demand growing, stable, or declining |
| Seasonality Risk | Is this product highly seasonal — high risk of dead stock |

**Overall Risk Level:**
- **LOW** — safe to proceed, strong fundamentals
- **MEDIUM** — proceed with caution, specific risks identified
- **HIGH** — significant barriers or margin danger, not recommended without changes

**Output:** Score per dimension with one-line reasoning, overall risk level, and the top
three specific risks the seller should address.

---

### 7. Report Generator Agent
**File:** `agents/report_agent.py`

**This agent MUST NOT run without explicit human approval at the HITL checkpoint.**
The Orchestrator is responsible for enforcing this gate. The Report Agent itself must also
check that approval is present in the context before executing.

**Verdict options:**
- **Go** — strong opportunity, recommend proceeding
- **Proceed with Caution** — viable but specific conditions must be met
- **Do Not Proceed** — risk or margin analysis indicates this product is not viable

**Report contents:**
- Final verdict with clear reasoning tied to agent findings
- Recommended strategy (which business model, which supplier type, pricing approach)
- Listing draft optimized for the target marketplace:
  - Title within character limits for that platform
  - Bullet points or description using high-volume keywords from Competitor Agent
  - Tone and style matched to marketplace character (Etsy = handmade/artisan voice,
    Amazon = feature-dense, Daraz = local language cues where applicable)

**Platform character limits** are loaded from `platform_configs/` YAML files — never hardcoded.

---

## Hard Rules — Never Violate

These rules apply to every agent, every file, every function in this codebase.

1. **Never hardcode API keys or credentials** — all secrets load from `.env` via python-dotenv
2. **Fee structures must be searched dynamically every run** — never assume, cache, or hardcode fees
3. **HITL checkpoint is mandatory** — the Report Generator Agent must never run without explicit
   human approval; this gate must exist in code, not just in documentation
4. **Every agent must have a class docstring, method docstrings, and inline comments** on every
   logic block — no exceptions
5. **Every fee deduction must appear line by line** in Margin Calculator output — no grouped totals
6. **Platform configs load from YAML files** in `platform_configs/` — never from hardcoded values
   in agent logic
7. **All external calls go through the MCP server** — no agent calls Serper.dev or any external
   API directly
8. **Input validation is required** on all user-provided inputs before any agent processes them

---

## Security Rules

- No secrets in source code — `.env` only, and `.env` is gitignored
- `.env.example` documents all required keys with placeholder values only
- Input validation on all user inputs in the Orchestrator before passing downstream
- MCP server is the single point of contact for all external calls — centralizes audit logging
- All tool calls must be logged with timestamp, agent name, tool name, and query

---

## Platform Fee Awareness

Fee structures differ by marketplace, region, product category, and business model.
The Fee Structure Research Agent must account for all of these dimensions on every run.

| Marketplace | Region | Business Models Supported |
|---|---|---|
| Daraz | Pakistan | Dropshipping, FBS |
| Walmart | USA | FBS, FBM |
| Amazon | USA | Dropshipping, FBS, FBM (FBA) |
| Etsy | USA | Dropshipping, FBS |

Platform-specific metadata (character limits, fee search query templates, category structures)
lives in `platform_configs/` as YAML files — one file per marketplace.

---

## Coding Conventions

All code in this project must follow these conventions without exception.

**Type hints:** Every function signature must include type hints on all parameters and return type.

```python
def calculate_margin(selling_price: float, total_costs: float) -> float:
```

**Docstrings:** Google-style docstrings on every class and every function.

```python
def calculate_margin(selling_price: float, total_costs: float) -> float:
    """Calculates net margin as a percentage of selling price.

    Args:
        selling_price: The price at which the product will be listed.
        total_costs: Sum of all costs and fees per unit.

    Returns:
        Net margin as a float percentage (e.g., 23.5 for 23.5%).
    """
```

**Inline comments:** Every logic block must have a comment explaining why, not just what.

**Variable names:** Descriptive names always. No single-letter variables outside of loop indices.

**Function scope:** One function = one responsibility. If a function does two things, split it.

**Imports:** Standard library first, third-party second, local imports third. Separated by blank lines.

---

## File Structure Reference

```
dropsmart/
├── AGENTS.md                          # This file — read before touching anything
├── README.md                          # User-facing project documentation
├── .env                               # Secret keys — never committed
├── .env.example                       # Key names with placeholder values only
├── requirements.txt                   # All Python dependencies
├── main.py                            # Entry point — starts the Orchestrator
├── agents/
│   ├── __init__.py
│   ├── orchestrator.py                # Entry point agent, HITL enforcer
│   ├── supplier_agent.py              # Supplier search, business-model-aware
│   ├── competitor_agent.py            # Live marketplace competitor data
│   ├── fee_agent.py                   # Dynamic fee research — never hardcoded
│   ├── margin_agent.py                # Line-by-line margin calculator
│   ├── risk_agent.py                  # Six-dimension risk scoring
│   └── report_agent.py                # Final report — HITL-gated
├── mcp_server/
│   ├── __init__.py
│   └── search_mcp.py                  # MCP server — all external calls go here
├── platform_configs/
│   ├── daraz_pk.yaml                  # Daraz Pakistan metadata and config
│   ├── walmart_us.yaml                # Walmart USA metadata and config
│   ├── amazon_us.yaml                 # Amazon USA metadata and config
│   └── etsy_us.yaml                   # Etsy USA metadata and config
├── specs/
│   └── dropsmart_spec.md              # Detailed technical specification
├── .agent/
│   └── skills/
│       ├── supplier-research/SKILL.md
│       ├── competitor-analysis/SKILL.md
│       ├── fee-structure-research/SKILL.md
│       ├── margin-calculator/SKILL.md
│       ├── risk-assessor/SKILL.md
│       └── report-generator/SKILL.md
└── tests/
    └── __init__.py
```

---

## Agent Communication Pattern

Agents do not call each other directly. The Orchestrator manages all sequencing and passes
data between agents as structured Python dictionaries. Each agent receives a context dict,
does its work, and returns a results dict. The Orchestrator merges results into a growing
session context that is passed forward to subsequent agents.

This keeps agents decoupled and independently testable.

---

*Last updated: 2026-06-27*
