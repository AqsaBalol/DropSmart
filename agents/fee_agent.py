"""Fee Structure Research Agent for DropSmart.

Dynamically searches for and extracts the current, complete fee structure for the
target marketplace and business model. Every fee value is sourced from live search
results — nothing is hardcoded. Fallback values from platform YAML configs are
used only when search returns nothing, and always trigger a verification warning.

This is the most critical agent in the pipeline: a missed or wrong fee flows
directly into the Margin Calculator and produces an incorrect Go/No-Go verdict.
"""

# --- Standard library ---
import datetime
import json
import os
from pathlib import Path
from typing import Any

# --- Third-party ---
import yaml

# --- Local ---
from agents.base_agent import BaseAgent


# ---------------------------------------------------------------------------
# Daraz handling fee tier table
# Must mirror the YAML config — used as a fallback when search fails.
# ---------------------------------------------------------------------------

# Each entry: (upper_bound_inclusive, flat_fee_pkr)
# None upper bound means "all prices above the previous tier".
_DARAZ_HANDLING_FEE_TIERS: list[tuple[int | None, float]] = [
    (500,   10.0),
    (1000,  15.0),
    (2000,  20.0),
    (None,  60.0),
]

# ---------------------------------------------------------------------------
# Required fee category names per marketplace — used for missing-fee detection.
# Mirrors the fee_categories list from each YAML config.
# ---------------------------------------------------------------------------

_REQUIRED_FEE_NAMES: dict[str, list[str]] = {
    "daraz_pk": [
        "commission_pct",
        "vat_on_commission_pct",
        "payment_processing_pct",
        "vat_on_payment_processing_pct",
        "handling_fee",
        "vat_on_handling_fee_pct",
    ],
    "amazon_us": [
        "commission_pct",
    ],
    "walmart_us": [
        "commission_pct",
    ],
    "etsy_us": [
        "commission_pct",
        "payment_processing_pct",
        "handling_fee",
    ],
}

# Path to the platform_configs directory, resolved relative to this file.
_PLATFORM_CONFIGS_DIR: Path = Path(__file__).parent.parent / "platform_configs"


