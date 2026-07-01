"""DropSmart MCP Server — the ONLY file in the project that makes external HTTP calls.

All agents must call these tools through MCP instead of importing requests directly.
Every tool call is logged to security.log for audit purposes.
"""

# --- Standard library ---
import logging
import os
import re
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

# --- Third-party ---
import requests
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Environment & API key setup
# ---------------------------------------------------------------------------

# Load .env file from the project root (two directories up from this file)
load_dotenv()

# Retrieve the Serper API key — fail fast if it is missing so callers get
# a clear error at startup rather than a cryptic 403 during a live run.
_SERPER_API_KEY: str = os.getenv("SERPER_API_KEY", "")
if not _SERPER_API_KEY:
    raise EnvironmentError(
        "SERPER_API_KEY is not set. Add it to your .env file before running."
    )

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Serper.dev search endpoints
_SERPER_URL: str = "https://google.serper.dev/search"
_SERPER_SHOPPING_URL: str = "https://google.serper.dev/shopping"

# Maximum results the API will return per query
_MAX_RESULTS: int = 10

# Valid marketplace identifiers — used for input validation across all tools
_VALID_MARKETPLACES: frozenset[str] = frozenset(
    {"daraz_pk", "walmart_us", "amazon_us", "etsy_us"}
)

# Map each marketplace to its public domain for site-restricted competitor searches
_MARKETPLACE_DOMAINS: dict[str, str] = {
    "daraz_pk": "daraz.pk",
    "walmart_us": "walmart.com",
    "amazon_us": "amazon.com",
    "etsy_us": "etsy.com",
}

# Valid business model identifiers
_VALID_BUSINESS_MODELS: frozenset[str] = frozenset(
    {"dropshipping", "fbs", "fbm"}
)

# Rate limit — maximum Serper API calls per 60-second sliding window
_RATE_LIMIT_CALLS: int = 10
_RATE_LIMIT_WINDOW_SECONDS: int = 60

# ---------------------------------------------------------------------------
# Security / audit logger
# ---------------------------------------------------------------------------

# Log every tool call and response to security.log without ever writing API keys.
_security_logger = logging.getLogger("dropsmart.security")
_security_logger.setLevel(logging.INFO)

# Append to security.log in the project root; create it if it does not exist.
_log_handler = logging.FileHandler("security.log", encoding="utf-8")
_log_handler.setFormatter(
    logging.Formatter("%(message)s")  # raw line — timestamp is embedded in the message
)
_security_logger.addHandler(_log_handler)
_security_logger.propagate = False  # prevent duplicate output to the root logger


def _log_tool_call(tool_name: str, input_params: dict[str, Any]) -> None:
    """Writes a TOOL_CALL audit entry to security.log.

    Args:
        tool_name: Name of the MCP tool being invoked.
        input_params: Dict of sanitized input parameters (must not contain API keys).
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    # Sanitize: never log any value that looks like an API key (>20 uppercase alphanumeric chars)
    safe_params = {
        k: "[REDACTED]" if isinstance(v, str) and re.match(r"^[A-Za-z0-9_\-]{20,}$", v) else v
        for k, v in input_params.items()
    }
    _security_logger.info(
        f"[{timestamp}] TOOL_CALL | tool={tool_name} | params={safe_params}"
    )


def _log_tool_response(
    tool_name: str, status: str, result_count: int
) -> None:
    """Writes a TOOL_RESP audit entry to security.log.

    Args:
        tool_name: Name of the MCP tool that responded.
        status: "success" or "error".
        result_count: Number of results returned; 0 on error.
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    _security_logger.info(
        f"[{timestamp}] TOOL_RESP | tool={tool_name} | status={status} | result_count={result_count}"
    )


# ---------------------------------------------------------------------------
# Rate limiter — sliding window, shared across all tools
# ---------------------------------------------------------------------------

# Stores UTC timestamps of recent Serper API calls to enforce the per-minute cap
_call_timestamps: deque[float] = deque()


