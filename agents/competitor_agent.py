"""Competitor Analysis Agent for DropSmart.

Searches for live competitor listings on the target marketplace, then uses
Gemini (two calls, no more) to extract structured pricing and trend data.

Call 1 — Listing extraction: extracts listings, keywords, market leader, and
          total listing count from site-restricted search results.
Call 2 — Trend synthesis: a separate set of trend/seasonality queries feeds
          a focused Gemini prompt that returns only direction, product type,
          peak months, and demand signal.

Performance metrics and sweet-spot price range are derived from the already-
extracted listings in pure Python — no third Gemini call is made.
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

# site: operators confine results to the target marketplace domain so Gemini
# receives actual listing data rather than review blogs or price aggregators.
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

# Region labels for trend queries — more specific than the marketplace label.
_MARKETPLACE_REGIONS: dict[str, str] = {
    "daraz_pk": "Pakistan",
    "walmart_us": "USA",
    "amazon_us": "USA",
    "etsy_us": "USA",
}

# Review-count thresholds for saturation classification.
# A single heavily-reviewed listing signals saturation as reliably as many.
_SATURATION_HIGH_REVIEW_THRESHOLD: int = 1000
_SATURATION_MEDIUM_REVIEW_THRESHOLD: int = 100

# Active listing count thresholds that ALSO feed the saturation signal.
# Used in conjunction with review counts — either condition triggers the level.
_SATURATION_HIGH_LISTING_THRESHOLD: int = 500
_SATURATION_MEDIUM_LISTING_THRESHOLD: int = 100

# Cap on listings kept in the output — keeps the HITL summary scannable.
_MAX_LISTINGS: int = 5

# Target keyword count requested from Gemini (spec: 5-8).
_KEYWORD_COUNT: int = 7

# Fraction of the price list trimmed from each end when deriving the sweet
# spot. 0.2 trims the cheapest 20 % and the most expensive 20 %, leaving the
# middle cluster that reflects where most successful sellers actually price.
_SWEET_SPOT_TRIM_RATIO: float = 0.2


class CompetitorAgent(BaseAgent):
    """Analyses the live competitor landscape on the target marketplace.

    Runs two Gemini calls per pipeline run:
    1. Extracts listings, keywords, market leader, and estimated total listing
       count from site-restricted search results.
    2. Synthesises trend direction, product type, peak months, and current
       demand signal from a separate set of trend-focused search results.

    Performance metrics (avg rating, avg review count) and the sweet-spot price
    range are computed from the extracted listings in pure Python — no third
    Gemini call is made.

    Returns a ``competitor_result`` dict consumed by MarginAgent (for
    avg_market_price), RiskAgent (for market_saturation), and ReportAgent.
    """

    def __init__(self) -> None:
        """Initialises the CompetitorAgent with its fixed agent name."""
        super().__init__("competitor_agent")

    # ------------------------------------------------------------------
    # Public pipeline interface
    # ------------------------------------------------------------------

    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """Searches for competitor listings and returns full market intelligence.

        Executes two search phases (listing queries + trend queries) and two
        Gemini calls. Pure-Python helpers derive performance metrics and the
        sweet-spot price range from the extracted listings so no third Gemini
        call is required.

        Args:
            context: Cumulative session context from the Orchestrator. Must
                contain ``product_name`` and ``marketplace``.

        Returns:
            A dict with a single key ``"competitor_result"`` containing:

            .. code-block:: python

                {
                    "listings": [
                        {
                            "title": str,
                            "price": float,
                            "currency": str,           # "PKR" or "USD"
                            "rating": float,
                            "review_count": int,
                            "seller_name": str,
                            "source_url": str,
                            "data_retrieved_date": str # ISO 8601
                        },
                        ...  # up to 5
                    ],
                    "total_active_listings": int,
                    "price_range": {"min": float, "max": float},
                    "sweet_spot_price_range": {"min": float, "max": float},
                    "market_leader": str,
                    "performance_metrics": {
                        "estimated_monthly_sales_range": str,
                        "avg_rating_to_rank": float,
                        "avg_review_count_top_sellers": int,
                        "market_leader_tenure_signal": str
                    },
                    "trends_and_seasonality": {
                        "trend_direction": str,        # "growing"/"stable"/"declining"/"unknown"
                        "product_type": str,           # "evergreen"/"seasonal"/"fad"/"unknown"
                        "peak_season_months": list[str],
                        "current_month_demand_signal": str,
                        "source_urls": list[str]
                    },
                    "top_keywords": [
                        {"keyword": str, "volume_signal": str}  # "high"/"medium"/"low"
                    ],
                    "market_saturation": str,          # "low"/"medium"/"high"
                    "avg_market_price": float,
                    "currency": str,
                    "search_queries_used": list[str]
                }

            If all searches fail, every numeric field is 0.0, lists are empty,
            and string fields default to "unknown" so downstream agents run.
        """
        product: str = context.get("product_name", "").strip()
        marketplace: str = context.get("marketplace", "").strip()

        # Currency is determined purely by marketplace — never ask Gemini for it
        currency: str = "PKR" if marketplace == "daraz_pk" else "USD"

        self._log_start("competitor analysis")

        # Lazy import avoids the MCP server's module-level SERPER_API_KEY check
        # executing before load_dotenv() has been called by the Orchestrator.
        from mcp_server.search_mcp import web_search

        all_queries_ran: list[str] = []

        # ----------------------------------------------------------------
        # Phase 1 — Listing search (Gemini call 1)
        # ----------------------------------------------------------------
        listing_queries: list[str] = self._get_search_queries(context)
        listing_results: list[dict[str, Any]] = []

        for query in listing_queries:
            self._logger.info("Listing search query: %s", query)
            try:
                results = web_search(query=query, num_results=8)
                listing_results.extend(results)
                all_queries_ran.append(query)
            except Exception as exc:
                # Skip and continue — partial results beat a full abort
                self._logger.warning("Listing query failed (skipping): %r — %s", query, exc)

        # Attempt listing extraction only when we have search results
        gemini_listing: dict[str, Any] = {}
        if listing_results:
            search_text: str = self._format_results_for_prompt(listing_results)
            listing_prompt: str = self._build_competitor_prompt(context, search_text)
            raw_listing: str = self._safe_generate(listing_prompt)
            gemini_listing = self._parse_gemini_response(raw_listing)

        if not gemini_listing:
            # Neither search results nor Gemini produced usable data — bail early
            # rather than running the trend phase with nothing to compare against.
            self._logger.warning(
                "No usable listing data for %r on %r", product, marketplace
            )
            self._log_end("competitor analysis", success=False)
            return {"competitor_result": self._empty_result(all_queries_ran, currency)}

        # ----------------------------------------------------------------
        # Post-process listing extraction results (pure Python)
        # ----------------------------------------------------------------
        today_str: str = datetime.date.today().isoformat()
        listings: list[dict[str, Any]] = gemini_listing.get("listings", [])[:_MAX_LISTINGS]

        # Enforce data_retrieved_date on every listing — Gemini may omit it
        for listing in listings:
            if not listing.get("data_retrieved_date"):
                listing["data_retrieved_date"] = today_str

        avg_price, price_range = self._compute_price_stats(listings)
        sweet_spot: dict[str, float] = self._derive_sweet_spot(listings)

        total_active_listings: int = int(gemini_listing.get("total_active_listings", 0))
        market_leader: str = gemini_listing.get("market_leader", "unknown")
        tenure_signal: str = gemini_listing.get("market_leader_tenure_signal", "unknown")

        # Saturation now considers both total_active_listings AND review counts
        saturation: str = self._classify_saturation(listings, total_active_listings)

        # Performance metrics are derived from the listing data in pure Python —
        # no extra Gemini call is needed because the signal is already extracted.
        performance_metrics: dict[str, Any] = self._build_performance_metrics(
            listings, tenure_signal
        )

        # Keywords come back as [{keyword, volume_signal}] dicts from the prompt
        top_keywords: list[dict[str, str]] = gemini_listing.get("top_keywords", [])[
            :_KEYWORD_COUNT
        ]

        # ----------------------------------------------------------------
        # Phase 2 — Trend search (Gemini call 2)
        # ----------------------------------------------------------------
        trend_queries: list[str] = self._get_trend_search_queries(context)
        trend_results: list[dict[str, Any]] = []

        for query in trend_queries:
            self._logger.info("Trend search query: %s", query)
            try:
                results = web_search(query=query, num_results=5)
                trend_results.extend(results)
                all_queries_ran.append(query)
            except Exception as exc:
                self._logger.warning("Trend query failed (skipping): %r — %s", query, exc)

        # Fall back to safe defaults when trend search yields nothing
        trends: dict[str, Any] = self._empty_trends()
        if trend_results:
            trend_text: str = self._format_results_for_prompt(trend_results)
            trend_prompt: str = self._build_trends_prompt(context, trend_text)
            raw_trend: str = self._safe_generate(trend_prompt)
            parsed_trends: dict[str, Any] = self._parse_gemini_response(raw_trend)
            if parsed_trends:
                trends = parsed_trends

        self._log_end("competitor analysis", success=True)

        return {
            "competitor_result": {
                "listings": listings,
                "total_active_listings": total_active_listings,
                "price_range": price_range,
                "sweet_spot_price_range": sweet_spot,
                "market_leader": market_leader,
                "performance_metrics": performance_metrics,
                "trends_and_seasonality": trends,
                "top_keywords": top_keywords,
                "market_saturation": saturation,
                "avg_market_price": avg_price,
                "currency": currency,
                "search_queries_used": all_queries_ran,
            }
        }

    # ------------------------------------------------------------------
    # Query builders
    # ------------------------------------------------------------------

    def _get_search_queries(self, context: dict[str, Any]) -> list[str]:
        """Builds 2–3 site-restricted queries targeting competitor listings.

        The ``site:`` operator confines results to the target marketplace domain
        so Gemini receives actual listing pages rather than review blogs or
        price comparison sites.

        Args:
            context: Session context containing ``product_name`` and
                ``marketplace``.

        Returns:
            List of 2–3 query strings ordered from most specific to broadest.
        """
        product: str = context.get("product_name", "product").strip()
        marketplace: str = context.get("marketplace", "").strip()

        domain: str = _MARKETPLACE_DOMAINS.get(marketplace, "")
        label: str = _MARKETPLACE_LABELS.get(marketplace, marketplace)

        queries: list[str] = []

        if domain:
            # Highest-signal query: site-restricted listing search with price signal
            queries.append(f"site:{domain} {product} price reviews")
            # Adds "buy" to bias toward transactional listing pages within the domain
            queries.append(f"site:{domain} buy {product} best seller")

        # Broadest fallback: catches any indexed subdomains or microsites the
        # marketplace uses (e.g. Amazon's TLD variants)
        queries.append(f"{label} {product} price reviews top seller listings")

        return queries

    def _get_trend_search_queries(self, context: dict[str, Any]) -> list[str]:
        """Builds 1–2 queries specifically for trend and seasonality research.

        These queries are intentionally separate from the listing queries so the
        trend Gemini call receives trend-focused snippets (market reports, Google
        Trends summaries, news) rather than listing pages.

        Args:
            context: Session context containing ``product_name`` and
                ``marketplace``.

        Returns:
            List of 1–2 trend-focused query strings.
        """
        product: str = context.get("product_name", "product").strip()
        marketplace: str = context.get("marketplace", "").strip()
        region: str = _MARKETPLACE_REGIONS.get(marketplace, "")
        year: int = datetime.date.today().year

        queries: list[str] = []

        # Query 1: explicit trend direction signal for the current year
        queries.append(
            f'"{product}" demand trend {region} {year} growing declining'
        )

        # Query 2: seasonality — looks for sales events or seasonal demand patterns
        # associated with this product category in the target region
        queries.append(
            f'"{product}" best selling season peak months {region}'
        )

        return queries

    # ------------------------------------------------------------------
    # Prompt builders
    # ------------------------------------------------------------------

    def _build_competitor_prompt(
        self, context: dict[str, Any], search_results: str
    ) -> str:
        """Constructs the Gemini prompt for listing extraction (Gemini call 1).

        Instructs Gemini to act as a data extractor only — never to invent
        listings or keywords that are not present in the search results.
        Also requests ``total_active_listings``, ``market_leader``, and
        ``market_leader_tenure_signal`` which are new vs. the original prompt.

        Keywords are now returned as ``{keyword, volume_signal}`` dicts instead
        of a flat list — volume_signal is qualitative, based on how many listing
        titles the keyword appears in across the results, not invented.

        Args:
            context: Session context providing ``product_name`` and
                ``marketplace``.
            search_results: Pre-formatted block of all search result titles,
                snippets, and URLs produced by ``_format_results_for_prompt``.

        Returns:
            A complete prompt string ready to pass to ``_safe_generate``.
        """
        product: str = context.get("product_name", "the product")
        marketplace: str = context.get("marketplace", "")
        today: str = datetime.date.today().isoformat()
        currency: str = "PKR" if marketplace == "daraz_pk" else "USD"

        return f"""You are a competitor listing data extractor for an e-commerce research tool.