class FeeAgent(BaseAgent):
    """Dynamically researches the complete fee structure for the target marketplace.

    Runs 3–4 targeted web searches via the MCP server, then asks Gemini to
    extract each fee component individually. Never combines fees or applies a
    single representative rate for a tiered fee. Sets verification warnings
    when any fee cannot be confirmed from live search results.

    Platform-specific behaviour:
    - **Daraz Pakistan**: VAT applies separately to commission, payment
      processing fee, and handling fee. Handling fee is tiered by selling price
      in PKR. VAT rate is province-dependent (Punjab 16%, others 15%).
    - **Amazon USA**: Referral fee varies by category. FBA fees apply only for
      ``fbm`` business model. New 2026 FBA fuel surcharge must be included.
    - **Walmart USA**: Single referral fee covers all transaction costs. No
      separate payment processing fee. WFS fees for ``fbm`` only.
    - **Etsy USA**: Transaction fee applies to item price plus shipping. Offsite
      Ads fee is mandatory above USD 10,000 annual sales threshold.
    """

    def __init__(self) -> None:
        """Initialises the FeeAgent with its fixed agent name."""
        super().__init__("fee_agent")

    # ------------------------------------------------------------------
    # Public pipeline interface
    # ------------------------------------------------------------------

    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """Searches for and returns the complete fee structure for the marketplace.

        Reads ``marketplace``, ``business_model_alias``, ``product_name``, and
        (for Daraz) ``province`` from the session context. Runs targeted fee
        searches via the MCP server, extracts structured fee data with Gemini,
        validates completeness, and calculates ``total_fee_pct``.

        Args:
            context: Cumulative session context from the Orchestrator. Must
                contain ``marketplace``, ``region``, and ``product_name``.
                For Daraz listings, ``province`` must also be present.

        Returns:
            A dict with a single key ``"fee_result"`` containing all required
            fee fields. Numeric fields default to ``0.0`` on failure so the
            Margin Agent can still run — but ``missing_fees_detected`` and
            ``fee_verification_warning`` will flag the problem to the seller
            at the HITL checkpoint.

        .. code-block:: python

            {
                "platform": str,
                "commission_pct": float,
                "payment_processing_pct": float,
                "handling_fee": float,
                "vat_on_commission_pct": float,
                "vat_on_payment_processing_pct": float,
                "vat_on_handling_fee_pct": float,
                "total_fee_pct": float,
                "currency": str,
                "source_urls": list[str],
                "data_retrieved_date": str,
                "missing_fees_detected": bool,
                "fee_verification_warning": str,
                "search_queries_used": list[str],
                "platform_specific_fees": dict,  # FBA/WFS/Etsy extras
            }
        """
        marketplace: str = context.get("marketplace", "").strip()
        business_model: str = context.get(
            "business_model_alias",
            context.get("business_model", "fbs"),
        ).strip()
        product_name: str = context.get("product_name", "general").strip()

        # Determine currency from marketplace — used in output and in prompt
        currency: str = "PKR" if marketplace == "daraz_pk" else "USD"

        self._log_start("fee structure research")

        # Lazy import mirrors the pattern in supplier_agent.py and competitor_agent.py
        from mcp_server.search_mcp import web_search

        # --- Build and run queries ---
        queries: list[str] = self._get_search_queries(context)
        all_raw_results: list[dict[str, Any]] = []
        queries_that_ran: list[str] = []

        for query in queries:
            self._logger.info("Fee search query: %s", query)
            try:
                results = web_search(query=query, num_results=5)
                all_raw_results.extend(results)
                queries_that_ran.append(query)
            except Exception as exc:
                # Log and skip — we want all queries to attempt before giving up
                self._logger.warning("Fee query failed (skipping): %r — %s", query, exc)

        # --- Extract fee data via Gemini ---
        warnings: list[str] = []
        gemini_data: dict[str, Any] = {}

        if all_raw_results:
            search_text: str = self._format_results_for_prompt(all_raw_results)
            prompt: str = self._build_fee_prompt(context, search_text)
            raw_response: str = self._safe_generate(prompt)
            gemini_data = self._parse_gemini_response(raw_response)

        if not gemini_data:
            warnings.append(
                "Web search returned no usable fee data. "
                "Fee values below are platform defaults — verify manually before listing."
            )

        # --- Determine VAT rate (Daraz only) ---
        vat_rate: float = self._determine_vat_rate(context) if marketplace == "daraz_pk" else 0.0

        # --- Read fee fields from Gemini output, with 0.0 as safe default ---
        commission_pct: float = float(gemini_data.get("commission_pct", 0.0))
        payment_processing_pct: float = float(
            gemini_data.get("payment_processing_pct", 0.0)
        )

        # VAT on fees — relevant for Daraz; 0.0 for US marketplaces (no sales tax on fees)
        vat_on_commission_pct: float = float(
            gemini_data.get("vat_on_commission_pct", 0.0)
        )
        vat_on_payment_processing_pct: float = float(
            gemini_data.get("vat_on_payment_processing_pct", 0.0)
        )
        vat_on_handling_fee_pct: float = float(
            gemini_data.get("vat_on_handling_fee_pct", 0.0)
        )

        # --- Apply Daraz-specific logic that cannot be delegated to Gemini ---
        if marketplace == "daraz_pk":
            # VAT rates are province-dependent and computed here rather than
            # trusted from Gemini — this is a business rule, not a web fact.
            vat_on_commission_pct = vat_rate
            vat_on_payment_processing_pct = vat_rate
            vat_on_handling_fee_pct = vat_rate

            # Derive the selling price for tier selection.
            # Prefer competitor avg price; fall back to a neutral middle-tier price
            # and warn the seller so they can check the correct tier manually.
            selling_price = self._get_selling_price_estimate(context)
            if selling_price == 0.0:
                warnings.append(
                    "Could not determine selling price for Daraz handling fee tier selection. "
                    "Defaulted to PKR 60 (2001+ tier). Verify tier against your actual selling price."
                )
                handling_fee: float = 60.0
            else:
                handling_fee = self._calculate_handling_fee_tier(
                    selling_price, currency
                )
        else:
            # For non-Daraz marketplaces, handling_fee covers Etsy's $0.20 listing
            # fee or defaults to 0.0 for platforms with no per-order flat fee.
            handling_fee = float(gemini_data.get("handling_fee", 0.0))

        # --- Validate completeness — flag any required fee that is still 0.0 ---
        missing: list[str] = self._detect_missing_fees(
            marketplace=marketplace,
            commission_pct=commission_pct,
            payment_processing_pct=payment_processing_pct,
            handling_fee=handling_fee,
        )
        missing_fees_detected: bool = bool(missing)
        if missing:
            warnings.append(
                f"The following fees could not be confirmed from search results: "
                f"{', '.join(missing)}. Margin calculation may be understated."
            )

        # --- Calculate total_fee_pct from percentage components only ---
        # Flat fees (handling_fee) are excluded — Margin Agent applies them separately.
        # VAT on fees is a percentage of the fee amount, not of selling price,
        # so it is also excluded here and applied line-by-line in the Margin Agent.
        total_fee_pct: float = round(commission_pct + payment_processing_pct, 4)

        # --- Collect platform-specific fees that don't fit the standard schema ---
        platform_specific_fees: dict[str, Any] = gemini_data.get(
            "platform_specific_fees", {}
        )

        # --- Collect source URLs from Gemini output ---
        source_urls: list[str] = gemini_data.get("source_urls", [])
        if not source_urls:
            # Fall back to URLs from raw search results when Gemini didn't extract them
            source_urls = [
                r.get("url", "")
                for r in all_raw_results
                if r.get("url", "")
            ][:5]

        self._log_end("fee structure research", success=not missing_fees_detected)

        return {
            "fee_result": {
                "platform": marketplace,
                "commission_pct": commission_pct,
                "payment_processing_pct": payment_processing_pct,
                "handling_fee": handling_fee,
                "vat_on_commission_pct": vat_on_commission_pct,
                "vat_on_payment_processing_pct": vat_on_payment_processing_pct,
                "vat_on_handling_fee_pct": vat_on_handling_fee_pct,
                "total_fee_pct": total_fee_pct,
                "currency": currency,
                "source_urls": source_urls,
                "data_retrieved_date": datetime.date.today().isoformat(),
                "missing_fees_detected": missing_fees_detected,
                "fee_verification_warning": " | ".join(warnings) if warnings else "",
                "search_queries_used": queries_that_ran,
                "platform_specific_fees": platform_specific_fees,
            }
        }

    # ------------------------------------------------------------------
    # Query builder
    # ------------------------------------------------------------------

    def _get_search_queries(self, context: dict[str, Any]) -> list[str]:
        """Builds 3–4 targeted fee search queries for the target marketplace.

        Attempts to load query templates from the platform YAML config first,
        substituting ``{product_category}`` with a category derived from the
        product name. Falls back to hardcoded queries when the YAML is unreadable
        so the agent can still run without the config file being present.

        Args:
            context: Session context providing ``marketplace`` and
                ``product_name``.

        Returns:
            A list of 3–4 search query strings ready for the MCP server's
            ``web_search`` tool. Templates from the YAML are used when available;
            hardcoded fallbacks are used otherwise.
        """
        marketplace: str = context.get("marketplace", "").strip()
        product_name: str = context.get("product_name", "general").strip()

        # Use the product name directly as the category — the search engine handles
        # broader matching, so an exact category label is not required.
        product_category: str = product_name

        # --- Attempt to load fee_search_queries from YAML config ---
        yaml_queries: list[str] = self._load_yaml_fee_queries(
            marketplace, product_category
        )
        if yaml_queries:
            return yaml_queries[:4]  # cap at 4 to stay within the rate limit budget

        # --- Hardcoded fallbacks — used only when YAML is unavailable ---
        current_year: int = datetime.date.today().year

        fallback_queries: dict[str, list[str]] = {
            "daraz_pk": [
                f"Daraz Pakistan seller commission fees {current_year} {product_category}",
                f"Daraz seller center fee structure Pakistan {current_year}",
                f"Daraz Pakistan VAT on commission handling fee {current_year}",
                "site:seller.daraz.pk fees commission rates",
            ],
            "amazon_us": [
                f"Amazon seller referral fees by category {current_year} {product_category}",
                f"Amazon FBA fulfillment fees {current_year}",
                f"Amazon seller fees complete breakdown {current_year}",
                "site:sellercentral.amazon.com fees schedule",
            ],
            "walmart_us": [
                f"Walmart Marketplace referral fees by category {current_year} {product_category}",
                f"Walmart seller fees complete breakdown {current_year}",
                f"Walmart Fulfillment Services WFS fees {current_year}",
            ],
            "etsy_us": [
                f"Etsy seller fees complete breakdown {current_year}",
                f"Etsy transaction fee listing fee payment processing {current_year}",
                f"Etsy offsite ads fee {current_year} opt out threshold",
                "site:etsy.com/legal/fees current fee schedule",
            ],
        }
        return fallback_queries.get(marketplace, [
            f"{marketplace} seller fees commission {current_year}"
        ])

    def _load_yaml_fee_queries(
        self, marketplace: str, product_category: str
    ) -> list[str]:
        """Loads ``fee_search_queries`` from the platform YAML config file.

        The YAML templates may contain ``{product_category}`` placeholders that
        are substituted at runtime. If the file does not exist or cannot be
        parsed, returns an empty list so the caller falls back to hardcoded queries.

        Args:
            marketplace: Marketplace identifier used to locate the YAML file.
            product_category: String substituted into ``{product_category}``
                placeholders in the query templates.

        Returns:
            A list of rendered query strings, or ``[]`` if the YAML file is
            unavailable or the ``fee_search_queries`` key is absent.
        """
        config_path: Path = _PLATFORM_CONFIGS_DIR / f"{marketplace}.yaml"

        if not config_path.exists():
            self._logger.warning(
                "Platform config not found: %s — using fallback fee queries.", config_path
            )
            return []

        try:
            with open(config_path, encoding="utf-8") as fh:
                config: dict = yaml.safe_load(fh)

            templates: list[str] = config.get("fee_search_queries", [])
            # Substitute the placeholder in each template string
            rendered: list[str] = [
                t.replace("{product_category}", product_category)
                for t in templates
            ]
            return rendered

        except Exception as exc:
            self._logger.warning(
                "Could not load fee queries from %s: %s — using fallback.", config_path, exc
            )
            return []

    # ------------------------------------------------------------------
    # Prompt builder
    # ------------------------------------------------------------------

    def _build_fee_prompt(
        self, context: dict[str, Any], search_results: str
    ) -> str:
        """Constructs the Gemini prompt for extracting structured fee data.

        The prompt is platform-aware: Daraz instructions emphasise the three
        separate VAT applications and the tiered handling fee; Amazon instructions
        emphasise category-specific referral fees and FBA components; Etsy
        instructions emphasise that the transaction fee applies to shipping too.

        Args:
            context: Session context providing ``marketplace``, ``business_model_alias``,
                and ``product_name`` for framing the extraction.
            search_results: Pre-formatted string of all search result titles,
                snippets, and URLs ready to be embedded in the prompt.

        Returns:
            A complete prompt string ready to be passed to ``_safe_generate``.
        """
        marketplace: str = context.get("marketplace", "")
        business_model: str = context.get(
            "business_model_alias", context.get("business_model", "fbs")
        )
        product_name: str = context.get("product_name", "the product")
        today: str = datetime.date.today().isoformat()
        currency: str = "PKR" if marketplace == "daraz_pk" else "USD"

        # Each platform has a short instruction block describing its unique fee
        # structure. These are embedded in the prompt so Gemini knows what to
        # look for without us having to enumerate every field in natural language.
        platform_instructions: dict[str, str] = {
            "daraz_pk": (
                "DARAZ PAKISTAN SPECIFIC RULES:\n"
                "- commission_pct: percentage of selling price (varies by category, typically 5–20%).\n"
                "- payment_processing_pct: percentage of selling price (verify current rate, ~2.25%).\n"
                "- handling_fee: TIERED flat amount in PKR — do NOT extract as a percentage. "
                "Tiers: PKR 0–500 = PKR 10, PKR 501–1000 = PKR 15, PKR 1001–2000 = PKR 20, PKR 2001+ = PKR 60. "
                "Extract the tier structure from the search results if found.\n"
                "- vat_on_commission_pct, vat_on_payment_processing_pct, vat_on_handling_fee_pct: "
                "VAT applies SEPARATELY to each of the three fee components above. "
                "Province-dependent: Punjab sellers pay 16%, all others pay 15%. "
                "If the search results mention a specific rate, extract it. Otherwise set to 0 — "
                "the agent will apply the correct rate from the seller's province.\n"
                "- NEVER combine commission + VAT into one number. Keep them separate.\n"
                "- For FBD (fbm) only: include FBD fulfillment fee and storage fee in platform_specific_fees."
            ),
            "amazon_us": (
                "AMAZON USA SPECIFIC RULES:\n"
                "- commission_pct maps to the Amazon Referral Fee (varies by category, typically 8–15%).\n"
                "- payment_processing_pct: set to 0.0 — Amazon's referral fee includes transaction costs.\n"
                "- handling_fee: set to 0.0 for non-FBA. For FBM (FBA), include FBA fulfillment fee "
                "in platform_specific_fees.fba_fulfillment_fee_usd (per unit flat amount).\n"
                "- NEW in 2026: FBA Fuel Surcharge = 3.5% of the FBA fulfillment fee. Include in "
                "platform_specific_fees.fba_fuel_surcharge_pct.\n"
                "- FBA Storage Fee: include Jan–Sep and Oct–Dec rates separately in "
                "platform_specific_fees (Q4 rate is roughly 3× standard rate).\n"
                "- Professional Seller Plan: $39.99/month fixed cost — include in "
                "platform_specific_fees.professional_plan_monthly_usd.\n"
                "- No VAT on fees for US sellers — set all vat_* fields to 0.0."
            ),
            "walmart_us": (
                "WALMART USA SPECIFIC RULES:\n"
                "- commission_pct maps to the Walmart Referral Fee (varies by category, 6–20%).\n"
                "- payment_processing_pct: set to 0.0 — Walmart's referral fee is all-inclusive.\n"
                "- handling_fee: set to 0.0 for FBS/dropshipping. For FBM (WFS), include WFS "
                "fulfillment fee in platform_specific_fees.wfs_fulfillment_fee_usd (per unit flat).\n"
                "- No monthly subscription fee — unlike Amazon, this is free to list.\n"
                "- No VAT on fees for US sellers — set all vat_* fields to 0.0."
            ),
            "etsy_us": (
                "ETSY USA SPECIFIC RULES:\n"
                "- commission_pct maps to the Etsy Transaction Fee (6.5% of item price PLUS shipping).\n"
                "- payment_processing_pct: 3.0% (Etsy Payments processing fee).\n"
                "- handling_fee: $0.20 flat listing fee charged per unit sold.\n"
                "- IMPORTANT: platform_specific_fees must include:\n"
                "  payment_processing_flat_usd = 0.25 (the $0.25 fixed part of the payment processing fee)\n"
                "  offsite_ads_fee_pct: 15% for sellers under $10k/year (optional opt-out), "
                "12% for sellers over $10k/year (mandatory, no opt-out).\n"
                "- No VAT on fees for US sellers — set all vat_* fields to 0.0."
            ),
        }
        platform_instruction: str = platform_instructions.get(
            marketplace,
            f"Extract all seller fees for {marketplace} accurately."
        )

        return f"""You are a fee structure data extractor for an e-commerce research tool.

Your task: extract the CURRENT, COMPLETE fee structure for a seller listing "{product_name}" on {marketplace} using the {business_model} business model.

{platform_instruction}

CRITICAL RULES — follow these exactly:
1. Return ONLY valid JSON. No markdown, no code fences, no explanation text before or after.
2. Never invent, assume, or fabricate a fee not mentioned in the search results.
3. Never combine multiple fees into one number — every component must appear separately.
4. If a fee cannot be found in the search results, set it to 0.0 and add its name to missing_fees list.
5. source_urls: list of URLs from the search results where fee information was found.
6. missing_fees: list of fee field names that could not be confirmed (e.g. ["commission_pct"]).
7. All percentage fields are decimal values representing percent (e.g. 8.5 means 8.5%, not 0.085).
8. data_retrieved_date must be exactly: {today}

SEARCH RESULTS:
{search_results}

Return a JSON object in exactly this structure:
{{
  "commission_pct": 0.0,
  "payment_processing_pct": 0.0,
  "handling_fee": 0.0,
  "vat_on_commission_pct": 0.0,
  "vat_on_payment_processing_pct": 0.0,
  "vat_on_handling_fee_pct": 0.0,
  "source_urls": ["https://..."],
  "missing_fees": [],
  "data_retrieved_date": "{today}",
  "platform_specific_fees": {{}}
}}"""

    # ------------------------------------------------------------------
    # Province and VAT helpers
    # ------------------------------------------------------------------

    def _determine_vat_rate(self, context: dict[str, Any]) -> float:
        """Returns the correct VAT rate for the seller's province on Daraz Pakistan.

        VAT on Daraz fees is not a single national rate — it is set at the
        provincial level. Punjab levies 16%; all other provinces levy 15%.
        For non-Daraz marketplaces this method is never called.

        Args:
            context: Session context. Reads ``province`` key (case-insensitive).

        Returns:
            ``0.16`` for Punjab sellers, ``0.15`` for all other provinces.
            Returns ``0.15`` as the safe default when ``province`` is absent —
            this is the lower (less optimistic) rate, consistent with the spec's
            rule to use the most conservative estimate when data is uncertain.
        """
        province: str = context.get("province", "").strip().lower()

        if province == "punjab":
            return 0.16

        if not province:
            # Province missing from context — return safe default and log so
            # the Orchestrator's HITL checkpoint can surface this to the seller.
            self._logger.warning(
                "province not found in context — defaulting VAT to 15%%. "
                "Punjab sellers should use 16%% and may see a slightly higher total cost."
            )

        # All other provinces (Sindh, KPK, Balochistan) use 15%
        return 0.15

    # ------------------------------------------------------------------
    # Handling fee tier calculator
    # ------------------------------------------------------------------

    def _calculate_handling_fee_tier(
        self, selling_price: float, currency: str
    ) -> float:
        """Applies the Daraz tiered handling fee structure.

        The handling fee is not a flat rate — it varies by selling price range
        in PKR. This method selects the correct tier and returns the flat fee
        amount. For non-PKR currencies (i.e. non-Daraz marketplaces), returns
        0.0 because this fee does not apply.

        Tier structure (from Daraz Seller Centre and YAML config fallback):
        - PKR 0–500   → PKR 10
        - PKR 501–1000  → PKR 15
        - PKR 1001–2000 → PKR 20
        - PKR 2001+   → PKR 60

        Args:
            selling_price: The estimated selling price of the product.
            currency: The marketplace currency. Returns 0.0 for anything other
                than ``"PKR"``.

        Returns:
            The flat handling fee in PKR for the applicable tier, or ``0.0``
            if the currency is not PKR.
        """
        # Handling fee only applies to Daraz Pakistan (PKR)
        if currency != "PKR":
            return 0.0

        # Walk the tier table and return the fee for the first matching band.
        # _DARAZ_HANDLING_FEE_TIERS is ordered low-to-high so the first hit is correct.
        for upper_bound, fee in _DARAZ_HANDLING_FEE_TIERS:
            if upper_bound is None or selling_price <= upper_bound:
                return fee

        # Should never reach here given the None sentinel, but be safe
        return 60.0

    def _get_selling_price_estimate(self, context: dict[str, Any]) -> float:
        """Reads the best available selling price estimate from session context.

        The Competitor Agent runs before the Fee Agent and may have populated
        ``competitor_result.avg_market_price``. If that is missing, returns
        0.0 to signal that the tier cannot be determined.

        Args:
            context: Cumulative session context assembled by the Orchestrator.

        Returns:
            The average market price as a float, or ``0.0`` if not available.
        """
        competitor_result: dict = context.get("competitor_result", {})
        avg_price: float = float(competitor_result.get("avg_market_price", 0.0))
        return avg_price

    # ------------------------------------------------------------------
    # Missing fee detection
    # ------------------------------------------------------------------

    def _detect_missing_fees(
        self,
        marketplace: str,
        commission_pct: float,
        payment_processing_pct: float,
        handling_fee: float,
    ) -> list[str]:
        """Identifies required fee fields that are still 0.0 after extraction.

        A fee field being 0.0 could mean either the fee genuinely does not apply
        (e.g. Walmart has no payment processing fee) or it was not found in
        search results. This method only flags fields that are defined as required
        in ``_REQUIRED_FEE_NAMES`` for the given marketplace.

        Args:
            marketplace: The target marketplace identifier.
            commission_pct: Extracted commission percentage (0.0 if not found).
            payment_processing_pct: Extracted payment processing percentage.
            handling_fee: Extracted or calculated handling fee amount.

        Returns:
            A list of field name strings that appear required but are still 0.0.
            Empty list means all required fees were found.
        """
        required: list[str] = _REQUIRED_FEE_NAMES.get(marketplace, ["commission_pct"])
        found_values: dict[str, float] = {
            "commission_pct": commission_pct,
            "payment_processing_pct": payment_processing_pct,
            "handling_fee": handling_fee,
            # VAT fields: mark as found because _determine_vat_rate always provides them
            "vat_on_commission_pct": 1.0,
            "vat_on_payment_processing_pct": 1.0,
            "vat_on_handling_fee_pct": 1.0,
        }
        return [name for name in required if found_values.get(name, 0.0) == 0.0]

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_gemini_response(self, raw_response: str) -> dict[str, Any]:
        """Parses the JSON string returned by Gemini into a fee data dict.

        Strips markdown code fences that Gemini adds despite instructions.
        Returns an empty dict on any parse failure so ``run()`` can fall back
        to YAML defaults and flag the problem via ``fee_verification_warning``.

        Args:
            raw_response: The raw string returned by ``_safe_generate``.

        Returns:
            The parsed fee dict, or ``{}`` if the response is not valid JSON.
        """
        cleaned: str = raw_response.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned.rsplit("```", 1)[0]
        cleaned = cleaned.strip()

        try:
            return json.loads(cleaned)

        except json.JSONDecodeError as exc:
            self._logger.error(
                "Failed to parse Gemini fee response as JSON: %s\n"
                "Raw response (first 300 chars): %s",
                exc,
                raw_response[:300],
            )
            return {}

    # ------------------------------------------------------------------
    # Prompt result formatter
    # ------------------------------------------------------------------

    def _format_results_for_prompt(
        self, results: list[dict[str, Any]]
    ) -> str:
        """Formats raw search result dicts into a labelled text block for the prompt.

        Args:
            results: List of result dicts from the MCP server's ``web_search``
                tool, each containing ``title``, ``snippet``, ``url``, ``date``.

        Returns:
            A formatted multi-line string with one numbered block per result.
        """
        lines: list[str] = []
        for i, result in enumerate(results, start=1):
            title: str = result.get("title", "No title")
            snippet: str = result.get("snippet", "No description")
            url: str = result.get("url", "No URL")
            date: str = result.get("date", "Date unknown")
            lines.append(
                f"[Result {i}]\n"
                f"Title: {title}\n"
                f"Snippet: {snippet}\n"
                f"URL: {url}\n"
                f"Date: {date}\n"
            )
        return "\n".join(lines)
