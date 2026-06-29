"""Orchestrator for the DropSmart pipeline.

Entry point for every product research run. Collects and validates user input,
drives the six specialist agents in sequence, enforces the mandatory HITL
checkpoint between the Risk Assessor and the Report Generator, and returns the
final session context dict to the caller.

The Orchestrator does NOT inherit from BaseAgent because it makes no Gemini
calls of its own — it only coordinates agents that do.
"""

# --- Standard library ---
import logging
import os
from typing import Any, Optional

# --- Third-party ---
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Valid marketplace identifiers — must match the values the MCP server enforces
_VALID_MARKETPLACES: frozenset[str] = frozenset(
    {"daraz_pk", "walmart_us", "amazon_us", "etsy_us"}
)

# Valid business model identifiers accepted by the Orchestrator.
# Note: the MCP server uses the short aliases (fbs, fbm); the Orchestrator uses
# the descriptive forms and maps them to the short aliases before passing downstream.
_VALID_BUSINESS_MODELS: frozenset[str] = frozenset(
    {"dropshipping", "fulfilled_by_seller", "fulfilled_by_marketplace"}
)

# Mapping from the Orchestrator's descriptive business model names to the
# short aliases used by the MCP server and specialist agents.
_BUSINESS_MODEL_ALIASES: dict[str, str] = {
    "dropshipping": "dropshipping",
    "fulfilled_by_seller": "fbs",
    "fulfilled_by_marketplace": "fbm",
}

# Marketplace → ISO region code, derived at runtime instead of asked separately
_MARKETPLACE_REGIONS: dict[str, str] = {
    "daraz_pk": "PK",
    "walmart_us": "US",
    "amazon_us": "US",
    "etsy_us": "US",
}

# Human-readable labels used in the HITL summary table
_MARKETPLACE_LABELS: dict[str, str] = {
    "daraz_pk": "Daraz Pakistan",
    "walmart_us": "Walmart USA",
    "amazon_us": "Amazon USA",
    "etsy_us": "Etsy USA",
}

_BUSINESS_MODEL_LABELS: dict[str, str] = {
    "dropshipping": "Dropshipping",
    "fulfilled_by_seller": "Fulfilled by Seller (FBS)",
    "fulfilled_by_marketplace": "Fulfilled by Marketplace (FBM/FBA)",
}


