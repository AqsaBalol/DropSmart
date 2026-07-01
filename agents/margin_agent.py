"""Margin Calculator Agent for DropSmart.

Pure Python arithmetic — no Gemini calls, no web searches. Reads supplier cost,
fee components, and seller costs from the session context assembled by earlier
agents, then computes a complete line-by-line deduction table and margin figures.

Every deduction appears as its own named line. No fees are grouped or hidden.
This is a hard spec requirement: the seller must see exactly where money goes.
"""

# --- Standard library ---
from typing import Any

# --- Local ---
from agents.base_agent import BaseAgent


# Business models where the seller personally ships — courier cost is real
_SELLER_SHIPS_MODELS: frozenset[str] = frozenset({"dropshipping", "fbs"})

# Known acronyms that .title() would incorrectly capitalise (e.g. "Fba" → "FBA")
_ACRONYM_OVERRIDES: dict[str, str] = {
    "fba": "FBA",
    "wfs": "WFS",
}


class MarginAgent(BaseAgent):
    """Calculates net margin from supplier cost, platform fees, and seller costs.

    Reads structured data already in the session context (from SupplierAgent,
    FeeAgent, and CompetitorAgent) plus the seller's own cost inputs. Performs
    all arithmetic in Python — never calls Gemini and never makes web requests.

    Every fee deduction is itemised separately in ``calculation_breakdown``.
    Grouped or hidden totals are a spec violation.

    Output contract:
    - ``net_profit_per_unit`` is selling price minus every named deduction.
    - ``margin_pct`` is net profit expressed as a percentage of selling price.
    - ``break_even_price`` is the minimum selling price at which profit = 0,
      i.e. the total of all cost and fee deductions.
    - ``calculation_breakdown`` is a list of human-readable strings, one per
      deduction line, matching the format shown in the spec worked example.
    """

    def __init__(self) -> None:
        """Initialises the MarginAgent with its fixed agent name."""
        super().__init__("margin_agent")

    # ------------------------------------------------------------------
    # Guard: this agent must never call Gemini
    # ------------------------------------------------------------------

    def _safe_generate(self, prompt: str) -> str:
        """Raises NotImplementedError — MarginAgent never calls Gemini.

        Overrides BaseAgent._safe_generate to prevent accidental AI calls from
        slipping into this agent. The margin calculation is pure arithmetic;
        any call to this method indicates a logic error in the caller.

        Args:
            prompt: Unused — included only to match the base class signature.

        Raises:
            NotImplementedError: Always. MarginAgent is arithmetic-only.
        """
        raise NotImplementedError(
            "MarginAgent does not use Gemini. "
            "All calculations are pure Python arithmetic. "
            "Check the calling code — _safe_generate should never be invoked here."
        )

    # ------------------------------------------------------------------
    # Public pipeline interface
    # ------------------------------------------------------------------

    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """Computes the complete margin analysis from context data.

        Reads supplier cost, all fee components, and seller costs from the
        cumulative session context. Applies every formula defined in the spec,
        building a line-by-line breakdown for the HITL summary and the final
        report.

        Args:
            context: Cumulative session context. Expected keys populated by
                earlier agents:
                - ``supplier_result`` (SupplierAgent)
                - ``fee_result`` (FeeAgent)
                - ``competitor_result`` (CompetitorAgent)
                Direct user inputs also expected:
                - ``packaging_cost`` (float)
                - ``courier_cost`` (float, 0 for FBM)
                - ``marketplace`` (str)
                - ``business_model`` / ``business_model_alias`` (str)

        Returns:
            A dict with a single key ``"margin_result"`` containing the full
            margin analysis. All monetary amounts are rounded to 2 decimal places.
            ``warnings`` lists any data-quality issues the seller should review
            at the HITL checkpoint.
        """
        self._log_start("margin calculation")

        marketplace: str = context.get("marketplace", "").strip()
        business_model: str = context.get(
            "business_model_alias",
            context.get("business_model", "fbs"),
        ).strip()
        product_name: str = context.get("product_name", "Unknown product")
        warnings: list[str] = []

        # --- Extract all input values ---
        fee_data: dict[str, Any] = self._extract_fee_data(context)
        # Currency and symbol are derived from marketplace, not from fee_result,
        # so they are correct even when the fee agent failed and returned no data.
        currency: str = "PKR" if marketplace == "daraz_pk" else "USD"
        symbol: str = self._get_currency_symbol(context)

        selling_price: float = self._extract_selling_price(context, warnings)
        supplier_cost: float = self._extract_supplier_cost(context, currency, warnings)
        # Shipping cost charged by the supplier (separate from seller's courier cost)
        supplier_shipping_cost: float = self._extract_supplier_shipping_cost(context)

        # Packaging cost comes directly from user input in the Orchestrator
        packaging_cost: float = float(context.get("packaging_cost", 0.0))

        # Cost to ship bulk inventory to the marketplace warehouse (FBM/FBA only).
        # 0.0 for FBS and dropshipping — those models never populate this key.
        fulfillment_prep_cost: float = self._extract_fulfillment_prep_cost(context)

        # Courier cost applies only when the seller ships — for FBM the marketplace
        # handles fulfilment and charges an FBA/WFS fee instead of courier cost.
        raw_courier: float = float(context.get("courier_cost", 0.0))
        courier_cost: float = (
            raw_courier if business_model in _SELLER_SHIPS_MODELS else 0.0
        )
        if business_model not in _SELLER_SHIPS_MODELS and raw_courier > 0.0:
            warnings.append(
                f"courier_cost of {symbol}{raw_courier:.2f} was provided but set to 0 "
                f"because business model '{business_model}' uses marketplace fulfilment."
            )

        # --- Fee components from FeeAgent ---
        commission_pct: float = fee_data.get("commission_pct", 0.0)
        vat_on_commission_pct: float = fee_data.get("vat_on_commission_pct", 0.0)
        payment_processing_pct: float = fee_data.get("payment_processing_pct", 0.0)
        vat_on_payment_processing_pct: float = fee_data.get(
            "vat_on_payment_processing_pct", 0.0
        )
        handling_fee_amount: float = fee_data.get("handling_fee", 0.0)
        vat_on_handling_fee_pct: float = fee_data.get("vat_on_handling_fee_pct", 0.0)

        # Warn if the fee agent flagged missing fees — those gaps will understate
        # total_deductions and produce an optimistic margin figure.
        if fee_data.get("missing_fees_detected", False):
            warnings.append(
                "Fee Agent reported missing fees. Margin may be overstated. "
                "Verify all fee components before proceeding."
            )
        if fee_data.get("fee_verification_warning", ""):
            warnings.append(fee_data["fee_verification_warning"])

        # --- Early return when selling price is unavailable ---
        if selling_price == 0.0:
            currency_str = "PKR" if marketplace == "daraz_pk" else "USD"
            self._log_end("margin calculation", success=False)
            return {
                "margin_result": {
                    "product_name": product_name,
                    "marketplace": marketplace,
                    "business_model": business_model,
                    "selling_price": 0.0,
                    "data_unavailable": True,
                    "data_unavailable_reason": (
                        "No competitor pricing data found — selling price "
                        "cannot be determined automatically."
                    ),
                    "currency": currency_str,
                    "supplier_cost_is_assumed": False,
                    "monthly_profit_potential": {
                        "50_units": 0.0,
                        "100_units": 0.0,
                        "200_units": 0.0,
                    },
                    "calculation_breakdown": [
                        "Selling Price: UNKNOWN — no competitor data found.",
                        "Enter your target selling price to calculate margin.",
                    ],
                    "warnings": [
                        "No competitor pricing found — selling price unknown. "
                        "Margin cannot be calculated. Verify competitor prices "
                        "and re-run, or enter your target price manually."
                    ],
                    "net_profit_per_unit": 0.0,
                    "margin_pct": 0.0,
                    "break_even_price": 0.0,
                    "total_deductions": 0.0,
                    "supplier_cost": 0.0,
                    "packaging_cost": 0.0,
                    "courier_cost": 0.0,
                    "fulfillment_prep_cost": 0.0,
                    "supplier_shipping_cost": 0.0,
                    "commission_amount": 0.0,
                    "commission_vat_amount": 0.0,
                    "payment_processing_amount": 0.0,
                    "payment_processing_vat_amount": 0.0,
                    "handling_fee_amount": 0.0,
                    "handling_fee_vat_amount": 0.0,
                    "platform_specific_fees_total": 0.0,
                }
            }

        # ----------------------------------------------------------------
        # CORE ARITHMETIC — every formula from the spec, in order
        # ----------------------------------------------------------------

        # Commission is a percentage of the full selling price
        commission_amount: float = round(selling_price * (commission_pct / 100), 2)

        # VAT on commission is applied to the commission amount, not the selling price
        # (this is the Daraz-specific triple-VAT structure; US platforms have 0% here)
        commission_vat_amount: float = round(
            commission_amount * (vat_on_commission_pct / 100), 2
        )

        # Payment processing fee is a percentage of the selling price
        payment_processing_amount: float = round(
            selling_price * (payment_processing_pct / 100), 2
        )

        # VAT on payment processing is applied to the payment processing amount
        payment_processing_vat_amount: float = round(
            payment_processing_amount * (vat_on_payment_processing_pct / 100), 2
        )

        # VAT on handling fee is applied to the flat handling fee amount
        handling_fee_vat_amount: float = round(
            handling_fee_amount * (vat_on_handling_fee_pct / 100), 2
        )

        # Platform-specific fees (FBA fulfilment, WFS, Etsy offsite ads, etc.)
        # Computed here so the total is available for total_deductions below.
        platform_specific_fees: dict[str, Any] = fee_data.get(
            "platform_specific_fees", {}
        )
        platform_specific_total, platform_specific_lines = (
            self._calculate_platform_specific_total(
                platform_specific_fees, selling_price, symbol
            )
        )

        # Total deductions: every cost and fee on a single line.
        # Order: supplier costs → seller costs → platform fees → platform-specific.
        total_deductions: float = round(
            supplier_cost
            + supplier_shipping_cost
            + packaging_cost
            + fulfillment_prep_cost
            + courier_cost
            + commission_amount
            + commission_vat_amount
            + payment_processing_amount
            + payment_processing_vat_amount
            + handling_fee_amount
            + handling_fee_vat_amount
            + platform_specific_total,
            2,
        )

        # Net profit: what the seller keeps after every deduction
        net_profit_per_unit: float = round(selling_price - total_deductions, 2)

        # Margin percentage: net profit as a share of the selling price
        # Guard against division by zero when selling_price is 0
        margin_pct: float = (
            round((net_profit_per_unit / selling_price) * 100, 2)
            if selling_price > 0.0
            else 0.0
        )

        # Break-even price: the price at which selling covers all costs exactly
        # (net profit = 0), i.e. the sum of all deductions
        break_even_price: float = total_deductions

        # ----------------------------------------------------------------
        # Build the human-readable line-by-line breakdown
        # ----------------------------------------------------------------
        breakdown: list[str] = self._build_breakdown(
            symbol=symbol,
            selling_price=selling_price,
            supplier_cost=supplier_cost,
            supplier_shipping_cost=supplier_shipping_cost,
            packaging_cost=packaging_cost,
            fulfillment_prep_cost=fulfillment_prep_cost,
            courier_cost=courier_cost,
            commission_pct=commission_pct,
            commission_amount=commission_amount,
            vat_on_commission_pct=vat_on_commission_pct,
            commission_vat_amount=commission_vat_amount,
            payment_processing_pct=payment_processing_pct,
            payment_processing_amount=payment_processing_amount,
            vat_on_payment_processing_pct=vat_on_payment_processing_pct,
            payment_processing_vat_amount=payment_processing_vat_amount,
            handling_fee_amount=handling_fee_amount,
            vat_on_handling_fee_pct=vat_on_handling_fee_pct,
            handling_fee_vat_amount=handling_fee_vat_amount,
            platform_specific_lines=platform_specific_lines,
            total_deductions=total_deductions,
            net_profit_per_unit=net_profit_per_unit,
            margin_pct=margin_pct,
            break_even_price=break_even_price,
            marketplace=marketplace,
        )

        self._log_end("margin calculation", success=True)

        return {
            "margin_result": {
                "product_name": product_name,
                "marketplace": marketplace,
                "business_model": business_model,
                "selling_price": selling_price,
                "supplier_cost": supplier_cost,
                "supplier_shipping_cost": supplier_shipping_cost,
                "packaging_cost": packaging_cost,
                "fulfillment_prep_cost": fulfillment_prep_cost,
                "courier_cost": courier_cost,
                "commission_amount": commission_amount,
                "commission_vat_amount": commission_vat_amount,
                "payment_processing_amount": payment_processing_amount,
                "payment_processing_vat_amount": payment_processing_vat_amount,
                "handling_fee_amount": handling_fee_amount,
                "handling_fee_vat_amount": handling_fee_vat_amount,
                "platform_specific_fees_total": platform_specific_total,
                "total_deductions": total_deductions,
                "net_profit_per_unit": net_profit_per_unit,
                "margin_pct": margin_pct,
                "break_even_price": break_even_price,
                "currency": currency,
                "monthly_profit_potential": self._build_monthly_projections(
                    net_profit_per_unit
                ),
                "calculation_breakdown": breakdown,
                "warnings": warnings,
            }
        }

    # ------------------------------------------------------------------
    # Data extraction helpers
    # ------------------------------------------------------------------

    def _extract_supplier_cost(
        self,
        context: dict[str, Any],
        currency: str,
        warnings: list[str],
    ) -> float:
        """Reads the recommended supplier's unit cost from supplier_result.

        Looks up the recommended supplier by name in the suppliers list to get
        the price the Supplier Agent selected. Falls back to the first supplier
        if the recommended name is not matched, then to 0.0 if no suppliers
        exist at all. Appends a warning for each fallback so the seller can
        review at the HITL checkpoint.

        The price key differs by marketplace: ``price_pkr`` for Daraz Pakistan,
        ``price_usd`` for all other marketplaces.

        Args:
            context: Session context containing ``supplier_result``.
            currency: The marketplace currency, used to select the correct
                price key from each supplier dict.
            warnings: Mutable list — any fallback taken appends a message here.

        Returns:
            The supplier unit cost as a float, or ``0.0`` if not available.
        """
        supplier_result: dict = context.get("supplier_result", {})
        suppliers: list[dict] = supplier_result.get("suppliers", [])
        recommended_name: str = supplier_result.get("recommended_supplier", "")

        # The price field name is currency-dependent — the Supplier Agent sets it
        # to price_pkr for Daraz and price_usd for all other marketplaces.
        price_key: str = "price_pkr" if currency == "PKR" else "price_usd"

        if not suppliers:
            warnings.append(
                "No supplier data available — supplier_cost set to 0. "
                "Enter the actual unit cost manually before approving."
            )
            return 0.0

        # Prefer the recommended supplier by name match
        for supplier in suppliers:
            if supplier.get("name", "") == recommended_name:
                cost = float(supplier.get(price_key, 0.0))
                if cost == 0.0:
                    warnings.append(
                        f"Recommended supplier '{recommended_name}' has no price data. "
                        "Falling back to first available supplier."
                    )
                    break
                return cost

        # Fall back to first supplier when name match fails or price is 0
        first_cost = float(suppliers[0].get(price_key, 0.0))
        if first_cost == 0.0:
            warnings.append(
                "Supplier cost could not be read from supplier_result — set to 0. "
                "Enter the actual unit cost manually."
            )
        else:
            if recommended_name:
                warnings.append(
                    f"Recommended supplier '{recommended_name}' not matched in suppliers list. "
                    f"Using first available supplier: '{suppliers[0].get('name', 'Unknown')}'."
                )
        return first_cost

    def _extract_fee_data(self, context: dict[str, Any]) -> dict[str, Any]:
        """Reads the fee_result dict from context.

        Returns the full fee_result dict so ``run()`` can access all individual
        fee components by name. Returns an empty dict if FeeAgent did not run
        or failed — all fee fields then default to 0.0, and ``run()`` will
        add the appropriate warnings.

        Args:
            context: Session context containing ``fee_result``.

        Returns:
            The fee_result dict, or ``{}`` if absent.
        """
        fee_result: dict = context.get("fee_result", {})
        if not fee_result:
            self._logger.warning(
                "fee_result not found in context — all fee components will be 0.0. "
                "Ensure FeeAgent ran successfully before MarginAgent."
            )
        return fee_result

    def _extract_selling_price(
        self,
        context: dict[str, Any],
        warnings: list[str],
    ) -> float:
        """Reads the average market price from competitor_result.

        The CompetitorAgent populates ``competitor_result.avg_market_price`` from
        live marketplace listings. This is the best proxy for the seller's likely
        selling price before they have set one. Falls back to 0.0 with a warning
        when no competitor data is available.

        Args:
            context: Session context containing ``competitor_result``.
            warnings: Mutable list — appends a message when the price is missing.

        Returns:
            The average market price as a float, or ``0.0`` if not available.
        """
        competitor_result: dict = context.get("competitor_result", {})
        avg_price: float = float(competitor_result.get("avg_market_price", 0.0))

        if avg_price == 0.0:
            warnings.append(
                "avg_market_price not found in competitor_result — selling_price set to 0. "
                "Margin and break-even calculations require a selling price."
            )
        return avg_price

    # ------------------------------------------------------------------
    # Breakdown builder
    # ------------------------------------------------------------------

    def _build_breakdown(
        self,
        symbol: str,
        selling_price: float,
        supplier_cost: float,
        supplier_shipping_cost: float,
        packaging_cost: float,
        fulfillment_prep_cost: float,
        courier_cost: float,
        commission_pct: float,
        commission_amount: float,
        vat_on_commission_pct: float,
        commission_vat_amount: float,
        payment_processing_pct: float,
        payment_processing_amount: float,
        vat_on_payment_processing_pct: float,
        payment_processing_vat_amount: float,
        handling_fee_amount: float,
        vat_on_handling_fee_pct: float,
        handling_fee_vat_amount: float,
        platform_specific_lines: list[str],
        total_deductions: float,
        net_profit_per_unit: float,
        margin_pct: float,
        break_even_price: float,
        marketplace: str,
    ) -> list[str]:
        """Builds the human-readable line-by-line deduction breakdown.

        Produces one string per deduction, formatted to match the spec's worked
        example. Zero-value lines for fees that do not apply to the marketplace
        are omitted so the breakdown stays clean — but every non-zero deduction
        is always shown regardless of size.

        Args:
            symbol: Currency symbol or code prepended to each monetary value.
            selling_price: The estimated selling price.
            supplier_cost: Unit cost from the recommended supplier.
            supplier_shipping_cost: Shipping cost charged by the supplier
                (separate from the seller's own courier cost). Shown only
                when > 0.0, immediately after the supplier cost line.
            packaging_cost: Seller-provided packaging cost per unit.
            fulfillment_prep_cost: Cost to ship bulk inventory to the marketplace
                warehouse, per unit (FBM/FBA only). ``0.0`` for all other models.
            courier_cost: Seller-provided courier cost (0 for FBM).
            commission_pct: Commission percentage applied to selling price.
            commission_amount: Calculated commission in currency units.
            vat_on_commission_pct: VAT rate applied to commission (0 for US).
            commission_vat_amount: Calculated VAT on commission.
            payment_processing_pct: Payment processing fee percentage.
            payment_processing_amount: Calculated payment processing fee.
            vat_on_payment_processing_pct: VAT rate on payment processing (0 US).
            payment_processing_vat_amount: Calculated VAT on payment processing.
            handling_fee_amount: Flat handling fee (tiered for Daraz, 0 or flat others).
            vat_on_handling_fee_pct: VAT rate on handling fee (0 for US).
            handling_fee_vat_amount: Calculated VAT on handling fee.
            platform_specific_lines: Pre-formatted breakdown strings for
                marketplace-specific fees (FBA, WFS, etc.) produced by
                ``_calculate_platform_specific_total``. Inserted after the
                handling-fee-VAT line, before the summary separator.
            total_deductions: Sum of all cost and fee items.
            net_profit_per_unit: Selling price minus total deductions.
            margin_pct: Net profit as percentage of selling price.
            break_even_price: Price at which profit equals zero.
            marketplace: Used to label the handling fee line with tier context.

        Returns:
            A list of strings, each representing one line in the breakdown table.
            Lines are ordered: selling price → costs → fees → platform fees → summary.
        """
        lines: list[str] = []
        sep = "─" * 44

        # symbol already includes the correct spacing for each currency:
        # "PKR " (trailing space) for Daraz → "PKR 1,799.00"
        # "$" (no space)  for US platforms  → "$29.99"
        lines.append(f"Selling Price:          {symbol}{selling_price:>10.2f}")
        lines.append(sep)

        # --- Cost section ---
        lines.append(f"  Supplier Cost:      - {symbol}{supplier_cost:>10.2f}")

        # Shipping from supplier: separate from the seller's own courier cost.
        # Only shown when the supplier reported a non-zero shipping charge.
        if supplier_shipping_cost > 0.0:
            lines.append(
                f"  Shipping from Supplier: - {symbol}{supplier_shipping_cost:>10.2f}"
            )

        # Only include packaging and courier when non-zero — zero values add no
        # information and clutter the table for dropshipping runs where they are 0.
        if packaging_cost > 0.0:
            lines.append(f"  Packaging Cost:     - {symbol}{packaging_cost:>10.2f}")

        if fulfillment_prep_cost > 0.0:
            lines.append(
                f"  Ship to Warehouse:  - {symbol}{fulfillment_prep_cost:>10.2f}"
            )

        if courier_cost > 0.0:
            lines.append(f"  Courier / Shipping: - {symbol}{courier_cost:>10.2f}")

        lines.append(sep)

        # --- Fee section — percentage-based fees ---
        if commission_amount > 0.0:
            lines.append(
                f"  Commission ({commission_pct:.2f}%): "
                f"- {symbol}{commission_amount:>10.2f}"
            )

        # VAT on commission: only shown for Daraz (vat_on_commission_pct > 0)
        if commission_vat_amount > 0.0:
            lines.append(
                f"  VAT on Commission ({vat_on_commission_pct:.0f}%): "
                f"- {symbol}{commission_vat_amount:>10.2f}"
            )

        if payment_processing_amount > 0.0:
            lines.append(
                f"  Payment Processing ({payment_processing_pct:.2f}%): "
                f"- {symbol}{payment_processing_amount:>10.2f}"
            )

        if payment_processing_vat_amount > 0.0:
            lines.append(
                f"  VAT on Payment Proc. ({vat_on_payment_processing_pct:.0f}%): "
                f"- {symbol}{payment_processing_vat_amount:>10.2f}"
            )

        # --- Handling fee line — label differs by marketplace ---
        if handling_fee_amount > 0.0:
            # For Daraz, append the tier label so the seller can verify the
            # correct tier was applied to their selling price.
            if marketplace == "daraz_pk":
                lines.append(
                    f"  Handling Fee (tiered): "
                    f"- {symbol}{handling_fee_amount:>10.2f}"
                )
            else:
                lines.append(
                    f"  Handling / Listing Fee: "
                    f"- {symbol}{handling_fee_amount:>10.2f}"
                )

        if handling_fee_vat_amount > 0.0:
            lines.append(
                f"  VAT on Handling Fee ({vat_on_handling_fee_pct:.0f}%): "
                f"- {symbol}{handling_fee_vat_amount:>10.2f}"
            )

        # --- Platform-specific fee lines (FBA, WFS, Etsy offsite ads, etc.) ---
        # Pre-computed by _calculate_platform_specific_total; empty list when none apply.
        lines.extend(platform_specific_lines)

        # --- Summary section ---
        lines.append(sep)
        lines.append(
            f"Net Profit per Unit:    {symbol}{net_profit_per_unit:>10.2f}"
        )
        lines.append(f"Margin %:               {margin_pct:>10.2f}%")
        lines.append(f"Break-Even Price:       {symbol}{break_even_price:>10.2f}")

        return lines

    # ------------------------------------------------------------------
    # Currency helper
    # ------------------------------------------------------------------

    def _get_currency_symbol(self, context: dict[str, Any]) -> str:
        """Returns the display currency symbol for the target marketplace.

        Derived from ``context["marketplace"]`` rather than from
        ``fee_result["currency"]`` so the symbol is always correct even when
        the Fee Agent failed and returned no data.

        Symbol formatting:
        - Daraz Pakistan → ``"PKR "`` (trailing space so the symbol and amount
          read naturally as a prefix, e.g. ``"PKR 1,799.00"``).
        - All US marketplaces → ``"$"`` (no space, e.g. ``"$29.99"``).

        Args:
            context: Cumulative session context. Reads ``"marketplace"`` key.

        Returns:
            ``"PKR "`` for ``daraz_pk``, ``"$"`` for all other marketplaces.
        """
        marketplace: str = context.get("marketplace", "").strip()
        return "PKR " if marketplace == "daraz_pk" else "$"

    # ------------------------------------------------------------------
    # Supplier shipping cost helper
    # ------------------------------------------------------------------

    def _extract_fulfillment_prep_cost(self, context: dict[str, Any]) -> float:
        """Reads the fulfillment_prep_cost from context.

        This value is only populated for the ``fulfilled_by_marketplace``
        business model — it represents the per-unit cost of shipping bulk
        inventory to the marketplace warehouse (e.g. Amazon FBA prep and
        inbound freight). For ``dropshipping`` and ``fulfilled_by_seller`` the
        key is absent or zero and this method returns ``0.0``.

        Args:
            context: Session context. Reads ``"fulfillment_prep_cost"`` key.

        Returns:
            The per-unit warehouse prep/shipping cost as a float, or ``0.0``
            if the key is absent or the value cannot be converted.
        """
        try:
            return float(context.get("fulfillment_prep_cost", 0.0))
        except (TypeError, ValueError):
            return 0.0

    def _extract_supplier_shipping_cost(self, context: dict[str, Any]) -> float:
        """Reads the recommended supplier's shipping_cost from supplier_result.

        Matches the recommended supplier by name in the suppliers list and
        returns their ``shipping_cost`` field. Returns 0.0 when the supplier is
        not found, has no shipping cost, or supplier_result is absent — the
        caller suppresses 0.0 lines so the breakdown stays clean.

        Args:
            context: Session context containing ``supplier_result``.

        Returns:
            The supplier's per-shipment or per-unit shipping cost as a float,
            or ``0.0`` if not available.
        """
        supplier_result: dict = context.get("supplier_result", {})
        suppliers: list[dict] = supplier_result.get("suppliers", [])
        recommended_name: str = supplier_result.get("recommended_supplier", "")

        for supplier in suppliers:
            if supplier.get("name", "") == recommended_name:
                return float(supplier.get("shipping_cost", 0.0))

        # Fall back to first supplier when no name match
        if suppliers:
            return float(suppliers[0].get("shipping_cost", 0.0))

        return 0.0

    # ------------------------------------------------------------------
    # Platform-specific fees helper
    # ------------------------------------------------------------------

    def _calculate_platform_specific_total(
        self,
        platform_specific_fees: dict[str, Any],
        selling_price: float,
        symbol: str,
    ) -> tuple[float, list[str]]:
        """Calculates the total and breakdown lines for platform-specific fees.

        Processes the ``platform_specific_fees`` dict from FeeAgent output.
        Three kinds of keys are recognised:
        - ``*_usd`` (but NOT ``*_monthly_usd``): flat fee, added directly.
        - ``*_pct``: percentage of selling price, ``selling_price * (value / 100)``.
        - ``*_monthly_usd``: subscription fees — skipped, not per-unit costs.

        Args:
            platform_specific_fees: Dict of fee keys to numeric values, as
                returned by FeeAgent under the ``platform_specific_fees`` key.
                Example keys: ``fba_fulfillment_fee_usd``, ``fba_fuel_surcharge_pct``,
                ``professional_plan_monthly_usd``.
            selling_price: The unit selling price, used to compute
                percentage-based fees.
            symbol: Currency symbol prepended to each monetary value in the
                returned breakdown lines.

        Returns:
            A tuple of:
            - ``total`` (float): Sum of all applicable platform-specific fees,
              rounded to 2 decimal places.
            - ``lines`` (list[str]): One formatted string per fee, ready to be
              inserted into the calculation breakdown. Empty when no fees apply.
        """
        total: float = 0.0
        lines: list[str] = []

        for key, value in platform_specific_fees.items():
            try:
                amount: float = float(value)
            except (TypeError, ValueError):
                continue

            if key.endswith("_monthly_usd"):
                # Subscription / plan fees are not per-unit costs — skip them.
                continue

            if key.endswith("_usd"):
                fee_amount = round(amount, 2)
                label = self._fee_key_to_label(key, suffix="_usd")
                lines.append(f"  {label}: - {symbol}{fee_amount:>10.2f}")
                total += fee_amount

            elif key.endswith("_pct"):
                fee_amount = round(selling_price * (amount / 100), 2)
                label = self._fee_key_to_label(key, suffix="_pct")
                lines.append(
                    f"  {label} ({amount:.2f}%): - {symbol}{fee_amount:>10.2f}"
                )
                total += fee_amount

        return round(total, 2), lines

    @staticmethod
    def _fee_key_to_label(key: str, suffix: str) -> str:
        """Converts a fee dict key into a human-readable label.

        Strips the given suffix, replaces underscores with spaces, and
        title-cases the result. For example:
        ``"fba_fulfillment_fee_usd"`` → ``"FBA Fulfillment Fee"``
        ``"offsite_ads_fee_pct"``     → ``"Offsite Ads Fee"``

        Args:
            key: The raw fee key from FeeAgent output.
            suffix: The suffix to strip (``"_usd"`` or ``"_pct"``).

        Returns:
            A title-cased human-readable label string.
        """
        base = key[: -len(suffix)] if key.endswith(suffix) else key
        words = base.replace("_", " ").title().split()
        words = [_ACRONYM_OVERRIDES.get(w.lower(), w) for w in words]
        return " ".join(words)

    # ------------------------------------------------------------------
    # Monthly projection helper
    # ------------------------------------------------------------------

    def _build_monthly_projections(
        self, net_profit_per_unit: float
    ) -> dict[str, float]:
        """Calculates monthly profit potential at three unit-volume tiers.

        Pure multiplication — no new inputs required beyond the net profit
        per unit already computed by ``run()``. Each value is the profit the
        seller would earn if they sold that many units in a month.

        Args:
            net_profit_per_unit: Net profit per unit after all deductions,
                as calculated by the core arithmetic in ``run()``.

        Returns:
            Dict with keys ``"50_units"``, ``"100_units"``, ``"200_units"``,
            each holding the total monthly profit rounded to 2 decimal places.
        """
        return {
            "50_units": round(net_profit_per_unit * 50, 2),
            "100_units": round(net_profit_per_unit * 100, 2),
            "200_units": round(net_profit_per_unit * 200, 2),
        }
