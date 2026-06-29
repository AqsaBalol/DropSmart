"""DropSmart entry point.

Orchestrates the full product intelligence pipeline. Collects user input,
validates it, runs the six specialist agents and the report generator,
and presents the final results.
"""

# --- Standard library ---
import logging
from typing import Any

# --- Third-party ---
from dotenv import load_dotenv

# --- Local ---
from agents.orchestrator import DropSmartOrchestrator


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _setup_logging() -> None:
    """Configures the root logger and all child loggers for DropSmart."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )


# ---------------------------------------------------------------------------
# User input collection
# ---------------------------------------------------------------------------

def get_user_input() -> dict[str, Any]:
    """Collects all required user inputs for a product research run.

    Prompts the user for product name, marketplace, business model, costs,
    and conditionally for province (Daraz Pakistan only). Shows numbered
    menus for multi-choice fields. Returns a dict ready for the Orchestrator.

    Returns:
        A dict with keys: ``product_name``, ``marketplace``, ``business_model``,
        ``packaging_cost``, ``courier_cost``, and optionally ``province``.
    """
    print("\n" + "=" * 60)
    print("DropSmart Product Intelligence System")
    print("=" * 60 + "\n")

    # --- Product name (required) ---
    while True:
        product_name = input("Enter product name: ").strip()
        if product_name:
            break
        print("Product name cannot be empty. Please try again.")

    # --- Marketplace (show menu) ---
    print("\nSelect marketplace:")
    marketplaces = [
        ("daraz_pk", "Daraz Pakistan"),
        ("walmart_us", "Walmart USA"),
        ("amazon_us", "Amazon USA"),
        ("etsy_us", "Etsy USA"),
    ]
    for i, (code, label) in enumerate(marketplaces, 1):
        print(f"  {i}. {label}")

    while True:
        choice = input("Enter choice (1-4): ").strip()
        if choice in ("1", "2", "3", "4"):
            marketplace = marketplaces[int(choice) - 1][0]
            break
        print("Invalid choice. Please enter 1-4.")

    # --- Business model (show menu) ---
    print("\nSelect business model:")
    business_models = [
        ("dropshipping", "Dropshipping"),
        ("fulfilled_by_seller", "Fulfilled by Seller (FBS)"),
        ("fulfilled_by_marketplace", "Fulfilled by Marketplace (FBM/FBA)"),
    ]
    for i, (code, label) in enumerate(business_models, 1):
        print(f"  {i}. {label}")

    while True:
        choice = input("Enter choice (1-3): ").strip()
        if choice in ("1", "2", "3"):
            business_model = business_models[int(choice) - 1][0]
            break
        print("Invalid choice. Please enter 1-3.")

    # --- Costs (with defaults) ---
    packaging_cost_str = input("Packaging cost per unit (default 0.0): ").strip()
    packaging_cost = float(packaging_cost_str) if packaging_cost_str else 0.0

    courier_cost_str = input("Courier/shipping cost per unit (default 0.0): ").strip()
    courier_cost = float(courier_cost_str) if courier_cost_str else 0.0

    # --- Province (only for Daraz Pakistan) ---
    user_input = {
        "product_name": product_name,
        "marketplace": marketplace,
        "business_model": business_model,
        "packaging_cost": packaging_cost,
        "courier_cost": courier_cost,
    }

    if marketplace == "daraz_pk":
        print("\nSelect province:")
        provinces = ["Punjab", "Sindh", "KPK", "Balochistan"]
        for i, province in enumerate(provinces, 1):
            print(f"  {i}. {province}")

        while True:
            choice = input("Enter choice (1-4, default 2 for Sindh): ").strip()
            if not choice:
                province = "Sindh"
                break
            if choice in ("1", "2", "3", "4"):
                province = provinces[int(choice) - 1]
                break
            print("Invalid choice. Please enter 1-4.")

        user_input["province"] = province

    return user_input


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Executes the full DropSmart pipeline.

    Loads environment variables, collects user input, instantiates the
    Orchestrator, runs the pipeline, and presents the final results.
    Handles validation errors gracefully by re-prompting the user.
    """
    # Load environment variables so agents can access GEMINI_API_KEY
    load_dotenv()

    # Set up logging so all agents can log effectively
    _setup_logging()

    logger = logging.getLogger("dropsmart.main")
    logger.info("DropSmart pipeline starting...")

    try:
        # Collect user input
        user_input = get_user_input()

        # Instantiate and run the orchestrator
        orchestrator = DropSmartOrchestrator()
        final_context = orchestrator.run(user_input)

        # Present results
        status = final_context.get("status", "unknown")
        print(f"\n{'=' * 60}")
        print(f"Pipeline Status: {status.upper()}")
        print(f"{'=' * 60}\n")

        if status == "completed":
            report_result = final_context.get("report_result", {})
            filepath = report_result.get("report_filepath", "unknown")
            print(f"✓ Report generated and saved to:")
            print(f"  {filepath}\n")
        else:
            print("Pipeline was cancelled at the HITL checkpoint.\n")

        logger.info("Pipeline completed with status: %s", status)

    except ValueError as exc:
        print(f"\n✗ Input validation error: {exc}\n")
        logger.error("Validation failed: %s", exc)
    except Exception as exc:
        print(f"\n✗ Unexpected error: {exc}\n")
        logger.error("Pipeline failed: %s", exc, exc_info=True)


if __name__ == "__main__":
    main()
