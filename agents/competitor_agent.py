"""Competitor Analysis Agent for DropSmart.

Searches for live competitor listings on the target marketplace and synthesises
trend/seasonality data. Makes exactly one Gemini call per pipeline run.

Listings come from the Shopping API via ``search_competitor_listings_live``,
which returns pre-structured fields (title, price, rating, etc.). Because the
data is already structured, Gemini is not needed for listing extraction —
listings are built directly in Python from the API response.

The single Gemini call (trend synthesis) receives web_search results focused
on demand direction and seasonality, and returns trend_direction, product_type,
peak_season_months, and current_month_demand_signal.

Keywords and market_leader are derived from the structured listing data in
pure Python using word-frequency analysis — no Gemini call required.
"""

# --- Standard library ---
import datetime
import json
import re
from collections import Counter
from typing import Any

# --- Local ---
from agents.base_agent import BaseAgent


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Human-readable marketplace names used inside trend search query strings.
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

# ISO region codes passed to the Shopping API tool.
_MARKETPLACE_ISO_REGIONS: dict[str, str] = {
    "daraz_pk": "PK",
    "walmart_us": "US",
    "amazon_us": "US",
    "etsy_us": "US",
}

# Review-count thresholds for saturation classification.
# A single heavily-reviewed listing signals saturation as reliably as many.
_SATURATION_HIGH_REVIEW_THRESHOLD: int = 1000
_SATURATION_MEDIUM_REVIEW_THRESHOLD: int = 100

# Active listing count thresholds that ALSO feed the saturation signal.
# These are scaled to the Shopping API's fixed pool size of ~30 results —
# NOT a platform-wide listing count (the Shopping API does not expose that).
# 20+ results returned signals strong product availability across many sellers.
# Under 8 results signals a niche or low-competition product at this query.
_SATURATION_HIGH_LISTING_THRESHOLD: int = 20
_SATURATION_MEDIUM_LISTING_THRESHOLD: int = 8

# Cap on listings kept in the output — keeps the HITL summary scannable.
_MAX_LISTINGS: int = 5

# Number of keywords to extract from listing titles.
_KEYWORD_COUNT: int = 7

# Fraction of the price list trimmed from each end when deriving the sweet
# spot. 0.2 trims the cheapest 20% and the most expensive 20%, leaving the
# middle cluster that reflects where most successful sellers actually price.
_SWEET_SPOT_TRIM_RATIO: float = 0.2

# Words excluded from keyword frequency analysis — too common to be signal.
_STOP_WORDS: frozenset[str] = frozenset({
    "a", "an", "and", "as", "at", "be", "best", "buy", "by",
    "for", "from", "in", "is", "it", "its", "new", "of",
    "on", "or", "set", "the", "to", "top", "with",
})

# Regional peak-sales events injected into the seasonality trend query.
# Generic "best selling season" queries often return listicles rather than
# product-specific seasonality data; naming known events forces search engines
# to return results that explicitly discuss seasonal demand for this product.
_REGIONAL_SALES_EVENTS: dict[str, str] = {
    "daraz_pk": "11.11 12.12 Ramadan Eid sale Pakistan",
    "walmart_us": "Black Friday Cyber Monday Christmas back to school",
    "amazon_us": "Black Friday Cyber Monday Christmas Prime Day",
    "etsy_us": "Christmas Valentine's Mother's Day holiday gift season",
}


