"""The closed catalogue of engine capabilities.

A *capability* is something the engine can do (track expiry, sell by weight,
run quotations…). Business types are *data* that switch capabilities on; code
only ever asks "does this tenant have capability X?" — never "is this tenant a
furniture shop?". The set is curated by engineering, each entry declares what
it needs, and `status` tells the admin console whether it is shipped yet.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class CapabilitySpec:
    key: str
    label: str
    description: str
    requires: tuple[str, ...] = ()
    status: str = "ready"  # ready | planned
    group: str = "Selling"


CAPABILITIES: dict[str, CapabilitySpec] = {c.key: c for c in [
    CapabilitySpec("barcode_scanning", "Barcode scanning", "Scan-to-cart, several barcodes per item.", group="Selling"),
    CapabilitySpec("fractional_qty", "Sell by weight / length", "Decimal quantities (kg, m, litre).", group="Selling"),
    CapabilitySpec("multi_uom", "Multiple units", "Sell in dozen, carton or strip with automatic conversion.", group="Selling"),
    CapabilitySpec("scale_barcode", "Scale barcodes", "Weight-embedded labels from deli/produce scales.", ("fractional_qty",), group="Selling"),
    CapabilitySpec("item_tax", "Item-level VAT", "Tax class per item, tax lines on the invoice.", group="Selling"),
    CapabilitySpec("customer_credit", "Customer credit (baki / due)", "Per-customer due ledger with collection.", group="Selling"),
    CapabilitySpec("loyalty", "Loyalty points", "Earn points on every sale.", ("customer_credit",), group="Selling"),
    CapabilitySpec("shifts", "Register shifts", "Opening float, cash count, Z-report.", group="Selling"),
    CapabilitySpec("promotions", "Promotions", "Rule-based discounts (BOGO, % by category).", status="planned", group="Selling"),
    CapabilitySpec("variants", "Variants", "Size / shade / colour options with their own SKU, price and barcode.", group="Catalogue"),
    CapabilitySpec("bundles", "Bundles & kits", "Gift sets and kits that consume component stock.", status="planned", group="Catalogue"),
    CapabilitySpec("lot_tracking", "Batch / lot tracking", "Lot numbers on stock received.", group="Inventory"),
    CapabilitySpec("expiry_tracking", "Expiry dates", "FEFO selling and expiry alerts.", ("lot_tracking",), group="Inventory"),
    CapabilitySpec("serial_tracking", "Serial numbers", "One serial per unit, traced to the sale.", status="planned", group="Inventory"),
    CapabilitySpec("warranty", "Warranty", "Warranty period printed on the invoice.", group="Inventory"),
    CapabilitySpec("multi_location", "Multiple locations", "Showroom, godown and shelf locations.", status="planned", group="Inventory"),
    CapabilitySpec("regulated_items", "Regulated items", "Prescription-only flag and customer profile.", ("lot_tracking",), group="Inventory"),
    CapabilitySpec("document_flow", "Quotation → order → delivery", "Order-based selling with advance deposits.", group="Orders"),
    CapabilitySpec("delivery", "Delivery scheduling", "Delivery date, address and charge on orders.", ("document_flow",), group="Orders"),
    CapabilitySpec("custom_orders", "Made-to-order", "Spec notes and lead time on order lines.", ("document_flow",), group="Orders"),
    CapabilitySpec("installments", "Instalments", "Payment plans with a schedule.", ("customer_credit",), status="planned", group="Orders"),
    CapabilitySpec("salesperson_commission", "Salesperson attribution", "Tag each sale with the salesperson.", group="Orders"),
]}


def resolve_capabilities(
    *,
    template: list[str],
    enabled: list[str] | None = None,
    disabled: list[str] | None = None,
    plan_allowed: list[str] | None = None,
) -> list[str]:
    """effective = ((template ∪ enabled) − disabled) ∩ plan-allowed, closed over `requires`.

    Unknown keys are dropped and a capability whose requirement is missing is dropped too,
    so a stale template can never switch on half of a feature.
    """
    chosen = (set(template) | set(enabled or [])) - set(disabled or [])
    chosen &= set(CAPABILITIES)
    if plan_allowed is not None:
        chosen &= set(plan_allowed)
    changed = True
    while changed:
        changed = False
        for key in list(chosen):
            if any(req not in chosen for req in CAPABILITIES[key].requires):
                chosen.discard(key)
                changed = True
    return sorted(chosen)


# Plan *features* are the paid modules; they are independent of business type.
FEATURES: dict[str, str] = {
    "pos": "Point of sale",
    "inventory": "Inventory & catalogue",
    "purchases": "Purchases & suppliers",
    "customers": "Customers & due ledger",
    "reports": "Standard reports",
    "reports_advanced": "Advanced reports (margin, exceptions)",
    "offline_sync": "Offline selling with automatic sync",
    "multi_branch": "Multiple branches",
    "staff": "Staff accounts & roles",
    "expenses": "Expenses & day book",
    "export": "Data export (CSV)",
    "orders": "Orders & quotations",
}

# What a plan limit key means (-1 = unlimited).
LIMIT_KEYS: dict[str, str] = {
    "products": "Products",
    "staff": "Staff accounts",
    "branches": "Branches",
    "devices": "Registered devices",
    "sales_per_month": "Sales per month",
}
