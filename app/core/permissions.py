"""Role gate: what a *user* may do. (Plan gate = features, capability gate = business type.)"""
from enum import StrEnum


class Permission(StrEnum):
    sell = "sell"
    apply_discount = "apply_discount"
    override_price = "override_price"
    void_sale = "void_sale"
    refund = "refund"
    view_sales = "view_sales"
    view_inventory = "view_inventory"
    manage_products = "manage_products"
    adjust_stock = "adjust_stock"
    view_cost = "view_cost"
    manage_purchases = "manage_purchases"
    manage_customers = "manage_customers"
    collect_dues = "collect_dues"
    manage_expenses = "manage_expenses"
    manage_shifts = "manage_shifts"
    manage_orders = "manage_orders"
    view_reports = "view_reports"
    view_margin = "view_margin"
    export_data = "export_data"
    manage_staff = "manage_staff"
    manage_branches = "manage_branches"
    manage_settings = "manage_settings"
    manage_subscription = "manage_subscription"


P = Permission

_CASHIER = {P.sell, P.view_sales, P.view_inventory, P.manage_customers, P.collect_dues, P.manage_shifts}
_SALESPERSON = {P.sell, P.view_sales, P.view_inventory, P.manage_customers, P.manage_orders}
_STOCK_KEEPER = {P.view_inventory, P.manage_products, P.adjust_stock, P.manage_purchases, P.view_cost}
_ACCOUNTANT = {
    P.view_sales, P.view_inventory, P.view_cost, P.view_reports, P.view_margin,
    P.export_data, P.manage_expenses, P.collect_dues, P.manage_customers,
}
_MANAGER = _CASHIER | _STOCK_KEEPER | {
    P.apply_discount, P.override_price, P.void_sale, P.refund, P.manage_expenses,
    P.view_reports, P.view_margin, P.export_data, P.manage_orders,
}
_OWNER = set(Permission)

ROLE_PERMISSIONS: dict[str, set[Permission]] = {
    "super_admin": set(),
    "owner": _OWNER,
    "manager": _MANAGER,
    "cashier": _CASHIER,
    "salesperson": _SALESPERSON,
    "stock_keeper": _STOCK_KEEPER,
    "accountant": _ACCOUNTANT,
}

ROLE_LABELS: dict[str, str] = {
    "owner": "Owner",
    "manager": "Manager",
    "cashier": "Cashier",
    "salesperson": "Salesperson",
    "stock_keeper": "Stock keeper",
    "accountant": "Accountant",
}

ASSIGNABLE_ROLES = ["owner", "manager", "cashier", "salesperson", "stock_keeper", "accountant"]

# Owner-only: the billing relationship with the platform.
UNGRANTABLE = {P.manage_subscription}


def effective_permissions(role: str, overrides: list[str] | None) -> set[Permission]:
    """`None` inherits the role; `[]` is an explicit "nothing". The difference is load-bearing:
    revoking an account fully must not be undone by a later widening of the role default."""
    if overrides is None:
        return set(ROLE_PERMISSIONS.get(role, set()))
    valid = {p.value for p in Permission}
    return {Permission(p) for p in overrides if p in valid}
