"""Report Generator Agent for DropSmart.

Runs ONLY after the human approves at the HITL checkpoint. Reads the complete
session context assembled by all six earlier agents and produces a final,
seller-facing intelligence report — including an SEO listing draft, margin
narrative, risk narrative, and executive summary.

One Gemini call generates all text fields in a single structured JSON response.
If Gemini fails, a deterministic fallback assembles the report directly from
context values so the pipeline never returns empty-handed.

The report is also saved as a JSON file under the reports/ directory so it
can be reviewed, shared, or loaded into a front-end without re-running the
pipeline.
"""

# --- Standard library ---
import json
import logging
import os
import re
from datetime import datetime
from typing import Any

# --- Local ---
from agents.base_agent import BaseAgent


# ---------------------------------------------------------------------------
# Keys that Gemini must populate (used for validation after JSON parse)
# ---------------------------------------------------------------------------

_GEMINI_TEXT_FIELDS: frozenset[str] = frozenset({
    "executive_summary",
    "supplier_recommendation",
    "competitor_insights",
    "fee_breakdown_text",
    "margin_summary_text",
    "risk_summary_text",
    "listing_title_draft",
    "listing_description_draft",
})

# Maximum characters allowed in a listing title (most marketplaces cap at ~200)
_MAX_TITLE_CHARS: int = 200

# Reports directory name relative to the project root
_REPORTS_DIR: str = "reports"