def _check_rate_limit() -> None:
    """Enforces a sliding-window rate limit of 10 Serper calls per 60 seconds.

    Also introduces a minimum 1.5-second delay between ALL calls — not only
    when the cap is reached. This prevents burst traffic from hitting the API
    in rapid succession (e.g. when FeeAgent and SupplierAgent run back-to-back)
    which would exhaust the window before downstream agents can execute.

    Raises:
        RuntimeError: If the rate limit has been reached.
    """
    # Minimum inter-call spacing — applied unconditionally before rate-limit
    # check so that bursts from multiple agents are naturally spread out.
    time.sleep(1.5)

    now = datetime.now(timezone.utc).timestamp()
    window_start = now - _RATE_LIMIT_WINDOW_SECONDS

    # Drop timestamps that have fallen outside the current 60-second window
    while _call_timestamps and _call_timestamps[0] < window_start:
        _call_timestamps.popleft()

    # Reject the call if the cap is already reached for this window
    if len(_call_timestamps) >= _RATE_LIMIT_CALLS:
        oldest = _call_timestamps[0]
        retry_in = int(_RATE_LIMIT_WINDOW_SECONDS - (now - oldest)) + 1
        raise RuntimeError(
            f"Rate limit reached: {_RATE_LIMIT_CALLS} calls per {_RATE_LIMIT_WINDOW_SECONDS}s. "
            f"Retry in ~{retry_in}s."
        )

    # Record this call timestamp
    _call_timestamps.append(now)


# ---------------------------------------------------------------------------
# Serper.dev API helper
# ---------------------------------------------------------------------------

def _call_serper(query: str, num_results: int) -> list[dict[str, Any]]:
    """Sends a single search query to the Serper.dev API and returns results.

    Args:
        query: The search query string.
        num_results: How many results to request (capped at _MAX_RESULTS).

    Returns:
        A list of result dicts, each containing at minimum:
        ``title``, ``snippet``, ``url``, and ``date`` (empty string if absent).

    Raises:
        RuntimeError: If the Serper API returns a non-200 status.
    """
    # Enforce the rate limit before every outbound call
    _check_rate_limit()

    # Cap results to the API maximum to avoid unexpected behavior
    capped_results = min(num_results, _MAX_RESULTS)

    # Build the request payload — API key goes in the header, never in the body
    payload = {"q": query, "num": capped_results}
    headers = {
        "X-API-KEY": _SERPER_API_KEY,
        "Content-Type": "application/json",
    }

    response = requests.post(_SERPER_URL, json=payload, headers=headers, timeout=15)

    # Surface HTTP errors with context instead of letting them bubble as raw exceptions
    if response.status_code != 200:
        raise RuntimeError(
            f"Serper API returned HTTP {response.status_code}: {response.text[:200]}"
        )

    data = response.json()

    # Extract the organic results list; fall back to empty list if the key is absent
    raw_results: list[dict] = data.get("organic", [])

    # Normalise each result into the project's standard shape
    normalised: list[dict[str, Any]] = []
    for item in raw_results:
        normalised.append(
            {
                "title": item.get("title", ""),
                "snippet": item.get("snippet", ""),
                "url": item.get("link", ""),
                "date": item.get("date", ""),  # Serper includes this when available
            }
        )

    return normalised


# ---------------------------------------------------------------------------
# Serper.dev Shopping API helper
# ---------------------------------------------------------------------------

def _call_serper_shopping(
    query: str,
    gl: str,
    num_results: int,
) -> list[dict[str, Any]]:
    """Sends a query to the Serper.dev Shopping API and returns normalised results.

    Uses the ``/shopping`` endpoint (distinct from the ``/search`` organic endpoint)
    which returns structured product listings with price, source, and rating data
    rather than web page snippets.

    Args:
        query: The product search query string.
        gl: Google country code for the search locale (e.g. ``"us"``, ``"pk"``).
        num_results: How many shopping results to request. Unlike the organic
            ``_call_serper`` helper this does NOT cap at ``_MAX_RESULTS`` because
            the shopping endpoint supports higher counts and callers need a
            larger pool before source-filtering reduces the set.

    Returns:
        A list of normalised result dicts, each containing:
        ``title`` (str), ``source`` (str), ``price`` (str), ``link`` (str),
        ``rating`` (float or None), ``rating_count`` (int or None).
        ``imageUrl`` is intentionally excluded — it is a large base64 string
        with no extraction value for downstream agents.

    Raises:
        RuntimeError: If the rate limit is reached or Serper returns a
            non-200 HTTP status.
    """
    # Enforce the shared sliding-window rate limit before every outbound call
    _check_rate_limit()

    payload = {"q": query, "gl": gl, "num": num_results}
    headers = {
        "X-API-KEY": _SERPER_API_KEY,
        "Content-Type": "application/json",
    }

    response = requests.post(
        _SERPER_SHOPPING_URL, json=payload, headers=headers, timeout=15
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Serper Shopping API returned HTTP {response.status_code}: "
            f"{response.text[:200]}"
        )

    data = response.json()

    # Extract the shopping results array; fall back to empty list if absent
    raw_results: list[dict] = data.get("shopping", [])

    normalised: list[dict[str, Any]] = []
    for item in raw_results:
        raw_rating = item.get("rating")
        raw_count = item.get("ratingCount")
        normalised.append(
            {
                "title": str(item.get("title", "")),
                "source": str(item.get("source", "")),
                "price": str(item.get("price", "")),
                "link": str(item.get("link", "")),
                "rating": float(raw_rating) if raw_rating is not None else None,
                "rating_count": int(raw_count) if raw_count is not None else None,
            }
        )

    return normalised


