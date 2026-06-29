"""Risk Assessment Agent for DropSmart.

Reads the cumulative session context produced by all earlier agents
(SupplierAgent, CompetitorAgent, FeeAgent, MarginAgent) and produces a
structured risk profile for the product opportunity.

Scoring is pure Python — no Gemini calls involved. One Gemini call is made
at the end to convert the numeric scores into a human-readable risk narrative,
but Gemini never influences the scores themselves.
"""

# --- Standard library ---
from typing import Any

# --- Local ---
from agents.base_agent import BaseAgent


# ---------------------------------------------------------------------------
# Score → level thresholds (shared by _score_dimension and overall scoring)
# ---------------------------------------------------------------------------

_LEVEL_MEDIUM_THRESHOLD: int = 30   # score <= 30 → "low"
_LEVEL_HIGH_THRESHOLD: int = 60     # score <= 60 → "medium", > 60 → "high"


class RiskAgent(BaseAgent):
    """Assesses product-launch risk across six independent dimensions.

    Reads structured outputs from earlier pipeline agents and applies
    deterministic, rules-based scoring — no Gemini calls affect the scores.
    A single Gemini call at the end generates a human-readable risk summary
    from the finalised scores.

    Risk dimensions scored:
    - **margin_risk**: based on ``margin_result.margin_pct``
    - **competition_risk**: based on ``competitor_result.market_saturation``
    - **supplier_risk**: based on ``supplier_result.reliability_score``
    - **fee_accuracy_risk**: based on ``fee_result.missing_fees_detected``
    - **market_saturation_risk**: same signal as competition_risk
    - **data_freshness_risk**: based on ``supplier_result.data_freshness_warning``

    Output contract — ``risk_result`` contains:
    - ``overall_risk_level``: ``"low"`` / ``"medium"`` / ``"high"``
    - ``risk_score``: integer 0–100 (mean of the six dimension scores)
    - ``top_risks``: list of three human-readable strings for the worst dimensions
    - ``risk_dimensions``: dict of six ``{score, level, reason}`` dicts
    - ``go_no_go_signal``: ``"GO"`` / ``"CAUTION"`` / ``"NO-GO"``
    - ``warnings``: list of data-quality notes raised during scoring
    """

    def __init__(self) -> None:
        """Initialises the RiskAgent with its fixed agent name."""
        super().__init__("risk_agent")

    # ------------------------------------------------------------------
    # Public pipeline interface
    # ------------------------------------------------------------------

    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """Executes risk assessment and returns the structured risk profile.

        Pulls signals from the session context, scores each of the six
        risk dimensions using deterministic Python rules, averages the scores
        to produce an overall risk level, and calls Gemini once to generate
        a plain-English risk summary paragraph.

        Args:
            context: Cumulative session context. Keys consumed here:
                - ``margin_result.margin_pct`` (float)
                - ``competitor_result.market_saturation`` (str)
                - ``supplier_result.reliability_score`` (int)
                - ``supplier_result.data_freshness_warning`` (bool)
                - ``fee_result.missing_fees_detected`` (bool)
                Also used for labelling: ``product``, ``marketplace``.

        Returns:
            A dict with a single key ``"risk_result"`` whose value is the
            full risk profile dict described in the class docstring.
        """
        self._log_start("risk assessment")

        warnings: list[str] = []

        # ----------------------------------------------------------------
        # Extract signals from the context
        # ----------------------------------------------------------------
        margin_pct: float = self._extract_margin_pct(context, warnings)
        market_saturation: str = self._extract_market_saturation(context, warnings)
        reliability_score: float = self._extract_reliability_score(context, warnings)
        supplier_found: bool = self._extract_supplier_found(context)
        missing_fees: bool = self._extract_missing_fees(context, warnings)
        data_freshness_warning: bool = self._extract_data_freshness_warning(context)

        # ----------------------------------------------------------------
        # Score each dimension (pure Python — no Gemini)
        # ----------------------------------------------------------------
        margin_dim = self._score_margin_risk(margin_pct)
        competition_dim = self._score_competition_risk(market_saturation)
        supplier_dim = self._score_supplier_risk(reliability_score, supplier_found)
        fee_accuracy_dim = self._score_fee_accuracy_risk(missing_fees)
        # Market saturation risk re-uses the same market_saturation signal —
        # it is a distinct reporting dimension even though the input is the same.
        market_saturation_dim = self._score_market_saturation_risk(market_saturation)
        data_freshness_dim = self._score_data_freshness_risk(data_freshness_warning)

        risk_dimensions: dict[str, dict[str, Any]] = {
            "margin_risk": margin_dim,
            "competition_risk": competition_dim,
            "supplier_risk": supplier_dim,
            "fee_accuracy_risk": fee_accuracy_dim,
            "market_saturation_risk": market_saturation_dim,
            "data_freshness_risk": data_freshness_dim,
        }

        # ----------------------------------------------------------------
        # Aggregate to overall score and level
        # ----------------------------------------------------------------
        all_scores: list[int] = [d["score"] for d in risk_dimensions.values()]
        # Integer average — round half-up via int(x + 0.5)
        overall_risk_score: int = int(sum(all_scores) / len(all_scores) + 0.5)
        overall_risk_level: str = self._score_dimension(overall_risk_score)

        # ----------------------------------------------------------------
        # Derive the three worst dimensions for top_risks
        # ----------------------------------------------------------------
        top_risks: list[str] = self._build_top_risks(risk_dimensions)

        # ----------------------------------------------------------------
        # GO / CAUTION / NO-GO signal
        # ----------------------------------------------------------------
        go_no_go_signal: str = self._derive_go_no_go(overall_risk_level)

        # ----------------------------------------------------------------
        # One Gemini call: human-readable risk summary (not used for scoring)
        # ----------------------------------------------------------------
        risk_summary: str = self._generate_risk_summary(
            context=context,
            overall_risk_level=overall_risk_level,
            overall_risk_score=overall_risk_score,
            risk_dimensions=risk_dimensions,
            go_no_go_signal=go_no_go_signal,
            top_risks=top_risks,
        )

        self._log_end("risk assessment", success=True)

        return {
            "risk_result": {
                "overall_risk_level": overall_risk_level,
                "risk_score": overall_risk_score,
                "top_risks": top_risks,
                "risk_dimensions": risk_dimensions,
                "go_no_go_signal": go_no_go_signal,
                "risk_summary": risk_summary,
                "warnings": warnings,
            }
        }

    # ------------------------------------------------------------------
    # Dimension level helper
    # ------------------------------------------------------------------

    def _score_dimension(self, score: int) -> str:
        """Maps a numeric dimension score to a risk level label.

        Args:
            score: Integer 0–100; higher values mean more risk.

        Returns:
            ``"low"`` when score <= 30, ``"medium"`` when score <= 60,
            ``"high"`` when score > 60.
        """
        if score <= _LEVEL_MEDIUM_THRESHOLD:
            return "low"
        if score <= _LEVEL_HIGH_THRESHOLD:
            return "medium"
        return "high"

    # ------------------------------------------------------------------
    # GO / CAUTION / NO-GO derivation
    # ------------------------------------------------------------------

    def _derive_go_no_go(self, overall_risk_level: str) -> str:
        """Converts an overall risk level into a go/no-go signal.

        Args:
            overall_risk_level: One of ``"low"``, ``"medium"``, ``"high"``.

        Returns:
            ``"GO"`` for low, ``"CAUTION"`` for medium, ``"NO-GO"`` for high.
        """
        mapping: dict[str, str] = {
            "low": "GO",
            "medium": "CAUTION",
            "high": "NO-GO",
        }
        # Default to NO-GO for any unrecognised level — fail safe
        return mapping.get(overall_risk_level, "NO-GO")

    # ------------------------------------------------------------------
    # Individual dimension scorers
    # ------------------------------------------------------------------

    def _score_margin_risk(self, margin_pct: float) -> dict[str, Any]:
        """Scores margin risk based on the computed net margin percentage.

        Args:
            margin_pct: Net margin as a percentage of selling price.

        Returns:
            Dict with ``score`` (int), ``level`` (str), and ``reason`` (str).
        """
        if margin_pct <= 0:
            score, reason = 100, f"Margin is {margin_pct:.1f}% — selling at a loss."
        elif margin_pct < 10:
            score, reason = 80, f"Margin is {margin_pct:.1f}% — very thin, vulnerable to fee changes."
        elif margin_pct <= 20:
            score, reason = 50, f"Margin is {margin_pct:.1f}% — acceptable but limited buffer."
        else:
            score, reason = 10, f"Margin is {margin_pct:.1f}% — healthy, good cost cushion."

        return {"score": score, "level": self._score_dimension(score), "reason": reason}

    def _score_competition_risk(self, market_saturation: str) -> dict[str, Any]:
        """Scores competition risk from the market saturation signal.

        Args:
            market_saturation: ``"low"``, ``"medium"``, or ``"high"`` as
                reported by CompetitorAgent.

        Returns:
            Dict with ``score``, ``level``, and ``reason``.
        """
        mapping: dict[str, tuple[int, str]] = {
            "low":    (20, "Low market saturation — limited direct competition."),
            "medium": (50, "Moderate saturation — competitive but manageable."),
            "high":   (80, "High saturation — market crowded, pricing pressure likely."),
        }
        score, reason = mapping.get(
            market_saturation,
            (50, f"Unknown saturation '{market_saturation}' — defaulting to medium."),
        )
        return {"score": score, "level": self._score_dimension(score), "reason": reason}

    def _score_supplier_risk(
        self, reliability_score: float, supplier_found: bool
    ) -> dict[str, Any]:
        """Scores supplier risk from the supplier reliability score.

        Args:
            reliability_score: Numeric reliability score 1–10 from SupplierAgent.
            supplier_found: ``False`` when no supplier was located at all.

        Returns:
            Dict with ``score``, ``level``, and ``reason``.
        """
        if not supplier_found:
            score, reason = 90, "No supplier found — sourcing is unresolved."
        elif reliability_score >= 8:
            score, reason = 10, f"Supplier reliability score {reliability_score:.0f}/10 — highly reliable."
        elif reliability_score >= 5:
            score, reason = 40, f"Supplier reliability score {reliability_score:.0f}/10 — adequate, monitor quality."
        else:
            score, reason = 70, f"Supplier reliability score {reliability_score:.0f}/10 — low confidence in fulfilment."

        return {"score": score, "level": self._score_dimension(score), "reason": reason}

    def _score_fee_accuracy_risk(self, missing_fees: bool) -> dict[str, Any]:
        """Scores fee accuracy risk based on whether FeeAgent flagged gaps.

        Args:
            missing_fees: ``True`` when FeeAgent detected at least one fee
                component it could not confirm.

        Returns:
            Dict with ``score``, ``level``, and ``reason``.
        """
        if missing_fees:
            score = 70
            reason = "FeeAgent detected missing fee components — margin may be overstated."
        else:
            score = 10
            reason = "All expected fee components were identified — fee data is complete."

        return {"score": score, "level": self._score_dimension(score), "reason": reason}

    def _score_market_saturation_risk(self, market_saturation: str) -> dict[str, Any]:
        """Scores market saturation risk (same signal as competition risk).

        Market saturation risk and competition risk are reported as separate
        dimensions in the output spec. Both read market_saturation, but they
        represent distinct business concerns: competition risk reflects pricing
        pressure, while market saturation risk reflects listing discoverability.

        Args:
            market_saturation: ``"low"``, ``"medium"``, or ``"high"``.

        Returns:
            Dict with ``score``, ``level``, and ``reason``.
        """
        mapping: dict[str, tuple[int, str]] = {
            "low":    (20, "Low saturation — new listing has good discoverability."),
            "medium": (50, "Medium saturation — visibility requires optimised listing."),
            "high":   (80, "High saturation — ranking a new listing will be difficult."),
        }
        score, reason = mapping.get(
            market_saturation,
            (50, f"Unknown saturation '{market_saturation}' — defaulting to medium."),
        )
        return {"score": score, "level": self._score_dimension(score), "reason": reason}

    def _score_data_freshness_risk(self, data_freshness_warning: bool) -> dict[str, Any]:
        """Scores data freshness risk based on the SupplierAgent's staleness flag.

        Args:
            data_freshness_warning: ``True`` when SupplierAgent indicated that
                supplier prices may be stale or unverifiable.

        Returns:
            Dict with ``score``, ``level``, and ``reason``.
        """
        if data_freshness_warning:
            score = 60
            reason = "Supplier data freshness warning raised — prices may be outdated."
        else:
            score = 10
            reason = "Supplier data appears current — no freshness concerns."

        return {"score": score, "level": self._score_dimension(score), "reason": reason}

    # ------------------------------------------------------------------
    # Top-risks builder
    # ------------------------------------------------------------------

    def _build_top_risks(
        self, risk_dimensions: dict[str, dict[str, Any]]
    ) -> list[str]:
        """Selects the three highest-scoring dimensions as human-readable strings.

        Sorts all six dimensions by score descending and returns the top three
        formatted as ``"<DimensionName>: <reason>"`` strings.

        Args:
            risk_dimensions: The fully scored dimensions dict.

        Returns:
            List of exactly three strings describing the most significant risks.
        """
        # Sort dimensions by score descending; stable sort preserves dict order on ties
        sorted_dims = sorted(
            risk_dimensions.items(),
            key=lambda kv: kv[1]["score"],
            reverse=True,
        )

        top_risks: list[str] = []
        for dim_name, dim_data in sorted_dims[:3]:
            # Convert snake_case key to a readable label e.g. "margin_risk" → "Margin Risk"
            label = dim_name.replace("_", " ").title()
            top_risks.append(f"{label}: {dim_data['reason']}")

        return top_risks

    # ------------------------------------------------------------------
    # Gemini call — human-readable summary only, never used for scoring
    # ------------------------------------------------------------------

    def _generate_risk_summary(
        self,
        context: dict[str, Any],
        overall_risk_level: str,
        overall_risk_score: int,
        risk_dimensions: dict[str, dict[str, Any]],
        go_no_go_signal: str,
        top_risks: list[str],
    ) -> str:
        """Generates a plain-English risk narrative using Gemini.

        This is the only Gemini call in RiskAgent. It converts the
        already-finalised numeric scores into a concise advisory paragraph.
        Gemini receives the scores as read-only context — it may not change
        them; the narrative is illustrative only.

        Args:
            context: Session context for product/marketplace labels.
            overall_risk_level: ``"low"`` / ``"medium"`` / ``"high"``.
            overall_risk_score: Integer 0–100 overall risk score.
            risk_dimensions: Dict of all six scored dimensions.
            go_no_go_signal: ``"GO"`` / ``"CAUTION"`` / ``"NO-GO"``.
            top_risks: The three highest-risk descriptions.

        Returns:
            A plain-English summary string. Returns a fallback sentence if
            the Gemini call fails so the pipeline is not blocked.
        """
        product: str = context.get("product", "the product")
        marketplace: str = context.get("marketplace", "the marketplace")

        # Build a compact summary of each dimension for Gemini to narrate
        dim_lines: str = "\n".join(
            f"  - {k.replace('_', ' ').title()}: "
            f"score={v['score']}, level={v['level']}, reason={v['reason']}"
            for k, v in risk_dimensions.items()
        )

        top_risks_text: str = "\n".join(
            f"  {i + 1}. {r}" for i, r in enumerate(top_risks)
        )

        prompt: str = (
            f"You are a product-launch risk advisor for an e-commerce seller.\n\n"
            f"Product: {product}\n"
            f"Marketplace: {marketplace}\n"
            f"Overall risk score: {overall_risk_score}/100 ({overall_risk_level} risk)\n"
            f"Go/No-Go signal: {go_no_go_signal}\n\n"
            f"Dimension scores (DO NOT change these — narrate only):\n{dim_lines}\n\n"
            f"Top 3 risks:\n{top_risks_text}\n\n"
            f"Write a concise 3–4 sentence advisory paragraph for the seller. "
            f"Reference the top risks by name. End with a clear one-sentence recommendation "
            f"that aligns with the {go_no_go_signal} signal."
        )

        try:
            return self._safe_generate(prompt)
        except Exception:
            # Gemini failure must not block the pipeline — all scoring is already done
            self._logger.warning(
                "[risk_agent] Gemini risk summary generation failed — using fallback text."
            )
            return (
                f"Risk assessment complete. Overall risk level: {overall_risk_level} "
                f"(score {overall_risk_score}/100). Signal: {go_no_go_signal}. "
                f"Top concern: {top_risks[0] if top_risks else 'N/A'}."
            )

    # ------------------------------------------------------------------
    # Context extraction helpers
    # ------------------------------------------------------------------

    def _extract_margin_pct(
        self, context: dict[str, Any], warnings: list[str]
    ) -> float:
        """Reads margin_pct from margin_result in context.

        Args:
            context: Session context.
            warnings: Mutable list — appended to when data is missing.

        Returns:
            Net margin percentage as a float, or ``0.0`` with a warning.
        """
        margin_result: dict = context.get("margin_result", {})
        margin_pct = margin_result.get("margin_pct")

        if margin_pct is None:
            warnings.append(
                "margin_result.margin_pct not found — margin_risk scored as loss scenario. "
                "Ensure MarginAgent ran before RiskAgent."
            )
            return 0.0

        return float(margin_pct)

    def _extract_market_saturation(
        self, context: dict[str, Any], warnings: list[str]
    ) -> str:
        """Reads market_saturation from competitor_result in context.

        Args:
            context: Session context.
            warnings: Mutable list — appended to when data is missing.

        Returns:
            Market saturation string (``"low"``/``"medium"``/``"high"``),
            or ``"medium"`` as a conservative default with a warning.
        """
        competitor_result: dict = context.get("competitor_result", {})
        saturation = competitor_result.get("market_saturation")

        if not saturation:
            warnings.append(
                "competitor_result.market_saturation not found — defaulting to 'medium'. "
                "Ensure CompetitorAgent ran before RiskAgent."
            )
            return "medium"

        return str(saturation).strip().lower()

    def _extract_reliability_score(
        self, context: dict[str, Any], warnings: list[str]
    ) -> float:
        """Reads reliability_score from supplier_result in context.

        Args:
            context: Session context.
            warnings: Mutable list — appended to when data is missing.

        Returns:
            Reliability score 1–10, or ``5.0`` as a neutral default.
        """
        supplier_result: dict = context.get("supplier_result", {})
        score = supplier_result.get("reliability_score")

        if score is None:
            warnings.append(
                "supplier_result.reliability_score not found — defaulting to 5. "
                "Ensure SupplierAgent ran before RiskAgent."
            )
            return 5.0

        return float(score)

    def _extract_supplier_found(self, context: dict[str, Any]) -> bool:
        """Determines whether SupplierAgent located at least one supplier.

        A supplier is considered found when ``supplier_result.suppliers`` is a
        non-empty list.

        Args:
            context: Session context.

        Returns:
            ``True`` if at least one supplier was found, ``False`` otherwise.
        """
        supplier_result: dict = context.get("supplier_result", {})
        suppliers: list = supplier_result.get("suppliers", [])
        return bool(suppliers)

    def _extract_missing_fees(
        self, context: dict[str, Any], warnings: list[str]
    ) -> bool:
        """Reads missing_fees_detected from fee_result in context.

        Args:
            context: Session context.
            warnings: Mutable list — appended to when fee_result is absent.

        Returns:
            ``True`` if missing fees were detected, ``False`` otherwise.
        """
        fee_result: dict = context.get("fee_result", {})

        if not fee_result:
            warnings.append(
                "fee_result not found in context — fee_accuracy_risk assumes no missing fees. "
                "Ensure FeeAgent ran before RiskAgent."
            )
            return False

        return bool(fee_result.get("missing_fees_detected", False))

    def _extract_data_freshness_warning(self, context: dict[str, Any]) -> bool:
        """Reads data_freshness_warning from supplier_result in context.

        Args:
            context: Session context.

        Returns:
            ``True`` if SupplierAgent raised a freshness warning, ``False``
            if absent or not raised.
        """
        supplier_result: dict = context.get("supplier_result", {})
        return bool(supplier_result.get("data_freshness_warning", False))
