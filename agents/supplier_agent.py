"""Supplier Research Agent for DropSmart.

Searches for real supplier options for the given product and business model,
then uses Gemini to extract structured supplier data from the raw search results.
This agent is always the first specialist to run in the pipeline.
"""

# --- Standard library ---
import datetime
import json
from typing import Any

# --- Local ---
from agents.base_agent import BaseAgent


# ---------------------------------------------------------------------------
# Business-model → search modifier mapping
# ---------------------------------------------------------------------------

# These strings are inserted into search queries to target the right supplier
# type for each model. They should never be hardcoded inside the query methods.
_BM_SEARCH_TERMS: dict[str, dict[str, str]] = {
    "dropshipping": {
        "supplier_type": "dropship supplier direct ship no MOQ",
        "qualifier": "blind shipping white label AliExpress Alibaba",
    },
    "fbs": {
        "supplier_type": "wholesale bulk supplier",
        "qualifier": "low MOQ bulk pricing Alibaba AliExpress",
    },
    "fbm": {
        "supplier_type": "wholesale supplier fulfillment center ready",
        "qualifier": "bulk packaging labeling compliant Alibaba",
    },
}

# When no business model alias is present in context, default to dropshipping
# terms rather than crashing — the error in context keys is a pipeline issue,
# not a supplier-search issue.
_DEFAULT_BM_KEY: str = "dropshipping"

# Maximum suppliers to include in the output — keeps the HITL summary readable
_MAX_SUPPLIERS: int = 5

# A search result's date string is considered stale if older than this many days
_FRESHNESS_THRESHOLD_DAYS: int = 30