# ---------------------------------------------------------------------------
# MCP server instance
# ---------------------------------------------------------------------------

mcp = FastMCP("dropsmart-search")


# ---------------------------------------------------------------------------
# Tool 1: web_search
# ---------------------------------------------------------------------------

@mcp.tool()
def web_search(query: str, num_results: int = 5) -> list[dict[str, Any]]:
    """General-purpose web search via the Serper.dev API.

    Used by the Risk Assessor for trend data and by any agent that needs
    information not covered by a more specialised search tool.

    Args:
        query: The search query string. Must be non-empty.
        num_results: Number of results to return. Default 5, max 10.

    Returns:
        A list of result dicts, each with keys:
        ``title`` (str), ``snippet`` (str), ``url`` (str), ``date`` (str).

    Raises:
        ValueError: If ``query`` is empty or ``num_results`` is out of range.
        RuntimeError: On rate-limit breach or Serper API error.
    """
    # --- Input validation ---
    query = query.strip()
    if not query:
        raise ValueError("query must be a non-empty string.")

    if not isinstance(num_results, int) or num_results < 1 or num_results > _MAX_RESULTS:
        raise ValueError(
            f"num_results must be an integer between 1 and {_MAX_RESULTS}. Got: {num_results}"
        )

    # --- Audit log: record the call before it executes ---
    _log_tool_call("web_search", {"query": query, "num_results": num_results})

    try:
        results = _call_serper(query, num_results)
        _log_tool_response("web_search", "success", len(results))
        return results

    except Exception as exc:
        _log_tool_response("web_search", "error", 0)
        raise


# ---------------------------------------------------------------------------
# Tool 2: search_marketplace_fees
# ---------------------------------------------------------------------------