Your task: extract structured competitor data from the search results below for the product "{product}" on {marketplace}.

CRITICAL RULES:
1. Return ONLY valid JSON. No markdown fences, no code fences, no text before or after.
2. Never invent, assume, or fabricate any data not present in the search results.
3. Extract keywords ONLY from the actual listing titles found — not from general product knowledge.
4. Use "{currency}" as the currency for all listings.
5. price must be a float; use 0.0 if not found.
6. rating must be a float 0.0–5.0; use 0.0 if not found.
7. review_count must be an integer; use 0 if not found.
8. seller_name: use the domain or store name visible in the URL or snippet; use "Unknown" if absent.
9. source_url must come directly from the search result URLs — never construct a URL.
10. data_retrieved_date for every listing must be exactly: {today}
11. Include at most {_MAX_LISTINGS} listings ranked by review_count descending.
12. top_keywords: extract {_KEYWORD_COUNT} keywords or phrases from the actual listing titles.
    For each keyword, estimate volume_signal as "high" (appears in 3+ titles), "medium" (2 titles),
    or "low" (1 title). Do NOT invent keywords.
13. total_active_listings: if any search snippet mentions a total result count or
    number of listings (e.g. "1,234 results"), extract that integer. Otherwise use 0.
14. market_leader: identify the seller or listing that appears most prominent
    (highest reviews or most repeat appearances). Note if it appears to be a
    marketplace-owned brand (e.g. "Daraz Mall", "Amazon's Choice"). Use "unknown" if unclear.