class DropSmartOrchestrator:
    """Coordinates the full DropSmart research pipeline for a single product.

    Responsibilities:
    - Loads environment variables so specialist agents can read ``GEMINI_API_KEY``
      and ``SERPER_API_KEY`` when they are instantiated.
    - Validates all user input before any agent runs — the pipeline never starts
      on bad data.
    - Lazy-imports and instantiates each specialist agent only when ``run()`` is
      called. This avoids circular-import issues that would occur if all agents
      were imported at module load time.
    - Runs agents sequentially and merges each agent's output into a cumulative
      session context dict that is passed forward to the next agent.
    - Enforces the mandatory HITL checkpoint: the Report Generator Agent is never
      called unless the human explicitly types ``yes`` at the prompt.
    - Returns the final session context dict so the caller (``main.py``) can
      display or persist the results.

    Attributes:
        _logger: Named logger for all Orchestrator-level log messages.
    """

    def __init__(self) -> None:
        """Initialises the Orchestrator.

        Loads the ``.env`` file so environment variables are available to every
        agent that is instantiated during ``run()``. Sets up the Orchestrator's
        own logger and initialises all six agent slots to ``None`` — agents are
        constructed lazily in ``run()`` to avoid circular imports.
        """
        # Load .env before doing anything else so GEMINI_API_KEY and
        # SERPER_API_KEY are in os.environ when agents are instantiated.
        load_dotenv()

        # Hierarchical logger name keeps Orchestrator messages separate from
        # agent messages while still being captured by a "dropsmart" filter.
        self._logger: logging.Logger = logging.getLogger("dropsmart.orchestrator")

        # --- Lazy agent slots ---
        # Initialised to None here; concrete instances are created in run().
        # Using None explicitly makes the intention visible and prevents
        # accidental calls to uninitialised agents.
        self._supplier_agent: Optional[Any] = None
        self._competitor_agent: Optional[Any] = None
        self._fee_agent: Optional[Any] = None
        self._margin_agent: Optional[Any] = None
        self._risk_agent: Optional[Any] = None
        self._report_agent: Optional[Any] = None

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self, user_input: dict[str, Any]) -> dict[str, Any]:
        """Executes the full DropSmart pipeline for a single product research run.

        Runs eight steps in order:
        1. Validate user input — raises ``ValueError`` on any bad value.
        2. Build the initial session context from validated input.
        3–7. Run each specialist agent in sequence, merging output into context.
        8. HITL checkpoint — pause and present summary; proceed only on approval.
        9. Run Report Generator Agent (only after approval).
        10. Return the completed session context.

        If an agent fails, the error is recorded in the context under
        ``"<step_name>_error"`` and the pipeline continues to the next step.
        This prevents a single agent failure from aborting the whole run — the
        seller still sees partial results and can make an informed HITL decision.

        Args:
            user_input: Dict of inputs collected from the user. Must contain
                at minimum: ``product_name``, ``marketplace``,
                ``business_model``, ``packaging_cost``. ``province`` is
                required when ``marketplace`` is ``"daraz_pk"``.

        Returns:
            The final session context dict. Always contains the original user
            inputs plus any agent outputs that completed successfully, plus
            ``"hitl_approved"`` (``True`` / ``False``) and ``"status"``
            (``"completed"`` or ``"cancelled"``).

        Raises:
            ValueError: If ``validate_input`` finds a missing or invalid field.
        """
        self._logger.info("Pipeline starting — product: %s", user_input.get("product_name"))

        # Step 1: Validate — abort before touching any agent if input is bad
        self.validate_input(user_input)

        # Step 2: Build the initial session context.
        # Derive region from marketplace so downstream agents and the MCP server
        # receive it without the user needing to supply it separately.
        context: dict[str, Any] = dict(user_input)
        context["region"] = _MARKETPLACE_REGIONS[user_input["marketplace"]]
        # Translate the descriptive business model name to the short alias that
        # the MCP server and specialist agents expect (e.g. "fbs", "fbm").
        context["business_model_alias"] = _BUSINESS_MODEL_ALIASES[
            user_input["business_model"]
        ]
        # Record HITL approval as False until the human explicitly approves
        context["hitl_approved"] = False
        context["status"] = "in_progress"

        # --- Lazy-import and instantiate all six specialist agents ---
        # Imports happen here rather than at the top of the file to prevent
        # circular imports: each agent file imports BaseAgent, and if
        # orchestrator.py imported them at module load time, the import chain
        # could resolve before BaseAgent is fully defined.
        from agents.supplier_agent import SupplierAgent
        from agents.competitor_agent import CompetitorAgent
        from agents.fee_agent import FeeAgent
        from agents.margin_agent import MarginAgent
        from agents.risk_agent import RiskAgent
        from agents.report_agent import ReportAgent

        self._supplier_agent = SupplierAgent()
        self._competitor_agent = CompetitorAgent()
        self._fee_agent = FeeAgent()
        self._margin_agent = MarginAgent()
        self._risk_agent = RiskAgent()
        self._report_agent = ReportAgent()

        # Steps 3–7: Run specialist agents in the order defined by the spec.
        # Each call merges the agent's output dict into context before the next
        # agent runs, so later agents see all earlier agents' findings.

        # Step 3 — Supplier Research
        supplier_result = self._run_agent(
            self._supplier_agent, context, step_name="supplier_research"
        )
        context.update(supplier_result)

        # Step 4 — Competitor Analysis
        competitor_result = self._run_agent(
            self._competitor_agent, context, step_name="competitor_analysis"
        )
        context.update(competitor_result)

        # Step 5 — Fee Structure Research
        fee_result = self._run_agent(
            self._fee_agent, context, step_name="fee_research"
        )
        context.update(fee_result)

        # Step 6 — Margin Calculation (no external calls — pure arithmetic)
        margin_result = self._run_agent(
            self._margin_agent, context, step_name="margin_calculation"
        )
        context.update(margin_result)

        # Step 7 — Risk Assessment
        risk_result = self._run_agent(
            self._risk_agent, context, step_name="risk_assessment"
        )
        context.update(risk_result)

        # Step 8: HITL checkpoint — pipeline pauses here.
        # The Report Agent is never called unless the human approves.
        approved: bool = self.hitl_checkpoint(context)
        context["hitl_approved"] = approved

        if not approved:
            # Record cancellation so callers can check status without inspecting
            # the hitl_approved flag themselves.
            context["status"] = "cancelled"
            self._logger.info(
                "Pipeline cancelled at HITL checkpoint — no report generated."
            )
            return context

        # Step 9 — Report Generation (runs only after explicit human approval)
        report_result = self._run_agent(
            self._report_agent, context, step_name="report_generation"
        )
        context.update(report_result)

        # Step 10: Mark the run as complete and return
        context["status"] = "completed"
        self._logger.info("Pipeline completed — product: %s", context.get("product_name"))
        return context

    # ------------------------------------------------------------------
    # Input validation
    # ------------------------------------------------------------------

    def validate_input(self, user_input: dict[str, Any]) -> None:
        """Validates all required user inputs before the pipeline starts.

        Validation runs before any agent is instantiated so that a bad input
        never reaches an agent, the MCP server, or any external API.

        Required keys for all runs:
        - ``product_name`` — non-empty string, max 200 characters.
        - ``marketplace`` — one of the four valid marketplace identifiers.
        - ``business_model`` — one of the three valid business model names.
        - ``packaging_cost`` — non-negative number (0 is valid).

        Conditional requirement:
        - ``province`` — required when ``marketplace`` is ``"daraz_pk"`` because
          the Daraz VAT rate differs between Punjab (16%) and all other provinces
          (15%). Accepting an unknown province would silently miscalculate margin.

        Args:
            user_input: The raw dict supplied by the caller.

        Raises:
            ValueError: With a clear, field-specific message for the first
                validation failure found. The pipeline should re-prompt for that
                field and call ``validate_input`` again rather than continuing.
        """
        # --- product_name ---
        product_name = user_input.get("product_name", "")
        if not isinstance(product_name, str) or not product_name.strip():
            raise ValueError(
                "product_name is required and must be a non-empty string."
            )
        if len(product_name.strip()) > 200:
            raise ValueError(
                "product_name must be 200 characters or fewer. "
                f"Got {len(product_name.strip())} characters."
            )

        # --- marketplace ---
        marketplace = user_input.get("marketplace", "")
        if marketplace not in _VALID_MARKETPLACES:
            raise ValueError(
                f"marketplace must be one of {sorted(_VALID_MARKETPLACES)}. "
                f"Got: {marketplace!r}"
            )

        # --- business_model ---
        business_model = user_input.get("business_model", "")
        if business_model not in _VALID_BUSINESS_MODELS:
            raise ValueError(
                f"business_model must be one of {sorted(_VALID_BUSINESS_MODELS)}. "
                f"Got: {business_model!r}"
            )

        # --- packaging_cost ---
        packaging_cost = user_input.get("packaging_cost")
        if packaging_cost is None:
            raise ValueError(
                "packaging_cost is required. Use 0 if packaging is included in "
                "the supplier cost or not applicable for this business model."
            )
        if not isinstance(packaging_cost, (int, float)) or packaging_cost < 0:
            raise ValueError(
                "packaging_cost must be a non-negative number. "
                f"Got: {packaging_cost!r}"
            )

        # --- province (Daraz Pakistan only) ---
        # Province determines whether VAT on fees is 15% (non-Punjab) or 16%
        # (Punjab). Accepting a run without this on Daraz would produce a
        # silently wrong margin figure — so we block it here.
        if marketplace == "daraz_pk":
            province = user_input.get("province", "")
            if not isinstance(province, str) or not province.strip():
                raise ValueError(
                    "province is required for Daraz Pakistan listings. "
                    "Provide the seller's province (e.g. 'Punjab', 'Sindh', 'KPK', 'Balochistan') "
                    "because VAT on fees differs by province: Punjab = 16%, all others = 15%."
                )

    # ------------------------------------------------------------------
    # Agent runner
    # ------------------------------------------------------------------

    def _run_agent(
        self,
        agent_instance: Any,
        context: dict[str, Any],
        step_name: str,
    ) -> dict[str, Any]:
        """Calls a specialist agent's ``run()`` method with logging and error isolation.

        Logs the start and end of each agent step so the run timeline is visible
        in the log stream. Catches any exception the agent raises, records it in
        the returned dict under ``"<step_name>_error"``, and returns so the
        pipeline can continue to the next agent rather than aborting.

        Returning an error dict rather than raising preserves partial results:
        the seller still sees whatever earlier agents found, which is more useful
        than a blank screen caused by one agent's failure.

        Args:
            agent_instance: An instantiated specialist agent that exposes a
                ``run(context: dict) -> dict`` method.
            context: The current cumulative session context dict. Passed to the
                agent unchanged — the agent must not mutate it in place.
            step_name: Short identifier for this step, used in log messages and
                as the error key prefix (e.g. ``"fee_research"``).

        Returns:
            The dict returned by ``agent_instance.run(context)`` on success, or
            ``{"<step_name>_error": "<error message>", "<step_name>_status": "failed"}``
            on failure.
        """
        self._logger.info("Step [%s] starting", step_name)

        try:
            result: dict[str, Any] = agent_instance.run(context)
            self._logger.info(
                "Step [%s] completed — keys returned: %s",
                step_name,
                list(result.keys()),
            )
            return result

        except Exception as exc:
            # Log at ERROR — a failing agent is a real problem, not a warning.
            # Include the full exception message so it's visible in the log
            # file without needing a traceback dump.
            self._logger.error("Step [%s] failed: %s", step_name, exc)

            # Return a minimal error dict rather than re-raising so subsequent
            # agents (and the HITL checkpoint) still execute with whatever data
            # was accumulated before this failure.
            return {
                f"{step_name}_error": str(exc),
                f"{step_name}_status": "failed",
            }

    # ------------------------------------------------------------------
    # HITL checkpoint
    # ------------------------------------------------------------------

    def hitl_checkpoint(self, context: dict[str, Any]) -> bool:
        """Presents the pipeline summary to the human and waits for approval.

        Prints a formatted summary table to stdout covering the product, marketplace,
        business model, estimated margin, overall risk level, and the top three
        specific risks. Then prompts the human to type ``yes`` or ``no``.

        The pipeline must not call the Report Generator Agent unless this method
        returns ``True``. This is the architectural HITL gate described in the spec.

        Args:
            context: The cumulative session context after all five specialist
                agents (Supplier through Risk) have run. The method reads from
                this dict using safe ``.get()`` calls with clear fallbacks so
                a failed upstream agent does not crash the checkpoint.

        Returns:
            ``True`` if the human types ``yes`` (case-insensitive).
            ``False`` for any other input, including ``no``, empty, or Ctrl-C.
        """
        # --- Extract values for the summary table ---
        # Use descriptive fallbacks so the seller can still make a decision even
        # if an upstream agent failed and its key is absent from context.

        product_name: str = str(context.get("product_name", "Unknown"))
        marketplace_id: str = str(context.get("marketplace", "Unknown"))
        marketplace_label: str = _MARKETPLACE_LABELS.get(marketplace_id, marketplace_id)

        business_model: str = str(context.get("business_model", "Unknown"))
        business_model_label: str = _BUSINESS_MODEL_LABELS.get(business_model, business_model)

        # Margin data — produced by the Margin Calculator Agent
        margin_result: dict = context.get("margin_result", {})
        margin_pct: str = (
            f"{margin_result.get('margin_pct')}%"
            if margin_result.get("margin_pct") is not None
            else "N/A (margin calculation failed)"
        )
        net_profit: str = str(margin_result.get("net_profit_per_unit", "N/A"))
        break_even: str = str(margin_result.get("break_even_price", "N/A"))

        # Risk data — produced by the Risk Assessor Agent
        risk_result: dict = context.get("risk_result", {})
        overall_risk: str = str(
            risk_result.get("overall_risk_level", "N/A (risk assessment failed)")
        )
        top_risks: list = risk_result.get("top_risks", [])

        # Print the separator and header first to visually separate the
        # summary from any preceding log output in the terminal.
        separator = "=" * 60

        print(f"\n{separator}")
        print("  DROPSMART — PRE-REPORT SUMMARY")
        print(f"{separator}")
        print(f"  Product          : {product_name}")
        print(f"  Marketplace      : {marketplace_label}")
        print(f"  Business Model   : {business_model_label}")
        print(separator)
        print("  MARGIN SUMMARY")
        print(f"  Estimated Margin : {margin_pct}")
        print(f"  Net Profit/Unit  : {net_profit}")
        print(f"  Break-Even Price : {break_even}")
        print(separator)
        print("  RISK ASSESSMENT")
        print(f"  Overall Risk     : {overall_risk}")

        if top_risks:
            print("  Top 3 Risks:")
            # Print at most 3 risks — the Risk Agent should supply exactly 3,
            # but slice defensively in case it returned more or fewer.
            for i, risk in enumerate(top_risks[:3], start=1):
                print(f"    {i}. {risk}")
        else:
            # If the risk agent failed, make that explicit rather than showing
            # nothing — the seller should know the risk data is incomplete.
            print("  Top 3 Risks      : N/A (risk assessment failed or incomplete)")

        print(separator)

        # --- Prompt for human decision ---
        # Strip and lower the response so "Yes", "YES", and "yes" all approve.
        try:
            answer: str = input(
                "\n  Proceed to generate the full report? (yes/no): "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            # EOFError occurs in non-interactive environments (e.g. piped input
            # that has ended); KeyboardInterrupt is Ctrl-C. Treat both as a
            # rejection so the pipeline does not accidentally generate a report.
            print("\n  [HITL] Input interrupted — treating as 'no'. Pipeline cancelled.")
            self._logger.warning("HITL checkpoint interrupted — pipeline cancelled.")
            return False

        if answer == "yes":
            self._logger.info("HITL checkpoint: human approved — Report Agent will run.")
            print(f"\n{separator}")
            print("  Approval recorded. Generating report...")
            print(f"{separator}\n")
            return True

        # Any non-"yes" response — including "no", empty string, or a typo —
        # is treated as rejection. Being strict here prevents an accidental
        # Enter keypress from triggering the report.
        self._logger.info(
            "HITL checkpoint: human rejected (input=%r) — pipeline cancelled.", answer
        )
        print(f"\n{separator}")
        print("  Cancelled. No report generated.")
        print(f"{separator}\n")
        return False