@mcp.tool()
def search_marketplace_fees(
    marketplace: str,
    region: str,
    business_model: str,
    product_category: str,
) -> list[dict[str, Any]]:
    """Searches for the current fee structure for a marketplace / business model combination.

    Runs 3 distinct targeted search queries to satisfy the multi-source verification
    requirement in the spec (Section 7, Rule 1). All results include the source URL
    so the Fee Agent can flag any fee that cannot be tied to a verified source.

    Args:
        marketplace: One of ``daraz_pk``, ``walmart_us``, ``amazon_us``, ``etsy_us``.
        region: ISO region code, e.g. ``"PK"`` or ``"US"``.
        business_model: One of ``dropshipping``, ``fbs``, ``fbm``.
        product_category: Product category string used to refine fee queries
            (e.g. ``"electronics"``). Use ``"general"`` when unknown.

    Returns:
        A flat list of search result dicts from all 3 queries combined.
        Each result contains: ``title``, ``snippet``, ``url``, ``date``,
        and ``query_index`` (1, 2, or 3) so results can be traced back to
        which query produced them — required for conflict resolution.

    Raises:
        ValueError: If any parameter fails validation.
        RuntimeError: On rate-limit breach or Serper API error.
    """
    # --- Input validation ---
    marketplace = marketplace.strip().lower()
    if marketplace not in _VALID_MARKETPLACES:
        raise ValueError(
            f"marketplace must be one of {sorted(_VALID_MARKETPLACES)}. Got: {marketplace!r}"
        )

    region = region.strip().upper()
    if not region:
        raise ValueError("region must be a non-empty string, e.g. 'PK' or 'US'.")

    business_model = business_model.strip().lower()
    if business_model not in _VALID_BUSINESS_MODELS:
        raise ValueError(
            f"business_model must be one of {sorted(_VALID_BUSINESS_MODELS)}. Got: {business_model!r}"
        )

    product_category = product_category.strip()
    if not product_category:
        raise ValueError("product_category must be a non-empty string. Use 'general' if unknown.")

    # --- Audit log ---
    _log_tool_call(
        "search_marketplace_fees",
        {
            "marketplace": marketplace,
            "region": region,
            "business_model": business_model,
            "product_category": product_category,
        },
    )

    # --- Build 3 targeted fee search queries ---
    # Each query approaches the fee topic from a different angle to maximise source coverage.
    current_year = datetime.now(timezone.utc).year

    # Map marketplace IDs to human-readable names used in search queries
    marketplace_labels: dict[str, str] = {
        "daraz_pk": "Daraz Pakistan",
        "walmart_us": "Walmart USA",
        "amazon_us": "Amazon USA",
        "etsy_us": "Etsy",
    }
    label = marketplace_labels[marketplace]

    # Map business models to descriptive phrases for query clarity
    business_model_labels: dict[str, str] = {
        "dropshipping": "dropshipping seller",
        "fbs": "fulfilled by seller",
        "fbm": "fulfilled by marketplace",
    }
    bm_label = business_model_labels[business_model]

    queries: list[str] = [
        # Query 1: Official seller centre / help centre source
        f"{label} seller fee structure {product_category} {current_year} commission percentage",
        # Query 2: Business model and category-specific fee breakdown
        f"{label} {bm_label} fees {product_category} commission VAT payment processing {current_year}",
        # Query 3: Site-specific or forum source for cross-verification
        f"{label} complete fee breakdown seller {product_category} {bm_label} {current_year}",
    ]

    # --- Execute all 3 queries and tag each result with its query index ---
    all_results: list[dict[str, Any]] = []

    for index, query in enumerate(queries, start=1):
        try:
            results = _call_serper(query, num_results=5)
            # Attach query_index so the Fee Agent can trace which query found each result
            for result in results:
                result["query_index"] = index
            all_results.extend(results)
        except RuntimeError as exc:
            # On rate limit, re-raise immediately so the caller can back off
            if "Rate limit" in str(exc):
                _log_tool_response("search_marketplace_fees", "error", 0)
                raise
            # On a transient Serper error for one query, continue with remaining queries
            # rather than failing the entire tool call — partial results are better than none.
            _security_logger.warning(
                f"[{datetime.now(timezone.utc).isoformat()}] "
                f"TOOL_WARN | tool=search_marketplace_fees | query_index={index} | error={exc}"
            )

    _log_tool_response("search_marketplace_fees", "success", len(all_results))
    return all_results


# ---------------------------------------------------------------------------
# Tool 3: search_supplier_prices
# ---------------------------------------------------------------------------

@mcp.tool()
def search_supplier_prices(
    product: str,
    business_model: str,
    region: str,
) -> list[dict[str, Any]]:
    """Searches for supplier options and estimated wholesale prices for a product.

    The search strategy adapts to the seller's business model:
    - ``dropshipping``: targets suppliers offering direct-to-customer shipping,
      no MOQ, blind/white-label options.
    - ``fbs``: targets bulk wholesale suppliers with low MOQ and lead-time data.
    - ``fbm``: targets suppliers able to ship to marketplace fulfillment centres
      with compliant bulk packaging.

    Args:
        product: Product name or description. Must be non-empty, max 200 characters.
        business_model: One of ``dropshipping``, ``fbs``, ``fbm``.
        region: ISO region code for shipping context, e.g. ``"PK"`` or ``"US"``.

    Returns:
        A list of search result dicts (``title``, ``snippet``, ``url``, ``date``)
        from a targeted supplier-specific query. Downstream agents parse the
        snippets to extract supplier names, cost ranges, and MOQ information.

    Raises:
        ValueError: If any parameter fails validation.
        RuntimeError: On rate-limit breach or Serper API error.
    """
    # --- Input validation ---
    product = product.strip()
    if not product:
        raise ValueError("product must be a non-empty string.")
    if len(product) > 200:
        raise ValueError("product must be 200 characters or fewer.")

    business_model = business_model.strip().lower()
    if business_model not in _VALID_BUSINESS_MODELS:
        raise ValueError(
            f"business_model must be one of {sorted(_VALID_BUSINESS_MODELS)}. Got: {business_model!r}"
        )

    region = region.strip().upper()
    if not region:
        raise ValueError("region must be a non-empty string, e.g. 'PK' or 'US'.")

    # --- Audit log ---
    _log_tool_call(
        "search_supplier_prices",
        {"product": product, "business_model": business_model, "region": region},
    )

    # --- Build a business-model-specific search query ---
    # Each model has different procurement requirements, so the query must target
    # the right supplier type to return actionable results for that model.
    if business_model == "dropshipping":
        # Dropshipping suppliers must ship direct to customer with no MOQ
        query = (
            f"{product} dropshipping supplier direct ship no MOQ "
            f"blind shipping wholesale price Alibaba AliExpress"
        )

    elif business_model == "fbs":
        # FBS sellers buy bulk stock and ship themselves — need MOQ and bulk pricing
        query = (
            f"{product} wholesale bulk supplier low MOQ unit price "
            f"bulk order pricing Alibaba AliExpress"
        )

    else:
        # fbm — seller ships to the marketplace warehouse; needs compliant packaging
        query = (
            f"{product} wholesale supplier warehouse ready bulk packaging "
            f"fulfillment center compliant labeling Alibaba"
        )

    try:
        results = _call_serper(query, num_results=8)
        _log_tool_response("search_supplier_prices", "success", len(results))
        return results

    except Exception:
        _log_tool_response("search_supplier_prices", "error", 0)
        raise


