"""Competitor Analysis Agent for DropSmart.

Searches for live competitor listings on the target marketplace, then uses
Gemini to extract structured pricing, keyword, and saturation data. Always
runs after the Supplier Agent and before the Fee Agent in the pipeline.
"""

# --- Standard library ---
import datetime
import json
from typing import Any

# --- Local ---
from agents.base_agent import BaseAgent


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# site: search operators restrict results to the target marketplace domain so
# Gemini receives actual listing data rather than review blogs or aggregators.
_MARKETPLACE_DOMAINS: dict[str, str] = {
    "daraz_pk": "daraz.pk",
    "walmart_us": "walmart.com",
    "amazon_us": "amazon.com",
    "etsy_us": "etsy.com",
}

# Human-readable marketplace names used inside search query strings.
_MARKETPLACE_LABELS: dict[str, str] = {
    "daraz_pk": "Daraz Pakistan",
    "walmart_us": "Walmart",
    "amazon_us": "Amazon",
    "etsy_us": "Etsy",
}

# Review-count thresholds used to classify market saturation from the data
# Gemini extracts. Thresholds are intentionally conservative — a small number
# of very highly-reviewed listings signals a saturated market just as clearly
# as many listings with moderate reviews.
_SATURATION_HIGH_REVIEW_THRESHOLD: int = 1000   # any single listing above this → high
_SATURATION_MEDIUM_REVIEW_THRESHOLD: int = 100   # any single listing above this → medium

# Maximum listings to include in the output — keeps the HITL summary scannable
_MAX_LISTINGS: int = 5

# Number of keywords to request from Gemini — must match the prompt instruction
_KEYWORD_COUNT: int = 5


