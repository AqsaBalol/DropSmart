# DropSmart — Multi-Agent E-Commerce Product Intelligence System

DropSmart tells a dropshipper or seller whether a product is worth listing — on Daraz Pakistan, Walmart USA, Amazon USA, or Etsy USA — **before** they spend money on it. It researches suppliers, checks live competitor pricing, finds the real marketplace fees, calculates true profit margin, scores six risk dimensions, and produces a Go / Proceed with Caution / Do Not Proceed verdict with a ready-to-use listing draft. A human approves the findings before any final report is generated.

Built for Kaggle's 5-Day AI Agents Intensive Vibe Coding Course with Google, submitted to the **Agents for Business** track.

---

## The Problem

New and small-scale sellers often pick products by gut feeling — a trending item on TikTok, a supplier's sales pitch — without knowing the real numbers behind it. Properly researching whether a single product is worth listing — checking real competitor prices, finding the actual marketplace fees, working out true margin after every deduction — routinely takes 2–3 hours per item when done manually. That's not a guess — it reflects hands-on e-commerce selling experience. Multiply that across the dozens of product ideas a seller considers before finding the one worth listing, and most of that time goes into items that turn out not to be worth pursuing at all. Marketplace fees are also opaque and change without notice, and supplier pricing is scattered across dozens of sites with no single place to compare them — so by the time a seller has pieced all of this together manually, they've often already bought inventory. DropSmart compresses that multi-hour research process into one pipeline run, so a seller finds out an item isn't worth it in minutes, not hours — before money or time gets sunk into it.

## How It Works

Seven specialist agents run in a fixed sequence, coordinated by a Python orchestrator, each one handing its findings to the next:

```
USER INPUT (product, marketplace, business model, costs)
        │
        ▼
   ORCHESTRATOR — validates input, manages session context, enforces HITL gate
        │
        ├─ 1. Supplier Research Agent      → supplier options, cost, MOQ, shipping
        ├─ 2. Competitor Analysis Agent    → live pricing, reviews, keywords, trend
        ├─ 3. Fee Structure Research Agent → named marketplace fees, source-backed
        ├─ 4. Margin Calculator Agent      → line-by-line deduction table, margin %
        ├─ 5. Risk Assessor Agent          → 6-dimension risk scorecard
        │
        ▼
   ⚠ HITL CHECKPOINT — full summary shown, seller must approve before continuing
        │ (reject → pipeline stops here, no report generated)
        ▼
        └─ 6. Report Generator Agent       → verdict, strategy, listing draft
        │
        ▼
   FINAL REPORT (saved to reports/, also printed to console)
```

All external research goes through a single MCP server (`mcp_server/search_mcp.py`) — every agent that needs live data calls through it rather than making its own HTTP requests, and every call is logged to `security.log` for audit.

Full technical specification, including BDD scenarios and the platform config schema: [`specs/dropsmart_spec.md`](specs/dropsmart_spec.md).

---

## Course Concepts Demonstrated

| Concept | Where |
|---|---|
| **Multi-agent system** | `agents/` — seven specialist agents (`supplier_agent.py`, `competitor_agent.py`, `fee_agent.py`, `margin_agent.py`, `risk_agent.py`, `report_agent.py`) coordinated by `orchestrator.py`. Built as a custom Python orchestration layer calling the Gemini API directly (`google-genai`), not on the `google-adk` framework. |
| **MCP Server** | `mcp_server/search_mcp.py` — a FastMCP server exposing search tools (`web_search`, `search_marketplace_fees`, `search_supplier_prices`, `search_competitor_listings`, `search_competitor_listings_live`) as the single point of contact for all external Serper.dev API calls. |
| **Security features** | `tests/test_security.py` (28 tests) + implementation in `search_mcp.py`: API-key redaction before any log write, a sliding-window rate limiter, and input validation in `orchestrator.validate_input()` that runs before any agent or external API is touched. Informed by the course's "Write Secure AI Code: Automated Threat Scans, Safety Guards, and Security Testing" codelab. |
| **Agent Skills** | `.agent/skills/` — six SKILL.md files (`supplier-research`, `competitor-analysis`, `fee-structure-research`, `margin-calculator`, `risk-assessor`, `report-generator`) defining each agent's trigger conditions, workflow, and output contract. |

---

## Setup