15. market_leader_tenure_signal: any snippet text suggesting how long the market leader
    has been selling (e.g. "established seller", "since 2018", "5,000+ sales"). Use "unknown" if absent.

SEARCH RESULTS:
{search_results}

Return this JSON structure exactly:
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
  "top_keywords": [
    {{"keyword": "keyword phrase", "volume_signal": "high"}}
  ],
  "total_active_listings": 0,
  "market_leader": "Seller name or unknown",
  "market_leader_tenure_signal": "Signal text or unknown"
}}"""

    def _build_trends_prompt(
        self, context: dict[str, Any], search_results: str
    ) -> str:
        """Constructs the Gemini prompt for trend synthesis (Gemini call 2).

        Instructs Gemini to synthesise only what is explicitly stated in the
        trend search results. It must return "unknown" rather than guess when
        the search results do not clearly indicate a value. It must never invent
        a percentage or a number — only qualitative direction labels are allowed.

        Args:
            context: Session context providing ``product_name``, ``marketplace``,
                and ``region`` for framing the extraction correctly.
            search_results: Pre-formatted block of trend-focused search result
                titles, snippets, and URLs.

        Returns:
            A complete prompt string ready to pass to ``_safe_generate``.
        """
        product: str = context.get("product_name", "the product")
        marketplace: str = context.get("marketplace", "")
        region: str = _MARKETPLACE_REGIONS.get(marketplace, "the target region")
        current_month: str = datetime.date.today().strftime("%B")  # e.g. "June"

        return f"""You are a market trend analyst for an e-commerce research tool.