class ReportAgent(BaseAgent):
    """Generates the final seller-facing intelligence report.

    Runs after HITL approval. Reads the complete session context and:
    1. Calls Gemini once with a comprehensive prompt to generate all narrative
       text fields as a single JSON object.
    2. Merges those text fields with deterministic values copied from context.
    3. Falls back to a context-only report if Gemini fails — never crashes.
    4. Saves the final report dict to ``reports/dropsmart_{product}_{date}.json``.

    Output contract — ``report_result`` contains:
    - ``product_name``: str — from context
    - ``marketplace``: str — from context
    - ``go_no_go_verdict``: str — ``"GO"`` / ``"CAUTION"`` / ``"NO-GO"``
    - ``executive_summary``: str — 2–3 sentences (Gemini)
    - ``supplier_recommendation``: str — (Gemini)
    - ``competitor_insights``: str — (Gemini)
    - ``fee_breakdown_text``: str — all fees listed (Gemini)
    - ``margin_summary_text``: str — (Gemini)
    - ``risk_summary_text``: str — (Gemini)
    - ``listing_title_draft``: str — SEO-optimised, uses top_keywords (Gemini)
    - ``listing_description_draft``: str — 3–4 sentences (Gemini)
    - ``verdict_reasoning``: list[str] — 3–5 emoji-prefixed reasoning points (Gemini)
    - ``recommended_strategy``: list[str] — 3–4 actionable strategy bullets (Gemini)
    - ``listing_bullets``: list[str] — exactly 5 benefit-focused listing bullets (Gemini)
    - ``recommended_selling_price``: float — avg_market_price from competitor_result
    - ``calculation_breakdown``: list[str] — copied from margin_result
    - ``generated_at``: str — ISO 8601 datetime of report generation
    - ``report_format``: str — always ``"text"``
    - ``report_filepath``: str — absolute path of the saved JSON file
    - ``formatted_report``: str — complete formatted text block (all 8 sections)
    """

    def __init__(self) -> None:
        """Initialises the ReportAgent with its fixed agent name."""
        super().__init__("report_agent")

    # ------------------------------------------------------------------
    # Public pipeline interface
    # ------------------------------------------------------------------

    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """Generates and saves the final intelligence report.

        This method is only called after the HITL checkpoint is approved.
        It builds a single Gemini prompt from the full session context,
        parses the response, and combines Gemini's narrative text with
        deterministic values copied directly from context.

        Args:
            context: The complete session context after all six earlier agents
                have run and the human has approved the HITL checkpoint.
                Must contain at minimum: ``product_name``, ``marketplace``,
                ``supplier_result``, ``competitor_result``, ``fee_result``,
                ``margin_result``, ``risk_result``.

        Returns:
            A dict with a single key ``"report_result"`` whose value is the
            full report dict described in the class docstring.
        """
        self._log_start("report generation")

        product_name: str = context.get("product_name", "Unknown Product")
        marketplace: str = context.get("marketplace", "unknown")

        # --- Attempt Gemini-generated narrative fields ---
        gemini_fields: dict[str, Any] = {}
        try:
            prompt: str = self._build_report_prompt(context)
            raw_response: str = self._safe_generate(prompt)
            gemini_fields = self._parse_report_response(raw_response)
        except Exception as exc:
            # Log but do not re-raise — fallback handles this case
            self._logger.error(
                "[report_agent] Gemini call failed, using fallback report: %s", exc
            )

        if not gemini_fields:
            self._logger.warning(
                "[report_agent] Gemini response was empty or unparseable — "
                "building fallback report from context."
            )
            gemini_fields = self._build_fallback_report(context)

        # --- Deterministic fields assembled directly from context ---
        risk_result: dict[str, Any] = context.get("risk_result", {})
        margin_result: dict[str, Any] = context.get("margin_result", {})
        competitor_result: dict[str, Any] = context.get("competitor_result", {})

        go_no_go_verdict: str = risk_result.get("go_no_go_signal", "CAUTION")
        recommended_selling_price: float = float(
            competitor_result.get("avg_market_price", 0.0)
        )
        calculation_breakdown: list[str] = margin_result.get(
            "calculation_breakdown", []
        )
        generated_at: str = datetime.now().isoformat()

        # --- Assemble the final report dict ---
        report: dict[str, Any] = {
            "product_name": product_name,
            "marketplace": marketplace,
            "go_no_go_verdict": go_no_go_verdict,
            # Narrative text fields: Gemini-generated or from fallback
            "executive_summary": gemini_fields.get("executive_summary", ""),
            "supplier_recommendation": gemini_fields.get("supplier_recommendation", ""),
            "competitor_insights": gemini_fields.get("competitor_insights", ""),
            "fee_breakdown_text": gemini_fields.get("fee_breakdown_text", ""),
            "margin_summary_text": gemini_fields.get("margin_summary_text", ""),
            "risk_summary_text": gemini_fields.get("risk_summary_text", ""),
            "listing_title_draft": gemini_fields.get("listing_title_draft", ""),
            "listing_description_draft": gemini_fields.get(
                "listing_description_draft", ""
            ),
            # New narrative list fields (Gemini-generated or fallback)
            "verdict_reasoning": gemini_fields.get("verdict_reasoning", []),
            "recommended_strategy": gemini_fields.get("recommended_strategy", []),
            "listing_bullets": gemini_fields.get("listing_bullets", []),
            # Deterministic fields copied from earlier agent outputs
            "recommended_selling_price": recommended_selling_price,
            "calculation_breakdown": calculation_breakdown,
            "generated_at": generated_at,
            "report_format": "text",
        }

        # --- Persist to disk ---
        filepath: str = self._save_report_to_file(report, context)
        report["report_filepath"] = filepath

        # Formatted text report assembled after file save so the JSON stays compact
        report["formatted_report"] = self._render_formatted_report(
            context, gemini_fields
        )

        self._log_end("report generation", success=True)

        return {"report_result": report}

    # ------------------------------------------------------------------
    # Gemini prompt builder
    # ------------------------------------------------------------------

    def _build_report_prompt(self, context: dict[str, Any]) -> str:
        """Builds the single comprehensive prompt sent to Gemini.

        Summarises all key findings from the session context into a compact,
        structured prompt that instructs Gemini to return ONLY valid JSON —
        no markdown, no prose outside the JSON object. Every number in the
        prompt comes directly from context; Gemini is told not to invent data.

        Args:
            context: The full session context.

        Returns:
            The complete prompt string ready to pass to ``_safe_generate``.
        """
        product_name: str = context.get("product_name", "Unknown Product")
        marketplace: str = context.get("marketplace", "unknown")
        business_model: str = context.get(
            "business_model_alias", context.get("business_model", "fbs")
        )

        # --- Supplier data ---
        supplier_result: dict = context.get("supplier_result", {})
        suppliers: list[dict] = supplier_result.get("suppliers", [])
        recommended_supplier: str = supplier_result.get(
            "recommended_supplier", "Unknown"
        )
        # Compact supplier table — at most 3 rows to keep the prompt concise
        supplier_rows: str = "\n".join(
            f"  - {s.get('name', 'N/A')}: "
            f"{s.get('price_usd', s.get('price_pkr', 'N/A'))} {context.get('fee_result', {}).get('currency', 'USD')}, "
            f"MOQ {s.get('moq', 'N/A')}, "
            f"reliability {s.get('reliability_score', 'N/A')}/10"
            for s in suppliers[:3]
        ) or "  No supplier data available."

        # --- Competitor data ---
        competitor_result: dict = context.get("competitor_result", {})
        avg_price: float = competitor_result.get("avg_market_price", 0.0)
        price_range: dict = competitor_result.get("price_range", {})
        # top_keywords is a list of {keyword, volume_signal} dicts (CompetitorAgent v2)
        # or a legacy list of plain strings — handle both formats safely.
        top_keywords_raw: list = competitor_result.get("top_keywords", [])
        market_saturation: str = competitor_result.get("market_saturation", "unknown")
        # Pass at most 5 keywords — more than that dilutes the title instruction
        keywords_str: str = ", ".join(
            k.get("keyword", str(k)) if isinstance(k, dict) else str(k)
            for k in top_keywords_raw[:5]
        ) or "none available"

        # --- Fee data ---
        fee_result: dict = context.get("fee_result", {})
        commission_pct: float = fee_result.get("commission_pct", 0.0)
        payment_pct: float = fee_result.get("payment_processing_pct", 0.0)
        handling_fee: float = fee_result.get("handling_fee", 0.0)
        vat_commission: float = fee_result.get("vat_on_commission_pct", 0.0)
        total_fee_pct: float = fee_result.get("total_fee_pct", 0.0)
        currency: str = fee_result.get("currency", "USD")
        missing_fees: bool = fee_result.get("missing_fees_detected", False)

        # --- Margin data ---
        margin_result: dict = context.get("margin_result", {})
        margin_pct: float = margin_result.get("margin_pct", 0.0)
        net_profit: float = margin_result.get("net_profit_per_unit", 0.0)
        selling_price: float = margin_result.get("selling_price", avg_price)
        supplier_cost: float = margin_result.get("supplier_cost", 0.0)
        total_deductions: float = margin_result.get("total_deductions", 0.0)
        break_even: float = margin_result.get("break_even_price", 0.0)

        # --- Risk data ---
        risk_result: dict = context.get("risk_result", {})
        risk_level: str = risk_result.get("overall_risk_level", "unknown")
        risk_score: int = risk_result.get("risk_score", 0)
        go_no_go: str = risk_result.get("go_no_go_signal", "CAUTION")
        top_risks: list[str] = risk_result.get("top_risks", [])
        top_risks_str: str = (
            "\n".join(f"  - {r}" for r in top_risks) or "  None."
        )

        prompt: str = (
            "You are a senior e-commerce product analyst writing a seller intelligence report.\n\n"
            "IMPORTANT RULES:\n"
            "1. Return ONLY valid JSON — no markdown fences, no explanatory prose, no trailing commas.\n"
            "2. Never invent data not present in the context below.\n"
            "3. All numbers cited must come verbatim from the context.\n"
            "4. Keep each text field within the length limit specified.\n\n"
            "=== CONTEXT ===\n"
            f"Product: {product_name}\n"
            f"Marketplace: {marketplace}\n"
            f"Business Model: {business_model}\n"
            f"Currency: {currency}\n\n"
            "--- SUPPLIERS ---\n"
            f"Recommended: {recommended_supplier}\n"
            f"Options:\n{supplier_rows}\n\n"
            "--- COMPETITORS ---\n"
            f"Average Market Price: {avg_price} {currency}\n"
            f"Price Range: {price_range.get('min', 'N/A')} – {price_range.get('max', 'N/A')} {currency}\n"
            f"Market Saturation: {market_saturation}\n"
            f"Top SEO Keywords (use ALL of these in the listing title): {keywords_str}\n\n"
            "--- FEES ---\n"
            f"Commission: {commission_pct}%\n"
            f"Payment Processing: {payment_pct}%\n"
            f"Handling Fee: {handling_fee} {currency}\n"
            f"VAT on Commission: {vat_commission}%\n"
            f"Total Fee %: {total_fee_pct}%\n"
            f"Missing Fees Flagged: {missing_fees}\n\n"
            "--- MARGIN ---\n"
            f"Selling Price: {selling_price} {currency}\n"
            f"Supplier Cost: {supplier_cost} {currency}\n"
            f"Total Deductions: {total_deductions} {currency}\n"
            f"Net Profit per Unit: {net_profit} {currency}\n"
            f"Margin %: {margin_pct}%\n"
            f"Break-Even Price: {break_even} {currency}\n\n"
            "--- RISK ---\n"
            f"Overall Risk Level: {risk_level}\n"
            f"Risk Score: {risk_score}/100\n"
            f"Go/No-Go Signal: {go_no_go}\n"
            f"Top Risks:\n{top_risks_str}\n\n"
            "=== OUTPUT FORMAT ===\n"
            "Return this exact JSON structure with all fields populated:\n\n"
            "{\n"
            '  "executive_summary": "<2-3 sentence overview for the seller: opportunity, margin, and risk>",\n'
            '  "supplier_recommendation": "<1-2 sentences: which supplier to use and why, citing reliability and price>",\n'
            '  "competitor_insights": "<2-3 sentences: competition level, pricing landscape, and positioning advice>",\n'
            '  "fee_breakdown_text": "<prose summary of all fee components listed above, each named and valued>",\n'
            '  "margin_summary_text": "<2 sentences: net profit per unit, margin %, and break-even price>",\n'
            '  "risk_summary_text": "<2-3 sentences: overall risk level, top risks, what to watch>",\n'
            f'  "listing_title_draft": "<SEO-optimised title under {_MAX_TITLE_CHARS} characters incorporating these keywords: {keywords_str}>",\n'
            '  "listing_description_draft": "<3-4 sentence product description that highlights key features and targets buyer intent>",\n'
            '  "verdict_reasoning": ["✅ <positive point backed by data above>", "⚠️ <caution point backed by data above>"],\n'
            '  "recommended_strategy": ["<actionable step 1>", "<actionable step 2>", "<actionable step 3>"],\n'
            '  "listing_bullets": ["<benefit 1, under 100 chars>", "<benefit 2>", "<benefit 3>", "<benefit 4>", "<benefit 5>"]\n'
            "}\n\n"
            "Additional rules for the three new list fields:\n"
            "- verdict_reasoning: 3-5 strings. Prefix ✅ for positives, ⚠️ for cautions/risks, ❌ for blockers. "
            "Base ONLY on the data provided above — do not invent reasons.\n"
            "- recommended_strategy: 3-4 short actionable bullet strings. "
            "Derive from risk level, margin thinness, and saturation signals above. No emoji prefix.\n"
            f"- listing_bullets: EXACTLY 5 strings, each under 100 characters, benefit-focused. "
            f"Naturally incorporate relevant keywords from: {keywords_str}."
        )

        return prompt

    # ------------------------------------------------------------------
    # Gemini response parser
    # ------------------------------------------------------------------

    def _parse_report_response(self, raw: str) -> dict[str, Any]:
        """Strips markdown fences and parses the JSON returned by Gemini.

        Gemini sometimes wraps JSON in triple-backtick fences (```json...```).
        This method strips those before parsing so valid JSON inside fences is
        not rejected by the standard library parser.

        Args:
            raw: The raw string returned by ``_safe_generate``.

        Returns:
            Parsed dict containing the text fields, or ``{}`` if parsing fails.
        """
        if not raw or not raw.strip():
            self._logger.warning("[report_agent] Received empty response from Gemini.")
            return {}

        cleaned: str = raw.strip()

        # Strip optional opening fence (```json or just ```)
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        # Strip optional closing fence
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = cleaned.strip()

        try:
            parsed: Any = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            self._logger.error(
                "[report_agent] JSON parse failed: %s — raw snippet: %.200s",
                exc,
                cleaned,
            )
            return {}

        if not isinstance(parsed, dict):
            self._logger.error(
                "[report_agent] Gemini returned JSON but not an object (got %s).",
                type(parsed).__name__,
            )
            return {}

        # Log any expected text fields that came back empty or missing
        missing: list[str] = [
            field for field in _GEMINI_TEXT_FIELDS if not parsed.get(field)
        ]
        if missing:
            self._logger.warning(
                "[report_agent] Gemini response missing fields: %s", missing
            )

        return parsed

    # ------------------------------------------------------------------
    # Fallback report builder
    # ------------------------------------------------------------------

    def _build_fallback_report(self, context: dict[str, Any]) -> dict[str, Any]:
        """Builds a report from context data when Gemini is unavailable.

        Uses only .get() with safe defaults throughout so this method never
        raises regardless of what is or is not present in context. The result
        is accurate (no invented data) though less polished than a Gemini
        response.

        Args:
            context: The full session context.

        Returns:
            A dict with all ``_GEMINI_TEXT_FIELDS`` populated from context.
        """
        product_name: str = context.get("product_name", "this product")
        marketplace: str = context.get("marketplace", "the marketplace")

        # --- Supplier ---
        supplier_result: dict = context.get("supplier_result", {})
        recommended_supplier: str = supplier_result.get(
            "recommended_supplier", "No supplier found"
        )
        suppliers: list[dict] = supplier_result.get("suppliers", [])
        # Find the recommended supplier's reliability score for the narrative
        rec_reliability: int = 5
        for s in suppliers:
            if s.get("name") == recommended_supplier:
                rec_reliability = int(s.get("reliability_score", 5))
                break

        # --- Competitor ---
        competitor_result: dict = context.get("competitor_result", {})
        avg_price: float = competitor_result.get("avg_market_price", 0.0)
        market_saturation: str = competitor_result.get("market_saturation", "unknown")
        # top_keywords may be a list of dicts or legacy strings — handle both
        top_keywords_raw: list = competitor_result.get("top_keywords", [])
        keywords_str: str = ", ".join(
            k.get("keyword", str(k)) if isinstance(k, dict) else str(k)
            for k in top_keywords_raw[:5]
        )

        # --- Fees ---
        fee_result: dict = context.get("fee_result", {})
        commission_pct: float = fee_result.get("commission_pct", 0.0)
        payment_pct: float = fee_result.get("payment_processing_pct", 0.0)
        handling_fee: float = fee_result.get("handling_fee", 0.0)
        currency: str = fee_result.get("currency", "USD")
        total_fee_pct: float = fee_result.get("total_fee_pct", 0.0)
        missing_fees: bool = fee_result.get("missing_fees_detected", False)

        # --- Margin ---
        margin_result: dict = context.get("margin_result", {})
        margin_pct: float = margin_result.get("margin_pct", 0.0)
        net_profit: float = margin_result.get("net_profit_per_unit", 0.0)
        break_even: float = margin_result.get("break_even_price", 0.0)

        # --- Risk ---
        risk_result: dict = context.get("risk_result", {})
        risk_level: str = risk_result.get("overall_risk_level", "unknown")
        risk_score: int = risk_result.get("risk_score", 0)
        go_no_go: str = risk_result.get("go_no_go_signal", "CAUTION")
        top_risks: list[str] = risk_result.get("top_risks", [])
        top_risk_str: str = top_risks[0] if top_risks else "No major risks identified."

        # Build listing title from keywords — join the top 5 separated by spaces
        if keywords_str:
            listing_title: str = f"{product_name} | {keywords_str}"
        else:
            listing_title = product_name
        listing_title = listing_title[:_MAX_TITLE_CHARS]

        missing_fees_note: str = (
            " Note: some fee components may be missing — verify before listing."
            if missing_fees
            else ""
        )

        keywords_line: str = f"Key search terms: {keywords_str}." if keywords_str else ""

        return {
            "executive_summary": (
                f"{product_name} on {marketplace} shows a net margin of {margin_pct:.1f}% "
                f"with an overall risk level of {risk_level} (score {risk_score}/100). "
                f"The go/no-go signal is {go_no_go}."
            ),
            "supplier_recommendation": (
                f"Recommended supplier: {recommended_supplier} "
                f"(reliability {rec_reliability}/10). "
                "Verify MOQ and shipping lead time before placing the first order."
            ),
            "competitor_insights": (
                f"The {marketplace} market for {product_name} shows {market_saturation} saturation "
                f"with an average price of {avg_price:.2f} {currency}. "
                "Optimising the listing with the top keywords will be essential for discoverability."
            ),
            "fee_breakdown_text": (
                f"Platform fees on {marketplace}: "
                f"Commission {commission_pct:.2f}%, "
                f"Payment Processing {payment_pct:.2f}%, "
                f"Handling Fee {handling_fee:.2f} {currency}. "
                f"Total fee load: {total_fee_pct:.2f}%.{missing_fees_note}"
            ),
            "margin_summary_text": (
                f"Net profit per unit is {net_profit:.2f} {currency} "
                f"({margin_pct:.1f}% margin). "
                f"Break-even price is {break_even:.2f} {currency}."
            ),
            "risk_summary_text": (
                f"Overall risk: {risk_level} (score {risk_score}/100) — {go_no_go}. "
                f"Primary concern: {top_risk_str}"
            ),
            "listing_title_draft": listing_title,
            "listing_description_draft": (
                f"Introducing {product_name}, now available on {marketplace}. "
                f"Competitively priced at {avg_price:.2f} {currency} "
                f"in a {market_saturation}-saturation market. "
                f"{keywords_line} "
                "Order today for fast, reliable delivery."
            ).strip(),
            # New list fields — minimal fallback values used when Gemini is unavailable
            "verdict_reasoning": (
                [f"⚠️ {r}" for r in top_risks[:5]]
                or ["⚠️ Full risk assessment not available — review data manually."]
            ),
            "recommended_strategy": [
                "Review the full risk and margin data before proceeding."
            ],
            "listing_bullets": [
                f"{product_name} — quality product, competitively priced."
            ],
        }

    # ------------------------------------------------------------------
    # Report file saver
    # ------------------------------------------------------------------

    def _save_report_to_file(
        self, report: dict[str, Any], context: dict[str, Any]
    ) -> str:
        """Saves the report dict as a JSON file in the reports/ directory.

        Creates the ``reports/`` folder if it does not exist. The filename is
        ``dropsmart_{product}_{date}.json`` where ``{product}`` is sanitised
        (only alphanumerics and underscores) and ``{date}`` is YYYY-MM-DD.

        Does not raise on I/O failure — logs the error and returns ``""`` so
        a disk problem never crashes the pipeline or blocks the caller.

        Args:
            report: The fully assembled report dict to serialise.
            context: Session context — used for the product name in the filename.

        Returns:
            Absolute path of the saved file, or ``""`` if saving failed.
        """
        product_name: str = context.get("product_name", "product")

        # --- Sanitise product name for use in a filename ---
        # Replace any non-alphanumeric, non-hyphen character with an underscore,
        # collapse runs of underscores, lower-case the result, trim edges.
        safe_product: str = re.sub(r"[^\w\s-]", "_", product_name)
        safe_product = re.sub(r"[\s]+", "_", safe_product)
        safe_product = re.sub(r"_+", "_", safe_product).strip("_").lower()
        # Truncate to keep the full path well under Windows MAX_PATH (260 chars)
        safe_product = safe_product[:60]

        date_str: str = datetime.now().strftime("%Y-%m-%d")
        filename: str = f"dropsmart_{safe_product}_{date_str}.json"

        # Resolve reports/ relative to the current working directory so it
        # always appears at the project root regardless of how the script is run.
        reports_dir: str = os.path.join(os.getcwd(), _REPORTS_DIR)

        try:
            os.makedirs(reports_dir, exist_ok=True)
        except OSError as exc:
            self._logger.error(
                "[report_agent] Could not create reports/ directory: %s", exc
            )
            return ""

        filepath: str = os.path.join(reports_dir, filename)

        try:
            with open(filepath, "w", encoding="utf-8") as fh:
                json.dump(report, fh, indent=2, ensure_ascii=False)
            self._logger.info("[report_agent] Report saved to: %s", filepath)
        except OSError as exc:
            self._logger.error(
                "[report_agent] Failed to save report to %s: %s", filepath, exc
            )
            return ""

        return filepath

    # ------------------------------------------------------------------
    # Label and symbol helpers (local copies — no import from MarginAgent)
    # ------------------------------------------------------------------

    def _get_currency_symbol(self, context: dict[str, Any]) -> str:
        """Returns the display currency symbol for the target marketplace.

        Mirrors ``MarginAgent._get_currency_symbol`` — duplicated here so
        ``ReportAgent`` has no dependency on another agent module.

        Args:
            context: Session context. Reads ``"marketplace"`` key.

        Returns:
            ``"PKR "`` (with trailing space) for ``daraz_pk``,
            ``"$"`` (no space) for all other marketplaces.
        """
        marketplace: str = context.get("marketplace", "").strip()
        return "PKR " if marketplace == "daraz_pk" else "$"

    @staticmethod
    def _get_marketplace_label(marketplace: str) -> str:
        """Maps a marketplace code to a human-readable display name.

        Args:
            marketplace: Internal marketplace code from session context,
                e.g. ``"daraz_pk"``.

        Returns:
            Display name such as ``"Daraz Pakistan"``, or the raw code
            as a fallback when the code is not recognised.
        """
        labels: dict[str, str] = {
            "daraz_pk":   "Daraz Pakistan",
            "walmart_us": "Walmart USA",
            "amazon_us":  "Amazon USA",
            "etsy_us":    "Etsy USA",
        }
        return labels.get(marketplace, marketplace)

    @staticmethod
    def _get_business_model_label(business_model: str) -> str:
        """Maps a business model code to a human-readable display name.

        Args:
            business_model: Internal business model code from session context,
                e.g. ``"dropshipping"``.

        Returns:
            Display name such as ``"Dropshipping"``, or the raw code as a
            fallback when the code is not recognised.
        """
        labels: dict[str, str] = {
            "dropshipping":              "Dropshipping",
            "fulfilled_by_seller":       "Fulfilled by Seller (FBS)",
            "fulfilled_by_marketplace":  "Fulfilled by Marketplace (FBM/FBA)",
        }
        return labels.get(business_model, business_model)

    # ------------------------------------------------------------------
    # Formatted report renderer
    # ------------------------------------------------------------------

    def _render_formatted_report(
        self,
        context: dict[str, Any],
        report_data: dict[str, Any],
    ) -> str:
        """Assembles all pipeline data into a single formatted text report.

        Reads supplier, competitor, margin, and risk results from ``context``
        and Gemini/fallback narrative fields from ``report_data``, then
        produces a human-readable multi-section text block using box-drawing
        characters.  All data access uses ``.get()`` with safe defaults so
        this method never raises regardless of what is present in context.

        Args:
            context: Complete session context after all agents have run.
                Keys read: ``product_name``, ``marketplace``,
                ``business_model`` / ``business_model_alias``,
                ``supplier_result``, ``competitor_result``,
                ``margin_result``, ``risk_result``.
            report_data: The Gemini-generated (or fallback) narrative dict.
                Keys read: ``verdict_reasoning``, ``recommended_strategy``,
                ``listing_bullets``, ``listing_title_draft``.

        Returns:
            A formatted multi-line string covering 8 sections:
            supplier intelligence, competitor analysis, trends & seasonality,
            margin calculation, risk assessment, final verdict, and listing
            draft.  Each section is delimited by ``━`` separator lines.
        """
        SEP: str = "━" * 43
        lines: list[str] = []

        def _section(title: str) -> None:
            lines.extend(["", SEP, title, SEP])

        # ── Common context values ─────────────────────────────────────────
        product_name: str = context.get("product_name", "Unknown Product")
        marketplace: str = context.get("marketplace", "unknown")
        business_model: str = context.get(
            "business_model", context.get("business_model_alias", "fbs")
        )
        marketplace_label: str = self._get_marketplace_label(marketplace)
        bm_label: str = self._get_business_model_label(business_model)
        symbol: str = self._get_currency_symbol(context)
        price_key: str = "price_pkr" if marketplace == "daraz_pk" else "price_usd"

        # ── 1. Title box ──────────────────────────────────────────────────
        lines.extend([
            "╔═══════════════════════════════════════════╗",
            "║         DROPSMART INTELLIGENCE REPORT      ║",
            f"║   {marketplace_label} | {bm_label} | {product_name}",
            "╚═══════════════════════════════════════════╝",
        ])

        # ── 2. Supplier Intelligence ───────────────────────────────────────
        _section("📦 SUPPLIER INTELLIGENCE")
        supplier_result: dict = context.get("supplier_result", {})
        suppliers: list[dict] = supplier_result.get("suppliers", [])
        recommended_supplier: str = supplier_result.get("recommended_supplier", "")

        _RISK_EMOJI: dict[str, str] = {
            "LOW": "✅", "MEDIUM": "⚠️", "HIGH": "❌"
        }

        if not suppliers:
            lines.append("No suppliers found — manual sourcing required.")
        else:
            n_sup: int = min(3, len(suppliers))
            lines.append(f"Top {n_sup} Suppliers Found:")
            for idx, sup in enumerate(suppliers[:3], 1):
                name: str = str(sup.get("name", "N/A"))
                raw_price = sup.get(price_key)
                price_display: str = (
                    f"{symbol}{float(raw_price):.2f}"
                    if isinstance(raw_price, (int, float))
                    else (f"{symbol}{raw_price}" if raw_price else "N/A")
                )
                moq: str = str(sup.get("moq", "N/A"))
                shipping_cost: float = float(sup.get("shipping_cost", 0.0))
                shipping_days = sup.get("shipping_days", "N/A")
                rating = sup.get("rating")
                review_count = sup.get("review_count")
                risk_label: str = str(sup.get("risk_label", "UNKNOWN")).upper()
                r_emoji: str = _RISK_EMOJI.get(risk_label, "")

                lines.append(f"  {idx}. {name}")
                lines.append(f"     Price: {price_display}  |  MOQ: {moq}")
                lines.append(
                    f"     Shipping: {symbol}{shipping_cost:.2f}"
                    f"  |  Delivery: {shipping_days} days"
                )
                if rating is not None:
                    rating_line: str = f"     Rating: {rating}"
                    if review_count is not None:
                        rating_line += f" ({review_count} reviews)"
                    lines.append(rating_line)
                risk_line: str = f"     Risk: {risk_label}"
                if r_emoji:
                    risk_line += f" {r_emoji}"
                lines.append(risk_line)

            lines.append(f"Recommended Supplier: {recommended_supplier}")

        # ── 3. Competitor Analysis ─────────────────────────────────────────
        _section(f"🏆 COMPETITOR ANALYSIS — {marketplace_label}")
        competitor_result: dict = context.get("competitor_result", {})
        price_range: dict = competitor_result.get("price_range", {})
        sweet_spot: dict = competitor_result.get("sweet_spot_price_range", {})
        listings: list[dict] = competitor_result.get("listings", [])
        perf_metrics: dict = competitor_result.get("performance_metrics", {})
        market_leader: str = str(competitor_result.get("market_leader", "") or "")

        total_listings = competitor_result.get("total_active_listings", "N/A")
        lines.append(f"Total Active Listings: {total_listings}")

        pr_min = price_range.get("min")
        pr_max = price_range.get("max")
        if isinstance(pr_min, (int, float)) and isinstance(pr_max, (int, float)):
            lines.append(
                f"Price Range: {symbol}{float(pr_min):.2f}"
                f" – {symbol}{float(pr_max):.2f}"
            )
        else:
            lines.append("Price Range: N/A")

        ss_min = sweet_spot.get("min")
        ss_max = sweet_spot.get("max")
        if isinstance(ss_min, (int, float)) and isinstance(ss_max, (int, float)):
            lines.append(
                f"Sweet Spot Price: {symbol}{float(ss_min):.2f}"
                f" – {symbol}{float(ss_max):.2f}"
            )

        if listings:
            lines.append("Top 3 Competitors:")
            for idx, listing in enumerate(listings[:3], 1):
                l_title: str = str(listing.get("title", "N/A"))
                raw_lp = listing.get("price")
                lp_str: str = (
                    f"{symbol}{float(raw_lp):.2f}"
                    if isinstance(raw_lp, (int, float))
                    else (str(raw_lp) if raw_lp else "N/A")
                )
                l_rating = listing.get("rating", "N/A")
                l_reviews = listing.get("review_count", "N/A")
                lines.append(f"  {idx}. {l_title}")
                lines.append(
                    f"     Price: {lp_str}"
                    f"  |  Rating: {l_rating}"
                    f"  |  Reviews: {l_reviews}"
                )

        if perf_metrics:
            lines.append("Performance Metrics:")
            lines.append(
                f"  Est. Monthly Sales:"
                f" {perf_metrics.get('estimated_monthly_sales_range', 'N/A')}"
            )
            lines.append(
                f"  Avg Rating (Top Sellers):"
                f" {perf_metrics.get('avg_rating_to_rank', 'N/A')}"
            )
            lines.append(
                f"  Avg Reviews (Top Sellers):"
                f" {perf_metrics.get('avg_review_count_top_sellers', 'N/A')}"
            )
            lines.append(f"  Market Leader: {market_leader or 'N/A'}")

        # Platform-advantage warning when the market leader name signals
        # a first-party or programme-backed seller.
        if market_leader:
            _ML_SIGNALS: frozenset[str] = frozenset(
                {"mall", "choice", "basics", "official"}
            )
            if any(sig in market_leader.lower() for sig in _ML_SIGNALS):
                lines.append(
                    "⚠️  WARNING: Market leader may have platform-level advantages."
                    " You must compete on price or unique feature."
                )

        # ── 4. Trends & Seasonality ────────────────────────────────────────
        _section("📈 TRENDS & SEASONALITY")
        trends: dict = competitor_result.get("trends_and_seasonality", {})
        trend_direction: str = str(
            trends.get("trend_direction", "unknown")
        ).strip().lower()
        product_type: str = str(
            trends.get("product_type", "unknown")
        ).strip().lower()
        peak_months: list = trends.get("peak_season_months", [])
        demand_signal: str = str(
            trends.get("current_month_demand_signal", "unknown")
        ).strip()
        top_keywords: list = competitor_result.get("top_keywords", [])

        _TREND_EMOJI: dict[str, str] = {
            "growing": "📈", "stable": "➡️", "declining": "📉"
        }
        _TYPE_EMOJI: dict[str, str] = {
            "evergreen": "✅", "seasonal": "🌊", "fad": "⚡"
        }
        lines.append(
            f"Trend Direction: {_TREND_EMOJI.get(trend_direction, '❓')}"
            f" {trend_direction.title()}"
        )
        lines.append(
            f"Product Type: {_TYPE_EMOJI.get(product_type, '❓')}"
            f" {product_type.title()}"
        )

        if peak_months:
            lines.append("Peak Seasons:")
            for month in peak_months:
                lines.append(f"  🔥 {month}")
        else:
            lines.append("Peak Seasons: No clear peak season identified.")

        lines.append(f"Current Month Demand: {demand_signal.title()}")

        if top_keywords:
            lines.append(f"High-Volume Keywords ({marketplace_label}):")
            for idx, kw in enumerate(top_keywords, 1):
                if isinstance(kw, dict):
                    kw_text: str = kw.get("keyword", str(kw))
                    vol: str = str(kw.get("volume_signal", "N/A"))
                    lines.append(f"  {idx}. {kw_text} — {vol} volume")
                else:
                    lines.append(f"  {idx}. {kw}")

        # ── 5. Margin Calculation ──────────────────────────────────────────
        _section(f"💰 MARGIN CALCULATION — {marketplace_label} {bm_label}")
        margin_result: dict = context.get("margin_result", {})
        selling_price: float = float(margin_result.get("selling_price", 0.0))
        breakdown: list[str] = margin_result.get("calculation_breakdown", [])
        monthly: dict = margin_result.get("monthly_profit_potential", {})

        lines.append(f"Recommended Selling Price: {symbol}{selling_price:.2f}")
        lines.append("")
        lines.extend(breakdown)

        if monthly:
            lines.append("")
            lines.append("Monthly Profit Potential:")
            lines.append(
                f"   50 units/month → {symbol}"
                f"{float(monthly.get('50_units', 0.0)):.2f}"
            )
            lines.append(
                f"  100 units/month → {symbol}"
                f"{float(monthly.get('100_units', 0.0)):.2f}"
            )
            lines.append(
                f"  200 units/month → {symbol}"
                f"{float(monthly.get('200_units', 0.0)):.2f}"
            )

        # ── 6. Risk Assessment ─────────────────────────────────────────────
        _section("⚠️  RISK ASSESSMENT")
        risk_result: dict = context.get("risk_result", {})
        risk_dimensions: dict = risk_result.get("risk_dimensions", {})

        _DIM_ORDER: list[str] = [
            "market_saturation_risk",
            "supplier_risk",
            "margin_risk",
            "competition_risk",
            "trend_risk",
            "seasonality_risk",
        ]
        _LEVEL_EMOJI: dict[str, str] = {
            "low": "✅", "medium": "⚠️", "high": "❌"
        }

        for dim_key in _DIM_ORDER:
            dim: dict = risk_dimensions.get(dim_key, {})
            if dim:
                dim_label: str = dim_key.replace("_", " ").title()
                level: str = str(dim.get("level", "unknown"))
                l_emoji: str = _LEVEL_EMOJI.get(level.lower(), "")
                lines.append(f"  {dim_label}: {level.upper()} {l_emoji}")

        overall_level: str = risk_result.get("overall_risk_level", "unknown").upper()
        risk_score: int = int(risk_result.get("risk_score", 0))
        lines.append(f"Overall Risk Score: {overall_level} ({risk_score}/100)")

        # ── 7. Final Verdict ───────────────────────────────────────────────
        _section("🎯 FINAL VERDICT")
        go_no_go: str = risk_result.get("go_no_go_signal", "CAUTION")
        _VERDICT_EMOJI: dict[str, str] = {
            "GO": "✅", "CAUTION": "⚠️", "NO-GO": "❌"
        }
        lines.append(f"Decision: {_VERDICT_EMOJI.get(go_no_go, '⚠️')} {go_no_go}")

        verdict_reasoning: list = report_data.get("verdict_reasoning", [])
        if verdict_reasoning:
            lines.append("Reasoning:")
            for reason in verdict_reasoning:
                lines.append(f"  {reason}")

        recommended_strategy: list = report_data.get("recommended_strategy", [])
        if recommended_strategy:
            lines.append("Recommended Strategy:")
            for step in recommended_strategy:
                lines.append(f"  → {step}")

        # ── 8. Listing Draft ───────────────────────────────────────────────
        _section(f"📝 LISTING DRAFT — OPTIMIZED FOR {marketplace_label.upper()}")
        listing_title: str = str(
            report_data.get("listing_title_draft", product_name)
        )
        lines.append(f'Title (75 chars max): "{listing_title}"')

        listing_bullets: list = report_data.get("listing_bullets", [])
        if listing_bullets:
            lines.append("Key Bullets:")
            for bullet in listing_bullets:
                lines.append(f"  - {bullet}")

        top_5_kw: list = top_keywords[:5]
        kw_display: list[str] = [
            kw.get("keyword", str(kw)) if isinstance(kw, dict) else str(kw)
            for kw in top_5_kw
        ]
        if kw_display:
            lines.append(f"High-Volume Keywords Used: {' | '.join(kw_display)}")

        return "\n".join(lines)