class CompetitorAgent(BaseAgent):
    """Analyses the live competitor landscape on the target marketplace.

    Calls the MCP server's ``web_search`` tool with site-restricted queries
    to fetch real listing data, then asks Gemini to extract structured
    competitor information. Returns pricing stats, top keywords, and a
    market saturation signal that the Risk Assessor uses downstream.

    The saturation classification is computed from review counts found in the
    search results — not from a hardcoded rule — so it reflects actual
    marketplace conditions at the time of the run.
    """

    def __init__(self) -> None:
        """Initialises the CompetitorAgent with its fixed agent name."""
        # Agent name appears in logger prefixes and orchestrator step logs —
        # keep it consistent with the step_name used in orchestrator.py.
        super().__init__("competitor_agent")

    # ------------------------------------------------------------------
    # Public pipeline interface
    # ------------------------------------------------------------------

    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """Searches for competitor listings and returns structured market data.

        Reads ``product_name``, ``marketplace``, and ``region`` from the session
        context, runs targeted site-restricted queries via the MCP server, and
        asks Gemini to extract competitor listing data as structured JSON.

        Args:
            context: Cumulative session context from the Orchestrator. Must
                contain ``product_name`` and ``marketplace``. ``region`` is
                used in logging but not in query construction.

        Returns:
            A dict with a single key ``"competitor_result"`` whose value is:

            .. code-block:: python

                {
                    "listings": [
                        {
                            "title": str,
                            "price": float,
                            "currency": str,         # "PKR" or "USD"
                            "rating": float,         # 0.0 if not found
                            "review_count": int,     # 0 if not found
                            "seller_name": str,
                            "source_url": str,
                            "data_retrieved_date": str  # ISO 8601 date
                        },
                        ...  # up to 5 entries
                    ],
                    "avg_market_price": float,
                    "price_range": {"min": float, "max": float},
                    "top_keywords": list[str],       # 5 keywords from titles
                    "market_saturation": str,        # "low", "medium", or "high"
                    "search_queries_used": list[str]
                }

            If all searches fail, ``"listings"`` is an empty list and numeric
            fields are ``0.0`` so downstream agents can still run.
        """
        product: str = context.get("product_name", "").strip()
        marketplace: str = context.get("marketplace", "").strip()

        self._log_start("competitor analysis")

        # Lazy import mirrors the supplier_agent.py pattern — avoids executing
        # the MCP server's module-level SERPER_API_KEY check before load_dotenv()
        # has been called by the Orchestrator.
        from mcp_server.search_mcp import web_search

        # --- Build and run queries ---
        queries: list[str] = self._get_search_queries(context)
        all_raw_results: list[dict[str, Any]] = []
        queries_that_ran: list[str] = []

        for query in queries:
            self._logger.info("Competitor search query: %s", query)
            try:
                results = web_search(query=query, num_results=8)
                all_raw_results.extend(results)
                queries_that_ran.append(query)
            except Exception as exc:
                # Skip failing queries rather than aborting — partial data from
                # the remaining queries is still enough for a useful analysis.
                self._logger.warning(
                    "Query failed (skipping): %r — %s", query, exc
                )

        # --- Handle the no-results case gracefully ---
        if not all_raw_results:
            self._logger.warning(
                "All competitor queries returned no results for product: %r on %r",
                product,
                marketplace,
            )
            self._log_end("competitor analysis", success=False)
            return {
                "competitor_result": self._empty_result(queries_that_ran)
            }

        # --- Ask Gemini to extract structured data from raw search text ---
        search_results_text: str = self._format_results_for_prompt(all_raw_results)
        prompt: str = self._build_competitor_prompt(context, search_results_text)
        raw_response: str = self._safe_generate(prompt)

        # --- Parse the JSON Gemini returns ---
        gemini_data: dict[str, Any] = self._parse_gemini_response(raw_response)

        if not gemini_data:
            # Parsing failed — return the empty scaffold so the pipeline continues
            self._log_end("competitor analysis", success=False)
            return {
                "competitor_result": self._empty_result(queries_that_ran)
            }

        # --- Post-process: compute aggregates and attach metadata ---
        listings: list[dict[str, Any]] = gemini_data.get("listings", [])[:_MAX_LISTINGS]
        today_str: str = datetime.date.today().isoformat()

        # Enforce data_retrieved_date on every listing — Gemini may omit it
        for listing in listings:
            if not listing.get("data_retrieved_date"):
                listing["data_retrieved_date"] = today_str

        # Compute price aggregates from the parsed listings rather than asking
        # Gemini to do arithmetic — Python arithmetic is more reliable here.
        avg_price, price_range = self._compute_price_stats(listings)

        # Classify saturation from review counts in the parsed listings —
        # Gemini was asked to extract review counts, so use them here directly.
        saturation: str = self._classify_saturation(listings)

        # Pull top_keywords from Gemini's response; fall back to empty list
        top_keywords: list[str] = gemini_data.get("top_keywords", [])[:_KEYWORD_COUNT]

        self._log_end("competitor analysis", success=True)

        return {
            "competitor_result": {
                "listings": listings,
                "avg_market_price": avg_price,
                "price_range": price_range,
                "top_keywords": top_keywords,
                "market_saturation": saturation,
                "search_queries_used": queries_that_ran,
            }
        }

    # ------------------------------------------------------------------
    # Query builder
    # ------------------------------------------------------------------

    def _get_search_queries(self, context: dict[str, Any]) -> list[str]:
        """Builds 2–3 site-restricted search queries targeting competitor listings.

        Using a ``site:`` operator confines results to the target marketplace
        so Gemini receives actual listing pages rather than price comparison
        sites, review blogs, or unrelated e-commerce stores.

        Args:
            context: Session context containing ``product_name`` and
                ``marketplace``.

        Returns:
            A list of 2–3 search query strings ready to be passed to the
            MCP server's ``web_search`` tool, ordered from most specific
            to broadest.
        """
        product: str = context.get("product_name", "product").strip()
        marketplace: str = context.get("marketplace", "").strip()

        domain: str = _MARKETPLACE_DOMAINS.get(marketplace, "")
        label: str = _MARKETPLACE_LABELS.get(marketplace, marketplace)

        queries: list[str] = []

        if domain:
            # Query 1: site-restricted listing search — highest-signal results
            queries.append(f"site:{domain} {product} price reviews")

            # Query 2: add "buy" to bias toward transactional listing pages
            # rather than editorial or comparison content within the same domain
            queries.append(f"site:{domain} buy {product} best seller")

        # Query 3: marketplace label without site: catches any indexed subdomains
        # or microsites the marketplace uses (e.g. Amazon's different TLDs)
        queries.append(
            f"{label} {product} price reviews top seller listings"
        )

        return queries

    # ------------------------------------------------------------------
    # Prompt builder
    # ------------------------------------------------------------------

    def _build_competitor_prompt(
        self, context: dict[str, Any], search_results: str
    ) -> str:
        """Constructs the Gemini prompt that extracts structured competitor data.

        The prompt instructs Gemini to act as a data extractor only —
        never to invent listings, prices, or keywords that are not present
        in the search results. Keywords must come from actual listing titles
        found in the results, not from general product knowledge.

        Args:
            context: Session context providing product, marketplace, and
                region for framing the extraction correctly.
            search_results: Pre-formatted string of all search result
                titles, snippets, and URLs, ready to be embedded in the prompt.

        Returns:
            A complete prompt string ready to be passed to ``_safe_generate``.
        """
        product: str = context.get("product_name", "the product")
        marketplace: str = context.get("marketplace", "")
        today: str = datetime.date.today().isoformat()

        # Currency depends on marketplace — the Margin Agent reads "currency"
        # from each listing to format the output correctly.
        currency: str = "PKR" if marketplace == "daraz_pk" else "USD"

        return f"""You are a competitor listing data extractor for an e-commerce research tool.

Your task: extract structured competitor listing data from the search results below for the product "{product}" on {marketplace}.

CRITICAL RULES — follow these exactly:
1. Return ONLY valid JSON. No markdown, no code fences, no explanation text before or after.
2. Never invent, assume, or fabricate a listing that is not present in the search results.
3. Extract keywords ONLY from the actual listing titles found in the results — not from general knowledge about the product.
4. Use "{currency}" as the currency value for all listings.
5. price must be a float. Use 0.0 if no price is found for a listing.
6. rating must be a float between 0.0 and 5.0. Use 0.0 if not found.
7. review_count must be an integer. Use 0 if not found.
8. seller_name: use the domain or store name visible in the URL or snippet. Use "Unknown" if not found.
9. source_url must come directly from the search result URLs — never construct a URL.
10. data_retrieved_date for every listing must be exactly: {today}
11. top_keywords: extract exactly {_KEYWORD_COUNT} high-frequency keywords or phrases from the listing titles found. These must appear in the actual titles — do not generate them.
12. Include at most {_MAX_LISTINGS} listings, ranked by estimated popularity (review count descending).

SEARCH RESULTS:
{search_results}

Return a JSON object in exactly this structure:
{{
  "listings": [
    {{
      "title": "Listing title as found",
      "price": 0.00,
      "currency": "{currency}",
      "rating": 0.0,
      "review_count": 0,
      "seller_name": "Seller or store name",
      "source_url": "https://...",
      "data_retrieved_date": "{today}"
    }}
  ],
  "top_keywords": ["keyword1", "keyword2", "keyword3", "keyword4", "keyword5"]
}}"""

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_gemini_response(self, raw_response: str) -> dict[str, Any]:
        """Parses the JSON string returned by Gemini into a competitor data dict.

        Defensively strips markdown code fences that Gemini may add despite
        being told not to. Returns an empty dict on any parse failure so the
        caller can detect the failure and return the empty scaffold rather
        than propagating an exception.

        Args:
            raw_response: The raw string returned by ``_safe_generate``.

        Returns:
            The parsed dict containing ``listings`` and ``top_keywords`` keys,
            or an empty dict ``{}`` if the response is not valid JSON.
        """
        # Strip whitespace and any markdown fences around the JSON block
        cleaned: str = raw_response.strip()
        if cleaned.startswith("```"):
            # Drop the opening fence line (e.g. "```json\n" or "```\n")
            cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned.rsplit("```", 1)[0]
        cleaned = cleaned.strip()

        try:
            parsed: dict[str, Any] = json.loads(cleaned)
            return parsed

        except json.JSONDecodeError as exc:
            self._logger.error(
                "Failed to parse Gemini response as JSON: %s\n"
                "Raw response (first 300 chars): %s",
                exc,
                raw_response[:300],
            )
            return {}

    # ------------------------------------------------------------------
    # Aggregate helpers
    # ------------------------------------------------------------------

    def _compute_price_stats(
        self, listings: list[dict[str, Any]]
    ) -> tuple[float, dict[str, float]]:
        """Computes average price and price range from a list of listings.

        Excludes listings with a price of 0.0 from the calculation because 0.0
        means "not found" — including those zeroes would drag the average down
        and produce a misleading range minimum.

        Args:
            listings: List of listing dicts, each expected to have a ``price``
                key as a float.

        Returns:
            A two-element tuple:
            - ``avg_price``: mean price across non-zero listings, or ``0.0``
              if no valid prices exist.
            - ``price_range``: dict with ``min`` and ``max`` keys, both ``0.0``
              when no valid prices exist.
        """
        # Filter to only listings that have a real (non-zero) price
        valid_prices: list[float] = [
            float(listing.get("price", 0.0))
            for listing in listings
            if float(listing.get("price", 0.0)) > 0.0
        ]

        if not valid_prices:
            return 0.0, {"min": 0.0, "max": 0.0}

        avg_price: float = round(sum(valid_prices) / len(valid_prices), 2)
        price_range: dict[str, float] = {
            "min": round(min(valid_prices), 2),
            "max": round(max(valid_prices), 2),
        }
        return avg_price, price_range

    def _classify_saturation(self, listings: list[dict[str, Any]]) -> str:
        """Classifies market saturation based on review counts in the listings.

        Uses the maximum review count found across all listings as the signal:
        a single listing with thousands of reviews indicates an established,
        competitive market just as reliably as many listings with lower counts.

        Saturation levels:
        - ``"high"``: any listing has more than ``_SATURATION_HIGH_REVIEW_THRESHOLD``
          reviews — strong incumbents present.
        - ``"medium"``: any listing has more than ``_SATURATION_MEDIUM_REVIEW_THRESHOLD``
          reviews — moderate competition.
        - ``"low"``: all listings have few reviews — relatively open market.

        Args:
            listings: List of listing dicts from Gemini's parsed response. Each
                is expected to have a ``review_count`` key as an integer.

        Returns:
            One of ``"low"``, ``"medium"``, or ``"high"``.
        """
        if not listings:
            # No data means we cannot assess saturation — default to low to
            # avoid falsely blocking the pipeline with a worst-case assumption.
            return "low"

        max_reviews: int = max(
            int(listing.get("review_count", 0)) for listing in listings
        )

        if max_reviews > _SATURATION_HIGH_REVIEW_THRESHOLD:
            return "high"
        if max_reviews > _SATURATION_MEDIUM_REVIEW_THRESHOLD:
            return "medium"
        return "low"

    # ------------------------------------------------------------------
    # Scaffold helpers
    # ------------------------------------------------------------------

    def _format_results_for_prompt(
        self, results: list[dict[str, Any]]
    ) -> str:
        """Formats raw search result dicts into a readable block for the prompt.

        Presenting results as labelled text blocks (rather than raw JSON)
        reduces the chance that Gemini confuses the input data structure with
        the JSON output format it is asked to produce.

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

    def _empty_result(self, queries_that_ran: list[str]) -> dict[str, Any]:
        """Returns a safe empty scaffold when search or parsing produces no data.

        Downstream agents (Margin, Risk) read numeric fields from
        ``competitor_result``. Returning 0.0 / empty list / "low" keeps
        those agents runnable even when competitor data is absent — the
        HITL checkpoint will surface the missing data to the seller.

        Args:
            queries_that_ran: Queries that were attempted before the failure,
                preserved for audit / debugging at the HITL checkpoint.

        Returns:
            A ``competitor_result`` dict with all required keys set to safe
            zero/empty defaults.
        """
        return {
            "listings": [],
            "avg_market_price": 0.0,
            "price_range": {"min": 0.0, "max": 0.0},
            "top_keywords": [],
            "market_saturation": "low",
            "search_queries_used": queries_that_ran,
        }