**Requirements:** Python 3.11+, a [Google AI Studio](https://aistudio.google.com/) API key, a [Serper.dev](https://serper.dev/) API key (free tier is enough for testing).

```bash
git clone https://github.com/AqsaBalol/DropSmart.git
cd DropSmart
pip install -r requirements.txt
```

Create a `.env` file in the project root:

```
GEMINI_API_KEY=your_google_ai_studio_key_here
SERPER_API_KEY=your_serper_key_here
```

Run it:

```bash
python main.py
```

You'll be prompted for a product name, marketplace, business model, and any relevant costs. The pipeline runs through all five research/analysis steps, shows you a pre-report summary, and waits for your approval (`yes`/`no`) before generating the final report.

---

## Running the Tests

```bash
pip install pytest --break-system-packages   # if not already installed
pytest tests/test_security.py -v
```

28 tests covering API key handling, input validation boundaries, rate-limit enforcement, and security-log redaction. Two tests are intentionally named `*_KNOWN_GAP_*` — they document real, current permissiveness in the validation logic rather than hide it (see below). A test suite that only shows passing checks isn't a security evaluation.

---

## Resilience: Gemini Model Fallback Chain

Every agent call goes through a 3-model fallback chain in `base_agent.py`: `gemini-2.5-flash-lite` → `gemini-2.5-flash` → `gemini-2.5-pro`. If a call fails with a transient error (503 / UNAVAILABLE / RESOURCE_EXHAUSTED — Google's servers being momentarily overloaded), the agent waits 2 seconds and retries on the next model in the chain, logging which model actually served the request. This isn't theoretical — it fired correctly during real pipeline runs while building this project, with `gemini-2.5-flash-lite` returning a 503 and the pipeline recovering automatically on `gemini-2.5-flash` without any manual intervention or lost session state. Non-transient errors (auth failures, malformed requests) are not retried and fail immediately, since retrying those would just waste time on an error that won't resolve itself.

---

## Known Limitations

The multi-agent orchestration, margin calculation, risk scoring, and HITL approval flow are complete and working end to end — what's incomplete is the raw data feeding two of the seven agents. Serper's search snippets don't contain a structured price field for JavaScript-rendered supplier pages, so Supplier and Fee agents sometimes have no number to extract. This isn't an architectural gap: the pipeline already handles it correctly by flagging it (`supplier_cost_is_assumed`, `fees_incomplete`) rather than silently producing a wrong number.

- Supplier price/MOQ/shipping is frequently unavailable because supplier pages render pricing via JavaScript, which doesn't appear in search snippets.
- Marketplace fee data is sometimes incomplete for the same underlying reason.
- The Fee Research Agent implements a subset of the five risk-mitigation rules described in its own spec (`.agent/skills/fee-structure-research/SKILL.md`, `specs/dropsmart_spec.md` Section 7): missing-fee detection is implemented; multi-source conflict resolution, date-freshness checking, and automatic retry-on-missing are specified but not yet coded.
- `orchestrator.validate_input()` requires a non-empty `province` string for Daraz listings but does not check it's one of the four real provinces. The CLI (`main.py`) masks this with a fixed 1–4 menu, but the validation function itself would accept any non-empty string if called from outside that menu.
- The security-log redaction pattern (`^[A-Za-z0-9_\-]{20,}\$`) is intentionally broad — it will also redact a legitimate long product SKU or identifier, not just real API keys. This is a conscious tradeoff: favoring over-redaction of the audit log over any risk of a real key leaking.
- Competitor price data occasionally contains outliers from snippet misparsing (e.g. an implausible unit price far outside the competitor price range for the same product) — there is currently no plausibility bound rejecting obviously-wrong extracted values.

---

## Future Improvements

The clear next step is swapping generic web search for each marketplace's own structured data source. For Amazon, the SP-API's `Product Fees v0` endpoint returns exact estimated fees for a product directly — and as of May 2026, Amazon cancelled its planned SP-API usage fees, so this is currently free for a seller's own account. For Daraz, the Daraz Open Platform API exposes transaction/fee data directly, backed by Daraz University's published Marketplace Commission Structure table as a fallback reference. Walmart is the weakest fit of the three — its Marketplace API is built for managing listings you're already approved to sell, not for pre-listing fee lookup, so it helps less here than the other two. All three require becoming an approved, credentialed seller on that specific platform — a real onboarding step, not a same-day swap, and this is already on the paid Google AI Studio tier, so the gap is about what the search tool returns, not what Gemini can do with it. This same pattern isn't limited to these four marketplaces: because every marketplace-specific detail already lives in its own file under `platform_configs/`, extending DropSmart to any other e-commerce platform is a matter of adding that platform's config and real API credentials, not redesigning the pipeline.

---

## Project Structure

```
dropsmart/
├── agents/
│   ├── base_agent.py        # shared Gemini client, model-fallback chain, logging
│   ├── orchestrator.py      # pipeline coordinator, input validation, HITL gate
│   ├── supplier_agent.py
│   ├── competitor_agent.py
│   ├── fee_agent.py
│   ├── margin_agent.py
│   ├── risk_agent.py
│   └── report_agent.py
├── mcp_server/
│   └── search_mcp.py        # single point of contact for all external API calls
├── platform_configs/        # per-marketplace YAML: fee categories, listing constraints
├── .agent/skills/           # six SKILL.md agent-skill definitions
├── specs/
│   └── dropsmart_spec.md    # full technical spec, BDD scenarios, architecture diagram
├── tests/
│   └── test_security.py
├── reports/                 # generated JSON reports, one per pipeline run
├── main.py                  # CLI entry point
└── requirements.txt
```

---

## Author

**Aqsa Ismail Balol** — BS Computer Science (2017–2021), ACCP Information Systems Management (Aptech Pakistan, 2018), Google Digital Marketing & E-commerce Professional Certificate (2025). 3 years of experience in E-commerce. Built for Kaggle's 5-Day AI Agents Intensive Vibe Coding Course with Google, capstone submission.