class CompetitorAgent(BaseAgent):
    """Analyses the live competitor landscape on the target marketplace.

    Phase 1 — Structured listing fetch:
        Calls ``search_competitor_listings_live`` (Shopping API) to get
        pre-structured listing data. Builds the listings list directly in
        Python — no Gemini call. Derives keywords, market_leader, price stats,
        and saturation from the listings in pure Python.

    Phase 2 — Trend synthesis (one Gemini call):
        Runs 2 web_search queries focused on demand direction and seasonality.
        Passes the results to Gemini for synthesis. Gemini returns trend_direction,
        product_type, peak_season_months, and current_month_demand_signal.

    Returns a ``competitor_result`` dict consumed by MarginAgent (avg_market_price),
    RiskAgent (market_saturation), and ReportAgent.
    """

    def __init__(self) -> None:
        """Initialises the CompetitorAgent with its fixed agent name."""
        super().__init__("competitor_agent")

    # ------------------------------------------------------------------
    # Public pipeline interface
    # ------------------------------------------------------------------

    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """Fetches competitor listings and returns full market intelligence.

        Phase 1 uses the Shopping API for structured listing data (no Gemini).
        Phase 2 uses web_search + one Gemini call for trend synthesis.

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
                            "currency": str,             # "PKR" or "USD"
                            "rating": float,
                            "review_count": int,
                            "seller_name": str,
                            "source_url": str,
                            "data_retrieved_date": str,  # ISO 8601
                            "off_marketplace": bool
                        },
                        ...  # up to _MAX_LISTINGS entries
                    ],
                    "total_active_listings": int,    # count of Shopping API results
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
                        "trend_direction": str,
                        "product_type": str,
                        "peak_season_months": list[str],
                        "current_month_demand_signal": str,
                        "source_urls": list[str]
                    },
                    "top_keywords": [
                        {"keyword": str, "volume_signal": str}
                    ],
                    "market_saturation": str,        # "low"/"medium"/"high"
                    "avg_market_price": float,
                    "currency": str,
                    "search_queries_used": list[str]
                }

            If both phases return no data, all numeric fields are 0.0, lists
            are empty, and strings default to "unknown".
        """
        product: str = context.get("product_name", "").strip()
        marketplace: str = context.get("marketplace", "").strip()

        # Currency is set from marketplace, not from any API response —
        # avoids the possibility of an API returning an unexpected currency code.
        currency: str = "PKR" if marketplace == "daraz_pk" else "USD"
        region: str = _MARKETPLACE_ISO_REGIONS.get(marketplace, "US")

        self._log_start("competitor analysis")

        # Lazy import pattern: avoids running the MCP module's top-level
        # SERPER_API_KEY check before the Orchestrator has called load_dotenv().
        from mcp_server.search_mcp import search_competitor_listings_live, web_search

        all_queries_ran: list[str] = []

        # ----------------------------------------------------------------
        # Phase 1 — Structured listing fetch via Shopping API (no Gemini)
        # ----------------------------------------------------------------

        raw_listings: list[dict[str, Any]] = []
        try:
            raw_listings = search_competitor_listings_live(
                product=context["product_name"],
                marketplace=context["marketplace"],
                region=region,
            )
            self._logger.info(
                "Shopping API returned %d items for %r on %r",
                len(raw_listings),
                product,
                marketplace,
            )
        except Exception as exc:
            self._logger.warning(
                "search_competitor_listings_live failed: %s — proceeding with empty listings.",
                exc,
            )

        # total_active_listings: the number of shopping results the API returned
        # for this product and marketplace. This is NOT the platform's true total
        # listing count (the Shopping API does not expose that figure), but it
        # is a useful relative saturation signal across products.
        total_active_listings: int = len(raw_listings)
        all_queries_ran.append(
            f"search_competitor_listings_live({product!r}, {marketplace!r})"
        )

        # Build structured listing dicts directly from the API response.
        # The Shopping API returns pre-structured fields so no Gemini extraction
        # call is needed — we just normalise the types and field names.
        today_str: str = datetime.date.today().isoformat()
        listings: list[dict[str, Any]] = []

        for item in raw_listings[:_MAX_LISTINGS]:
            price_float: float = self._parse_price_string(
                str(item.get("price", "") or "")
            )
            listings.append(
                {
                    "title": str(item.get("title", "")),
                    "price": price_float,
                    "currency": currency,
                    # Shopping API may return None — coerce to 0.0 explicitly
                    "rating": float(item.get("rating") or 0.0),
                    "review_count": int(item.get("rating_count") or 0),
                    "seller_name": str(item.get("source") or "Unknown"),
                    "source_url": str(item.get("link", "")),
                    "data_retrieved_date": today_str,
                    # Preserve off_marketplace flag so the report can annotate
                    # listings that came from outside the target marketplace.
                    "off_marketplace": bool(item.get("off_marketplace", False)),
                }
            )

        if not listings:
            self._logger.warning(
                "No usable listing data for %r on %r", product, marketplace
            )
            self._log_end("competitor analysis", success=False)
            return {"competitor_result": self._empty_result(all_queries_ran, currency)}

        # Derive all listing-based signals in pure Python — no Gemini needed
        # because the Shopping API already gave us structured numeric fields.
        avg_price, price_range = self._compute_price_stats(listings)
        sweet_spot: dict[str, float] = self._derive_sweet_spot(listings)
        saturation: str = self._classify_saturation(listings, total_active_listings)
        performance_metrics: dict[str, Any] = self._build_performance_metrics(
            listings, tenure_signal="unknown"
        )

        # Market leader: most frequently appearing seller across the top listings
        market_leader: str = self._pick_market_leader(listings)

        # Keywords: word-frequency analysis across listing titles — "high" when
        # a term appears in 3+ titles, "medium" for 2, "low" for 1.
        top_keywords: list[dict[str, str]] = self._extract_keywords_from_titles(
            listings
        )

        # ----------------------------------------------------------------
        # Phase 2 — Trend synthesis (one Gemini call)
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
                self._logger.warning(
                    "Trend query failed (skipping): %r — %s", query, exc
                )

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
    # Query builders (trend only — listing queries replaced by Shopping API)
    # ------------------------------------------------------------------

    def _get_trend_search_queries(self, context: dict[str, Any]) -> list[str]:
        """Builds 2 queries specifically for trend and seasonality research.

        These queries are intentionally separate from the listing fetch so the
        trend Gemini call receives trend-focused snippets (market reports, Google
        Trends summaries, news) rather than listing pages.

        The seasonality query includes known regional sales events from
        ``_REGIONAL_SALES_EVENTS`` so search engines surface results that
        explicitly discuss this product's seasonal demand rather than generic
        "best products to sell" listicles.

        Args:
            context: Session context containing ``product_name`` and
                ``marketplace``.

        Returns:
            List of 2 trend-focused query strings.
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

        # Query 2: seasonality — includes known regional sales events so search
        # results explicitly mention seasonal demand for this product rather than
        # returning generic "best dropshipping products" listicle pages.
        queries.append(
            f'"{product}" best selling season peak months {region} '
            f'{_REGIONAL_SALES_EVENTS.get(marketplace, "holiday season")}'
        )

        return queries

    # ------------------------------------------------------------------
    # Prompt builder (trends only — listing prompt removed)
    # ------------------------------------------------------------------

    def _build_trends_prompt(
        self, context: dict[str, Any], search_results: str
    ) -> str:
        """Constructs the Gemini prompt for trend synthesis (the one Gemini call).

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
    # Price string parser
    # ------------------------------------------------------------------

    def _parse_price_string(self, price_str: str) -> float:
        """Parses a price string containing currency symbols into a float.

        Handles formats returned by the Shopping API such as ``"$19.99"``,
        ``"PKR 1,799"``, ``"Rs. 233"``, and bare ``"19.99"``. Strips all
        non-numeric characters except the decimal point before parsing.

        Args:
            price_str: Raw price string from a Shopping API result item.

        Returns:
            The numeric price value as a float, or ``0.0`` if the string
            cannot be parsed. Logs a warning on failure rather than raising.
        """
        if not price_str or not price_str.strip():
            return 0.0

        # Remove commas used as thousands separators first, then strip every
        # character that is not a digit or a decimal point. This handles all
        # known currency prefix/suffix formats in one regex pass.
        numeric_only: str = re.sub(r"[^\d.]", "", price_str.replace(",", ""))

        if not numeric_only:
            self._logger.warning(
                "Could not extract numeric value from price string %r — using 0.0",
                price_str,
            )
            return 0.0

        try:
            return float(numeric_only)
        except ValueError:
            self._logger.warning(
                "float() conversion failed on %r (cleaned from %r) — using 0.0",
                numeric_only,
                price_str,
            )
            return 0.0

    # ------------------------------------------------------------------
    # Pure-Python keyword extractor
    # ------------------------------------------------------------------

    def _extract_keywords_from_titles(
        self, listings: list[dict[str, Any]]
    ) -> list[dict[str, str]]:
        """Extracts high-frequency keywords from listing titles using word frequency.

        Tokenises all listing titles, removes stop words and short tokens, counts
        how many distinct titles each token appears in, then returns the top
        ``_KEYWORD_COUNT`` tokens ordered by frequency.

        Volume signal thresholds:
        - ``"high"``: token appears in 3 or more titles.
        - ``"medium"``: token appears in exactly 2 titles.
        - ``"low"``: token appears in exactly 1 title.

        Args:
            listings: Structured listing dicts, each expected to have a
                ``"title"`` key. Missing titles are silently skipped.

        Returns:
            A list of up to ``_KEYWORD_COUNT`` dicts, each with ``"keyword"``
            and ``"volume_signal"`` keys, ordered by frequency descending.
        """
        # Count how many titles each token appears in — not total occurrences,
        # because a keyword repeated 5× in one title is less valuable than one
        # that appears across 5 separate sellers' listings.
        token_title_counts: Counter[str] = Counter()

        for listing in listings:
            title: str = listing.get("title", "").lower()
            # Unique tokens per title so a repeated word in one title only counts once
            unique_tokens: set[str] = {
                tok
                for tok in re.findall(r"[a-z0-9]+", title)
                if len(tok) >= 3 and tok not in _STOP_WORDS
            }
            token_title_counts.update(unique_tokens)

        results: list[dict[str, str]] = []
        for keyword, count in token_title_counts.most_common(_KEYWORD_COUNT):
            if count >= 3:
                volume_signal = "high"
            elif count >= 2:
                volume_signal = "medium"
            else:
                volume_signal = "low"
            results.append({"keyword": keyword, "volume_signal": volume_signal})

        return results

    # ------------------------------------------------------------------
    # Pure-Python market leader detector
    # ------------------------------------------------------------------

    def _pick_market_leader(self, listings: list[dict[str, Any]]) -> str:
        """Identifies the dominant seller from structured listing data.

        Counts how often each ``seller_name`` appears across the top listings.
        The most frequent seller is considered the market leader. Ties are broken
        by insertion order (the listing that appears first wins).

        Args:
            listings: Structured listing dicts, each with a ``"seller_name"``
                key. Entries with empty or ``"Unknown"`` seller names are
                excluded from the count.

        Returns:
            The most frequent seller name as a string, or ``"unknown"`` if
            the listings list is empty or no named sellers are present.
        """
        if not listings:
            return "unknown"

        # Exclude placeholder values — they would win the count trivially
        named_sellers: list[str] = [
            listing.get("seller_name", "")
            for listing in listings
            if listing.get("seller_name", "") not in ("", "Unknown")
        ]

        if not named_sellers:
            return "unknown"

        seller_counts: Counter[str] = Counter(named_sellers)
        # most_common(1)[0][0] returns the single most frequent seller name
        return seller_counts.most_common(1)[0][0]

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
            total_active_listings: Count of Shopping API results before filtering.

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

        Trims ``_SWEET_SPOT_TRIM_RATIO`` (20%) of prices from each end of the
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
        middle: list[float] = valid_prices[trim_count: len(valid_prices) - trim_count]

        # Guard against the unlikely case where trimming empties the list
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
            tenure_signal: A qualitative signal about the market leader's tenure.
                Defaults to ``"unknown"`` since the Shopping API does not expose
                seller tenure and Gemini is no longer called for listings.

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
            "market_leader_tenure_signal": tenure_signal,
        }

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------

    def _format_results_for_prompt(
        self, results: list[dict[str, Any]]
    ) -> str:
        """Formats raw web_search result dicts into a readable block for a prompt.

        Only used for trend search results — listing results now come from the
        Shopping API as structured dicts and are not passed to Gemini.

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