# ---------------------------------------------------------------------------
# Tool 4: search_competitor_listings
# ---------------------------------------------------------------------------

@mcp.tool()
def search_competitor_listings(
    product: str,
    marketplace: str,
    region: str,
) -> list[dict[str, Any]]:
    """Searches for live competitor product listings on the target marketplace.

    Restricts results to the target marketplace domain using a site: operator so
    that returned listings come from the platform the seller is actually entering —
    not aggregator sites or unrelated stores.

    Args:
        product: Product name. Must be non-empty, max 200 characters.
        marketplace: One of ``daraz_pk``, ``walmart_us``, ``amazon_us``, ``etsy_us``.
        region: ISO region code, e.g. ``"PK"`` or ``"US"``.

    Returns:
        A list of search result dicts (``title``, ``snippet``, ``url``, ``date``)
        restricted to the target marketplace domain. Downstream agents parse these
        to extract prices, review counts, and high-volume keywords.

    Raises:
        ValueError: If any parameter fails validation.
        RuntimeError: On rate-limit breach or Serper API error.
    """
    # --- Input validation ---
    product = product.strip()
    if not product:
        raise ValueError("product must be a non-empty string.")
    if len(product) > 200:
        raise ValueError("product must be 200 characters or fewer.")

    marketplace = marketplace.strip().lower()
    if marketplace not in _VALID_MARKETPLACES:
        raise ValueError(
            f"marketplace must be one of {sorted(_VALID_MARKETPLACES)}. Got: {marketplace!r}"
        )

    region = region.strip().upper()
    if not region:
        raise ValueError("region must be a non-empty string, e.g. 'PK' or 'US'.")

    # --- Audit log ---
    _log_tool_call(
        "search_competitor_listings",
        {"product": product, "marketplace": marketplace, "region": region},
    )

    # Resolve the marketplace domain for the site: operator so results come
    # exclusively from the target platform — not third-party review or comparison sites.
    domain = _MARKETPLACE_DOMAINS[marketplace]

    # Construct the site-restricted query — keeps results to actual live listings
    query = f"site:{domain} {product} buy price reviews"

    try:
        results = _call_serper(query, num_results=10)
        _log_tool_response("search_competitor_listings", "success", len(results))
        return results

    except Exception:
        _log_tool_response("search_competitor_listings", "error", 0)
        raise


# ---------------------------------------------------------------------------
# Tool 5: search_competitor_listings_live
# ---------------------------------------------------------------------------