Your task: synthesise trend and seasonality signals for the product "{product}" in {region} using ONLY the search results below.

CRITICAL RULES:
1. Return ONLY valid JSON. No markdown, no code fences, no explanation text.
2. If the search results do not clearly support a value, use "unknown" — do NOT guess.
3. Never invent a percentage or sales figure. Only qualitative direction labels are allowed.
4. source_urls must come directly from the search result URLs provided — never construct a URL.

FIELDS TO EXTRACT:
- trend_direction: "growing", "stable", "declining", or "unknown"
  (use "unknown" unless the search results explicitly mention a trend direction)
- product_type: "evergreen", "seasonal", "fad", or "unknown"
  (evergreen = sells year-round steadily; seasonal = clear peak period; fad = sharp spike then drop)
- peak_season_months: list of month name strings (e.g. ["November", "December"])
  where demand is highest — derive only from snippets that mention sales events
  or seasonal demand for this product. Use [] if nothing found.
- current_month_demand_signal: "high", "moderate", "low", or "unknown"
  (estimate based on whether {current_month} falls within any peak period found,
  or any explicit mention of current demand — use "unknown" if no signal)
- source_urls: list of URLs from the search results that contained trend information.
  Use [] if none of the results were useful.

SEARCH RESULTS:
{search_results}