class SupplierAgent(BaseAgent):
    """Finds real supplier options matched to the seller's product and business model.

    Calls the MCP server's ``web_search`` tool for each targeted query, aggregates
    the raw text results, then asks Gemini to extract structured supplier data.
    Returns up to five supplier entries with pricing, MOQ, shipping time,
    a reliability score, and a source URL.

    Business model awareness:
    - **dropshipping**: targets suppliers with MOQ of 1, direct-to-customer
      shipping, and blind/white-label capability.
    - **fbs**: targets bulk wholesale suppliers with reliable MOQ and lead-time
      data suitable for seller-held inventory.
    - **fbm**: targets suppliers that can ship compliance-ready bulk stock to
      marketplace fulfilment centres.
    """

    def __init__(self) -> None:
        """Initialises the SupplierAgent with its fixed agent name."""
        # The agent name is used as the logger prefix and in audit logs —
        # it must match the step name used by the Orchestrator ("supplier_agent").
        super().__init__("supplier_agent")

    # ------------------------------------------------------------------
    # Public pipeline interface
    # ------------------------------------------------------------------

    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """Searches for suppliers and returns structured supplier data.

        Reads ``product_name``, ``marketplace``, ``region``, and
        ``business_model_alias`` from the session context, runs targeted web
        searches via the MCP server, and asks Gemini to extract structured
        JSON from the raw results.

        Args:
            context: Cumulative session context from the Orchestrator. Must
                contain ``product_name``, ``marketplace``, and ``region``.
                ``business_model_alias`` is used when present (e.g. ``"fbs"``);
                falls back to ``business_model`` if absent.

        Returns:
            A dict with a single key ``"supplier_result"`` whose value is:

            .. code-block:: python

                {
                    "suppliers": [
                        {
                            "name": str,
                            "price_usd": float,      # or "price_pkr" for daraz_pk
                            "moq": int,
                            "shipping_days": str,
                            "shipping_cost": float,  # 0.0 if not found in results
                            "reliability_score": int,  # 1–10
                            "risk_label": str,       # "LOW"/"MEDIUM"/"HIGH"/"UNKNOWN"
                            "source_url": str,
                            "data_retrieved_date": str  # ISO 8601 date
                        },
                        ...  # up to 5 entries
                    ],
                    "recommended_supplier": str,
                    "search_queries_used": list[str],
                    "data_freshness_warning": bool
                }

            If all searches fail, ``"suppliers"`` is an empty list and
            ``"recommended_supplier"`` is ``"N/A — no search results returned"``.
        """
        product = context.get("product_name", "").strip()
        marketplace = context.get("marketplace", "").strip()
        region = context.get("region", "").strip()

        # Prefer the short alias set by the Orchestrator (e.g. "fbs"); fall back
        # to the full business_model value if the alias key is absent.
        business_model = context.get(
            "business_model_alias",
            context.get("business_model", _DEFAULT_BM_KEY),
        ).strip()

        self._log_start("supplier research")

        # Lazy import: importing at the top of this module would execute the MCP
        # server's module-level code (including the SERPER_API_KEY check) before
        # the Orchestrator has had a chance to call load_dotenv(). Importing here
        # guarantees the environment is already loaded.
        from mcp_server.search_mcp import web_search

        # --- Build queries ---
        queries: list[str] = self._get_search_queries(context)
        all_raw_results: list[dict[str, Any]] = []
        queries_that_ran: list[str] = []

        # --- Execute each query via the MCP server ---
        for query in queries:
            self._logger.info("Supplier search query: %s", query)
            try:
                results = web_search(query=query, num_results=8)
                all_raw_results.extend(results)
                queries_that_ran.append(query)
            except Exception as exc:
                # A single failed query should not abort the whole search —
                # partial results from the remaining queries are better than nothing.
                self._logger.warning(
                    "Query failed (skipping): %r — %s", query, exc
                )

        # --- Handle the no-results case gracefully ---
        if not all_raw_results:
            self._logger.warning(
                "All supplier search queries returned no results for product: %r", product
            )
            self._log_end("supplier research", success=False)
            return {
                "supplier_result": {
                    "suppliers": [],
                    "recommended_supplier": "N/A — no search results returned",
                    "search_queries_used": queries_that_ran,
                    "data_freshness_warning": False,
                }
            }

        # --- Check freshness before passing to Gemini ---
        # Warn if any result's date is older than the threshold so the seller
        # knows the pricing data may not reflect current market conditions.
        freshness_warning: bool = self._check_freshness(all_raw_results)

        # --- Build the Gemini prompt and call it ---
        search_results_text: str = self._format_results_for_prompt(all_raw_results)
        prompt: str = self._build_supplier_prompt(context, search_results_text)

        raw_response: str = self._safe_generate(prompt)

        # --- Parse the JSON Gemini returns ---
        # Unpack both fields — discarding gemini_recommendation here was the bug;
        # _pick_recommended_supplier needs it to prefer Gemini's explicit choice.
        suppliers, gemini_recommendation = self._parse_gemini_response(
            raw_response=raw_response,
            marketplace=marketplace,
        )

        # --- Attach risk_label to each supplier in pure Python ---
        # Done here rather than in _parse_gemini_response so it is always applied
        # regardless of which code path populates the suppliers list.
        for supplier in suppliers:
            score: float = float(supplier.get("reliability_score", 0))
            supplier["risk_label"] = self._derive_risk_label(score)

        # --- Determine the recommended supplier ---
        recommended: str = self._pick_recommended_supplier(
            suppliers, gemini_recommendation
        )

        self._log_end("supplier research", success=True)

        return {
            "supplier_result": {
                "suppliers": suppliers[:_MAX_SUPPLIERS],
                "recommended_supplier": recommended,
                "search_queries_used": queries_that_ran,
                "data_freshness_warning": freshness_warning,
            }
        }

    # ------------------------------------------------------------------
    # Query builder
    # ------------------------------------------------------------------

    def _get_search_queries(self, context: dict[str, Any]) -> list[str]:
        """Builds 2–3 targeted supplier search queries for the given product.

        For Pakistan (Daraz) uses keyword-based queries targeting local and
        China-to-PK wholesale routes. For all other marketplaces uses
        ``site:``-restricted queries that point directly at Alibaba or
        AliExpress listing pages — these pages frequently contain price ranges
        in their indexed titles and snippets, giving Gemini richer pricing
        signals than generic "best suppliers" articles.

        Args:
            context: Session context containing ``product_name``, ``marketplace``,
                and ``business_model_alias`` (or ``business_model``).

        Returns:
            A list of 2–3 search query strings, each ready to be passed to
            the MCP server's ``web_search`` tool.
        """
        product: str = context.get("product_name", "product").strip()
        marketplace: str = context.get("marketplace", "").strip()
        business_model: str = context.get(
            "business_model_alias",
            context.get("business_model", _DEFAULT_BM_KEY),
        ).strip()

        # Resolve search term modifiers for Pakistan queries only — USA queries
        # use site:-restricted patterns that don't need generic BM qualifiers.
        bm_terms = _BM_SEARCH_TERMS.get(business_model, _BM_SEARCH_TERMS[_DEFAULT_BM_KEY])
        supplier_type: str = bm_terms["supplier_type"]
        qualifier: str = bm_terms["qualifier"]

        current_year: int = datetime.date.today().year
        queries: list[str] = []

        if marketplace == "daraz_pk":
            # Pakistan-specific queries favour local and China-to-PK wholesale routes
            queries.append(
                f"wholesale {product} supplier Pakistan MOQ 1 {current_year}"
            )
            queries.append(
                f"dropship {product} Pakistan supplier price {current_year} {supplier_type}"
            )
            queries.append(
                f"{product} {supplier_type} Pakistan price per unit {qualifier}"
            )
        else:
            # site:-restricted queries target actual product listing pages on the
            # two largest B2B/dropshipping platforms. These pages embed price ranges
            # in indexed titles/snippets (e.g. "US $1.20–$2.50 / piece") so Gemini
            # receives real price data rather than general-purpose "how to source"
            # article content.
            if business_model == "dropshipping":
                queries.append(f"site:aliexpress.com {product} price")
                queries.append(f"site:aliexpress.com {product} wholesale no MOQ")
            elif business_model == "fbs":
                queries.append(f"site:alibaba.com {product} wholesale price MOQ")
                queries.append(f"site:aliexpress.com {product} bulk price")
            else:
                # fbm — warehouse-ready bulk stock
                queries.append(f"site:alibaba.com {product} wholesale bulk packaging")
                queries.append(f"site:aliexpress.com {product} supplier price")

        return queries

    # ------------------------------------------------------------------
    # Prompt builder
    # ------------------------------------------------------------------

    def _build_supplier_prompt(
        self, context: dict[str, Any], search_results: str
    ) -> str:
        """Constructs the Gemini prompt that extracts structured supplier data.

        The prompt instructs Gemini to act as a structured data extractor —
        never an inventor. It must return only valid JSON that matches the
        output schema, using only supplier information actually present in
        the search results.

        For site:-restricted results from Alibaba/AliExpress, prices often
        appear as ranges (e.g. "US $1.20–$2.50"). The prompt instructs Gemini
        to use the lower bound and flag the entry with
        ``price_is_range_lower_bound: true``.

        Args:
            context: Session context providing product, marketplace, and
                business model for framing the prompt correctly.
            search_results: Pre-formatted string of all search result
                titles, snippets, and URLs, ready to be embedded in the prompt.

        Returns:
            A complete prompt string ready to be passed to ``_safe_generate``.
        """
        product: str = context.get("product_name", "the product")
        marketplace: str = context.get("marketplace", "")
        business_model: str = context.get(
            "business_model_alias",
            context.get("business_model", _DEFAULT_BM_KEY),
        )

        today: str = datetime.date.today().isoformat()

        # Choose the price key name to match the marketplace currency so the
        # Margin Agent can identify it without a currency-lookup step.
        price_key: str = "price_pkr" if marketplace == "daraz_pk" else "price_usd"

        # Business model instructions tell Gemini what signals matter most for
        # the seller's sourcing decision — different models have different needs.
        bm_instructions: dict[str, str] = {
            "dropshipping": (
                "Prefer suppliers with MOQ of 1 or 'no minimum'. "
                "Flag suppliers that mention direct-to-customer shipping, blind shipping, "
                "or white-label capability as highly relevant."
            ),
            "fbs": (
                "Prefer suppliers with clear bulk pricing tiers and stated MOQ. "
                "Flag suppliers that mention reliable lead times and consistent stock as highly relevant."
            ),
            "fbm": (
                "Prefer suppliers that mention warehouse-ready bulk packaging, "
                "FBA/WFS labeling compliance, or fulfillment-centre delivery as highly relevant."
            ),
        }
        bm_instruction: str = bm_instructions.get(
            business_model, bm_instructions["dropshipping"]
        )

        return f"""You are a supplier data extractor for an e-commerce research tool.

Your task: extract structured supplier information from the search results below for the product "{product}".

TARGET MARKETPLACE: {marketplace}
BUSINESS MODEL: {business_model}
BUSINESS MODEL PRIORITY: {bm_instruction}

CRITICAL RULES — follow these exactly:
1. Return ONLY valid JSON. No markdown, no code fences, no explanation text before or after.
2. Never invent, assume, or fabricate a supplier that is not mentioned in the search results.
3. If price data looks older than 6 months based on dates in the results, set "price_may_be_outdated" to true on that supplier entry.
4. reliability_score is an integer 1–10: use signals like verified badges, years in business, review counts, ratings found in snippets. Default to 5 if no signal is present.
5. Use "{price_key}" as the price field name (not "price_usd" or "price_pkr").
6. shipping_days should be a string range e.g. "7–14" or "3–5". Use "unknown" if not found.
7. moq should be an integer. Use 1 if "no minimum" or "MOQ 1" is stated. Use 0 if truly unknown.
8. source_url must come directly from the search result URLs — never construct a URL.
9. data_retrieved_date for every supplier must be exactly: {today}
10. Include at most 5 suppliers. Rank them by relevance to the business model priority above.
11. shipping_cost: extract the per-shipment or per-unit shipping cost as a float when
    mentioned in the snippets (e.g. "shipping $2" → 2.0, "PKR 180 shipping" → 180.0,
    "free shipping" → 0.0). Use 0.0 if no shipping cost is mentioned — never invent a value.
12. Price data from site:alibaba.com and site:aliexpress.com often appears as a range
    (e.g. "US $1.20–$2.50 / piece", "PKR 180–300 per unit"). When you see a range,
    use the LOWER bound as the {price_key} value — this is the wholesale entry price
    at minimum order. Set "price_is_range_lower_bound": true on that supplier entry.

SEARCH RESULTS:
{search_results}

Return a JSON object in exactly this structure:
{{
  "suppliers": [
    {{
      "name": "Supplier Name",
      "{price_key}": 0.00,
      "moq": 0,
      "shipping_days": "7-14",
      "shipping_cost": 0.0,
      "reliability_score": 5,
      "source_url": "https://...",
      "data_retrieved_date": "{today}",
      "price_may_be_outdated": false,
      "price_is_range_lower_bound": false
    }}
  ],
  "recommended_supplier": "Name of the single best supplier for this business model, or N/A"
}}"""

    # ------------------------------------------------------------------
    # Result formatting helpers
    # ------------------------------------------------------------------

    def _format_results_for_prompt(
        self, results: list[dict[str, Any]]
    ) -> str:
        """Formats raw search result dicts into a readable block for the prompt.

        Gemini performs better when results are presented as labelled
        text blocks rather than raw JSON — this avoids prompt confusion between
        the input data and the JSON output format.

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

    def _check_freshness(self, results: list[dict[str, Any]]) -> bool:
        """Returns True if any result has a date older than the freshness threshold.

        A freshness warning prompts the Orchestrator's HITL checkpoint to display
        a note to the seller — they should verify pricing manually if data is stale.

        Args:
            results: List of search result dicts from the MCP server.

        Returns:
            ``True`` if at least one result has a parseable date older than
            ``_FRESHNESS_THRESHOLD_DAYS`` days. ``False`` if all dated results
            are recent, or if no result has a parseable date.
        """
        today: datetime.date = datetime.date.today()
        threshold: datetime.date = today - datetime.timedelta(days=_FRESHNESS_THRESHOLD_DAYS)

        for result in results:
            date_str: str = result.get("date", "")
            if not date_str:
                continue
            # Attempt to parse common date formats Serper returns.
            # Multiple format attempts because Serper is not consistent.
            for fmt in ("%b %d, %Y", "%Y-%m-%d", "%d %b %Y"):
                try:
                    parsed: datetime.date = datetime.datetime.strptime(date_str, fmt).date()
                    if parsed < threshold:
                        return True
                    break  # parsed successfully — no need to try other formats
                except ValueError:
                    continue

        return False

    def _parse_gemini_response(
        self,
        raw_response: str,
        marketplace: str,
    ) -> tuple[list[dict[str, Any]], str]:
        """Parses the JSON string returned by Gemini into suppliers and a recommendation.

        Gemini occasionally wraps JSON in markdown code fences despite being told
        not to — this method strips those fences defensively before parsing.
        Both the ``suppliers`` list and the ``recommended_supplier`` string are
        extracted from the same parse so neither field is silently discarded.
        If parsing fails entirely, logs the error and returns safe empty defaults
        so the pipeline can continue with a partial result rather than crashing.

        Args:
            raw_response: The raw string returned by ``_safe_generate``.
            marketplace: The target marketplace identifier, used only in the
                warning log message for context.

        Returns:
            A two-element tuple:
            - ``suppliers``: list of supplier dicts conforming to the output
              schema, or an empty list if the response could not be parsed.
            - ``gemini_recommendation``: the value of the ``recommended_supplier``
              key from Gemini's JSON, or an empty string if absent or unparseable.
        """
        # Strip any markdown code fences Gemini may have added despite instructions
        cleaned: str = raw_response.strip()
        if cleaned.startswith("```"):
            # Remove opening fence and optional language label (e.g. ```json)
            cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned.rsplit("```", 1)[0]
        cleaned = cleaned.strip()

        try:
            parsed: dict[str, Any] = json.loads(cleaned)
            suppliers: list[dict[str, Any]] = parsed.get("suppliers", [])

            # Ensure data_retrieved_date is always set — Gemini might omit it
            # despite the prompt instruction, so we enforce it here.
            today_str: str = datetime.date.today().isoformat()
            for supplier in suppliers:
                if not supplier.get("data_retrieved_date"):
                    supplier["data_retrieved_date"] = today_str

            # Extract the recommendation alongside the suppliers list so it is
            # not discarded before _pick_recommended_supplier can use it.
            gemini_recommendation: str = parsed.get("recommended_supplier", "")

            return suppliers, gemini_recommendation

        except json.JSONDecodeError as exc:
            self._logger.error(
                "Failed to parse Gemini response as JSON for marketplace=%r: %s\n"
                "Raw response (first 300 chars): %s",
                marketplace,
                exc,
                raw_response[:300],
            )
            return [], ""

    def _pick_recommended_supplier(
        self,
        suppliers: list[dict[str, Any]],
        gemini_recommendation: str,
    ) -> str:
        """Selects the recommended supplier name from available sources.

        Priority order:
        1. ``gemini_recommendation`` — Gemini evaluated all results against the
           business model priority and chose this supplier; trust it when present.
        2. ``suppliers[0]["name"]`` — first supplier in Gemini's ranked list when
           the recommendation field is absent or empty.
        3. A not-found message when the suppliers list is also empty.

        Args:
            suppliers: The parsed list of supplier dicts from Gemini's response.
                May be empty if all searches failed or parsing failed.
            gemini_recommendation: The ``recommended_supplier`` string extracted
                directly from Gemini's JSON by ``_parse_gemini_response``.
                Empty string when absent or when parsing failed.

        Returns:
            The name of the recommended supplier as a plain string.
        """
        # Use Gemini's explicit recommendation first — it was produced with full
        # context of the business model priority instructions in the prompt.
        if gemini_recommendation and gemini_recommendation.strip().lower() != "n/a":
            return gemini_recommendation.strip()

        # Fall back to the top-ranked supplier entry when the recommendation
        # field was missing or contained only "N/A".
        if suppliers:
            return suppliers[0].get("name", "N/A")

        return "N/A — no suppliers found in search results"

    # ------------------------------------------------------------------
    # Risk label helper
    # ------------------------------------------------------------------

    def _derive_risk_label(self, reliability_score: float) -> str:
        """Maps a supplier reliability score to a human-readable risk label.

        Computed in pure Python after Gemini extraction — the label is never
        asked of Gemini because it is a deterministic function of the numeric
        score, and asking Gemini would introduce unnecessary variance.

        Mapping:
        - score >= 8 → ``"LOW"``   (reliable, well-reviewed supplier)
        - score 5–7  → ``"MEDIUM"`` (adequate, monitor quality)
        - score 1–4  → ``"HIGH"``   (low confidence in fulfilment)
        - score == 0 → ``"UNKNOWN"`` (no signal found in search results)

        Args:
            reliability_score: The numeric score 0–10 extracted by Gemini
                from supplier snippet signals (badges, reviews, tenure).
                0 means no signal was found.

        Returns:
            One of ``"LOW"``, ``"MEDIUM"``, ``"HIGH"``, or ``"UNKNOWN"``.
        """
        if reliability_score == 0:
            return "UNKNOWN"
        if reliability_score >= 8:
            return "LOW"
        if reliability_score >= 5:
            return "MEDIUM"
        return "HIGH"