@mcp.tool()
def search_competitor_listings_live(
    product: str,
    marketplace: str,
    region: str,
) -> list[dict[str, Any]]:
    """Fetches live competitor product listings from Google Shopping for a marketplace.

    Uses the Serper.dev ``/shopping`` endpoint instead of the organic ``/search``
    endpoint so results carry structured price, rating, and source fields rather
    than plain text snippets.  This makes price extraction and listing comparison
    deterministic rather than requiring text parsing.

    **Source filtering**

    Filtering behaviour depends on the marketplace:

    - ``walmart_us``: results are filtered to those whose ``source`` field
      (lowercased) contains ``"walmart"``.  This works correctly because
      Walmart appears as a source name in Google Shopping results.
    - ``amazon_us`` / ``etsy_us``: NO source filter is applied.  Google
      Shopping does not use "amazon" or "etsy" as the ``source`` field —
      it uses individual brand or seller names — so filtering by those strings
      would eliminate nearly all results.  All 30 raw results are returned.
    - ``daraz_pk``: ``search_competitor_listings_live`` is not called for
      Daraz; the CompetitorAgent uses organic ``web_search`` instead.

    **Off-marketplace fallback**

    For ``walmart_us`` only: when fewer than 3 on-marketplace results survive
    the filter the tool supplements the list with up to 5 additional results
    from the unfiltered pool (deduplicated by link).  Each supplemental item
    carries an extra ``"off_marketplace": True`` key so downstream agents can
    distinguish comparison data from genuine target-marketplace listings.

    Args:
        product: Product name or description. Must be non-empty, max 200 characters.
        marketplace: One of ``daraz_pk``, ``walmart_us``, ``amazon_us``, ``etsy_us``.
        region: ISO region code, e.g. ``"PK"`` or ``"US"``.  Used for audit
            logging; the Shopping API locale is derived from ``marketplace``.

    Returns:
        A list of normalised shopping result dicts.  On-marketplace items contain:
        ``title`` (str), ``source`` (str), ``price`` (str), ``link`` (str),
        ``rating`` (float or None), ``rating_count`` (int or None).
        Off-marketplace supplement items carry all the same keys plus
        ``"off_marketplace": True``.

    Raises:
        ValueError: If any parameter fails validation.
        RuntimeError: On rate-limit breach or Serper API error.
    """
    # --- Input validation ---
    product = product.strip()
    if not product:
        raise ValueError("product must be a non-empty string.")
    if len(product) > 200:
        raise ValueError("product must be 200 characters or fewer.")

    marketplace = marketplace.strip().lower()
    if marketplace not in _VALID_MARKETPLACES:
        raise ValueError(
            f"marketplace must be one of {sorted(_VALID_MARKETPLACES)}. Got: {marketplace!r}"
        )

    region = region.strip().upper()
    if not region:
        raise ValueError("region must be a non-empty string, e.g. 'PK' or 'US'.")

    # --- Audit log ---
    _log_tool_call(
        "search_competitor_listings_live",
        {"product": product, "marketplace": marketplace, "region": region},
    )

    # --- Marketplace → Google Shopping locale mapping ---
    _GL_MAP: dict[str, str] = {
        "daraz_pk":   "pk",
        "walmart_us": "us",
        "amazon_us":  "us",
        "etsy_us":    "us",
    }

    gl: str = _GL_MAP[marketplace]

    try:
        # Request 30 results to provide a large enough pool after any filtering.
        raw: list[dict[str, Any]] = _call_serper_shopping(
            product, gl=gl, num_results=30
        )

        if marketplace == "walmart_us":
            # Walmart appears reliably as "walmart" in Google Shopping source field —
            # filter so only genuine Walmart listings are returned.
            filtered: list[dict[str, Any]] = [
                item for item in raw
                if "walmart" in item["source"].lower()
            ]
            # Off-marketplace fallback: supplement when on-marketplace results are scarce
            if len(filtered) < 3:
                filtered_links: set[str] = {item["link"] for item in filtered}
                supplement: list[dict[str, Any]] = [
                    {**item, "off_marketplace": True}
                    for item in raw
                    if item["link"] not in filtered_links
                ][:5]
                results: list[dict[str, Any]] = filtered + supplement
            else:
                results = filtered
        else:
            # amazon_us and etsy_us: return ALL results without source filtering.
            # Google Shopping shows brand/seller names as the source field —
            # filtering by "amazon" or "etsy" would eliminate nearly all results.
            results = raw

        _log_tool_response("search_competitor_listings_live", "success", len(results))
        return results

    except Exception:
        _log_tool_response("search_competitor_listings_live", "error", 0)
        raise


# ---------------------------------------------------------------------------
# Entry point — stdio transport for local use
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # stdio transport is required for local MCP use with Google ADK
    mcp.run(transport="stdio")