Return this JSON structure exactly:
{{
  "trend_direction": "unknown",
  "product_type": "unknown",
  "peak_season_months": [],
  "current_month_demand_signal": "unknown",
  "source_urls": []
}}"""

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_gemini_response(self, raw_response: str) -> dict[str, Any]:
        """Parses the JSON string returned by Gemini into a data dict.

        Defensively strips markdown code fences that Gemini sometimes adds
        despite explicit instructions not to. Returns ``{}`` on any parse
        failure so callers can detect the failure and use safe defaults.

        Args:
            raw_response: The raw string returned by ``_safe_generate``.

        Returns:
            Parsed dict, or ``{}`` if the response is not valid JSON.
        """
        cleaned: str = raw_response.strip()

        # Strip the opening fence line (```json\n or ```\n)
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1]
        # Strip the closing fence
        if cleaned.endswith("```"):
            cleaned = cleaned.rsplit("```", 1)[0]

        cleaned = cleaned.strip()

        try:
            parsed: dict[str, Any] = json.loads(cleaned)
            return parsed
        except json.JSONDecodeError as exc:
            self._logger.error(
                "Failed to parse Gemini response as JSON: %s\n"
                "Raw response (first 300 chars): %.300s",
                exc,
                raw_response,
            )
            return {}

    # ------------------------------------------------------------------
    # Pure-Python aggregate helpers
    # ------------------------------------------------------------------

    def _compute_price_stats(
        self, listings: list[dict[str, Any]]
    ) -> tuple[float, dict[str, float]]:
        """Computes average price and full price range from extracted listings.

        Excludes 0.0 prices — those indicate "not found" rather than actual
        zero-cost products, so including them would distort the mean and range.

        Args:
            listings: List of listing dicts each expected to have a ``price``
                float key.

        Returns:
            Tuple of (avg_price, price_range_dict). Both are 0.0 / {0.0, 0.0}
            when no valid prices exist.
        """
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

    def _classify_saturation(
        self,
        listings: list[dict[str, Any]],
        total_active_listings: int = 0,
    ) -> str:
        """Classifies market saturation using review counts AND listing count.

        Either condition is sufficient to trigger a higher saturation level —
        a small number of very heavily-reviewed listings indicates a saturated
        market just as clearly as a large raw listing count.

        Levels:
        - ``"high"``: total listings > 500 OR any listing has > 1 000 reviews.
        - ``"medium"``: total listings > 100 OR any listing has > 100 reviews.
        - ``"low"``: neither condition met, or no data available.

        Args:
            listings: Extracted listing dicts, each with a ``review_count`` key.
            total_active_listings: Estimated total number of sellers/listings on
                the marketplace for this product, as extracted by Gemini.

        Returns:
            One of ``"low"``, ``"medium"``, or ``"high"``.
        """
        if not listings and total_active_listings == 0:
            # No data — conservative default avoids falsely blocking the run
            return "low"

        max_reviews: int = max(
            (int(listing.get("review_count", 0)) for listing in listings),
            default=0,
        )

        if (total_active_listings > _SATURATION_HIGH_LISTING_THRESHOLD
                or max_reviews > _SATURATION_HIGH_REVIEW_THRESHOLD):
            return "high"

        if (total_active_listings > _SATURATION_MEDIUM_LISTING_THRESHOLD
                or max_reviews > _SATURATION_MEDIUM_REVIEW_THRESHOLD):
            return "medium"

        return "low"

    def _derive_sweet_spot(
        self, listings: list[dict[str, Any]]
    ) -> dict[str, float]:
        """Derives the sweet-spot price range from extracted listing prices.

        Trims ``_SWEET_SPOT_TRIM_RATIO`` (20 %) of prices from each end of the
        sorted price list to remove outliers, then returns the min and max of
        the remaining middle cluster. This reflects where successful sellers
        actually price rather than capturing fringe extremes.

        Requires at least 3 valid prices to trim meaningfully. If fewer are
        available, the full price range doubles as the sweet spot.

        Args:
            listings: Extracted listing dicts each expected to have a ``price``
                float key.

        Returns:
            Dict with ``"min"`` and ``"max"`` float keys. Both are 0.0 when
            no valid prices are found.
        """
        valid_prices: list[float] = sorted(
            float(l.get("price", 0.0))
            for l in listings
            if float(l.get("price", 0.0)) > 0.0
        )

        if not valid_prices:
            return {"min": 0.0, "max": 0.0}

        if len(valid_prices) < 3:
            # Too few points to trim — the full range is the best estimate
            return {
                "min": round(valid_prices[0], 2),
                "max": round(valid_prices[-1], 2),
            }

        # Trim at least 1 price from each end; scale up for larger lists
        trim_count: int = max(1, round(len(valid_prices) * _SWEET_SPOT_TRIM_RATIO))
        middle: list[float] = valid_prices[trim_count : len(valid_prices) - trim_count]

        # Guard against the (unlikely) case where trimming empties the list
        if not middle:
            middle = valid_prices

        return {
            "min": round(middle[0], 2),
            "max": round(middle[-1], 2),
        }

    def _build_performance_metrics(
        self,
        listings: list[dict[str, Any]],
        tenure_signal: str = "unknown",
    ) -> dict[str, Any]:
        """Derives performance metrics from extracted listing data in pure Python.

        All values are computed from the listings list already in memory — no
        extra Gemini call is needed.

        ``estimated_monthly_sales_range`` is always ``"insufficient data"``
        because the pipeline does not collect historical review-velocity data
        (which would require comparing review counts across two time points).
        The field is preserved in the schema to signal the intent clearly rather
        than omitting it.

        Args:
            listings: Extracted listing dicts, each expected to have ``rating``
                (float) and ``review_count`` (int) keys.
            tenure_signal: The ``market_leader_tenure_signal`` extracted by
                Gemini from listing snippets. Passed through unchanged.

        Returns:
            Dict with the four performance metric keys defined in the spec.
        """
        if not listings:
            return {
                "estimated_monthly_sales_range": "insufficient data",
                "avg_rating_to_rank": 0.0,
                "avg_review_count_top_sellers": 0,
                "market_leader_tenure_signal": tenure_signal,
            }

        # Average only ratings that were actually found (non-zero)
        valid_ratings: list[float] = [
            float(l.get("rating", 0.0))
            for l in listings
            if float(l.get("rating", 0.0)) > 0.0
        ]
        avg_rating: float = (
            round(sum(valid_ratings) / len(valid_ratings), 2)
            if valid_ratings
            else 0.0
        )

        # Average review count across all top listings (0-count listings are valid data)
        review_counts: list[int] = [int(l.get("review_count", 0)) for l in listings]
        avg_reviews: int = (
            round(sum(review_counts) / len(review_counts))
            if review_counts
            else 0
        )

        return {
            # Monthly sales cannot be derived without historical review velocity data
            "estimated_monthly_sales_range": "insufficient data",
            "avg_rating_to_rank": avg_rating,
            "avg_review_count_top_sellers": avg_reviews,
            # Passed through from Gemini's listing extraction — no recalculation needed
            "market_leader_tenure_signal": tenure_signal,
        }

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------

    def _format_results_for_prompt(
        self, results: list[dict[str, Any]]
    ) -> str:
        """Formats raw search result dicts into a readable block for any prompt.

        Presenting results as labelled text blocks (rather than raw JSON)
        reduces the chance that Gemini confuses the input data structure with
        the JSON output format it is asked to produce.

        Args:
            results: List of result dicts from ``web_search``, each containing
                ``title``, ``snippet``, ``url``, and ``date`` keys.

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

    # ------------------------------------------------------------------
    # Scaffold helpers
    # ------------------------------------------------------------------

    def _empty_trends(self) -> dict[str, Any]:
        """Returns a safe empty trends dict when trend search or parsing fails.

        All fields default to "unknown" / empty list rather than None so
        downstream agents and the report generator can safely read every key
        without defensive checks on their side.

        Returns:
            A ``trends_and_seasonality`` dict with all keys set to safe defaults.
        """
        return {
            "trend_direction": "unknown",
            "product_type": "unknown",
            "peak_season_months": [],
            "current_month_demand_signal": "unknown",
            "source_urls": [],
        }

    def _empty_result(
        self, queries_that_ran: list[str], currency: str = "USD"
    ) -> dict[str, Any]:
        """Returns a safe empty competitor_result when no data was retrieved.

        All numeric fields are 0.0, all list fields are empty, and string
        fields default to "unknown" or "low" so downstream agents (Margin,
        Risk, Report) can still run without key-not-found errors.

        Args:
            queries_that_ran: Queries attempted before the failure — preserved
                for audit at the HITL checkpoint.
            currency: Currency code derived from marketplace; included so
                the empty result has the same schema as a populated result.

        Returns:
            A full ``competitor_result`` dict with every key present.
        """
        return {
            "listings": [],
            "total_active_listings": 0,
            "price_range": {"min": 0.0, "max": 0.0},
            "sweet_spot_price_range": {"min": 0.0, "max": 0.0},
            "market_leader": "unknown",
            "performance_metrics": {
                "estimated_monthly_sales_range": "insufficient data",
                "avg_rating_to_rank": 0.0,
                "avg_review_count_top_sellers": 0,
                "market_leader_tenure_signal": "unknown",
            },
            "trends_and_seasonality": self._empty_trends(),
            "top_keywords": [],
            "market_saturation": "low",
            "avg_market_price": 0.0,
            "currency": currency,
            "search_queries_used": queries_that_ran,
        }
