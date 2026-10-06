"""Built-in business-type templates.

A template is plain data. Adding a business type (or changing one) never touches application
code: capabilities decide behaviour, `attributes` decide which extra product fields exist, and
the rest are defaults copied into a tenant when it is provisioned.
"""

PAYMENT_METHODS = [
    {"code": "cash", "label": "Cash", "label_bn": "নগদ", "kind": "cash", "enabled": True},
    {"code": "bkash", "label": "bKash", "label_bn": "বিকাশ", "kind": "mobile", "enabled": True},
    {"code": "nagad", "label": "Nagad", "label_bn": "নগদ (Nagad)", "kind": "mobile", "enabled": True},
    {"code": "rocket", "label": "Rocket", "label_bn": "রকেট", "kind": "mobile", "enabled": False},
    {"code": "upay", "label": "Upay", "label_bn": "উপায়", "kind": "mobile", "enabled": False},
    {"code": "card", "label": "Card", "label_bn": "কার্ড", "kind": "card", "enabled": True},
    {"code": "bank", "label": "Bank transfer", "label_bn": "ব্যাংক", "kind": "bank", "enabled": False},
]


def attr(key, label, type_="text", *, label_bn="", required=False, options=None, unit=None, group="Details",
         searchable=False, filterable=False, show_in_pos=False, show_on_label=False, show_on_receipt=False):
    return {
        "key": key, "label": label, "label_bn": label_bn, "type": type_, "required": required,
        "options": options or [], "unit": unit, "group": group,
        "flags": {
            "searchable": searchable, "filterable": filterable, "show_in_pos": show_in_pos,
            "show_on_label": show_on_label, "show_on_receipt": show_on_receipt,
        },
    }


def unit(code, label, label_bn="", fraction=False):
    return {"code": code, "label": label, "label_bn": label_bn or label, "allow_fraction": fraction}


LOCAL_SHOP = {
    "key": "local_shop",
    "label": "Local Shop",
    "description": "Corner store, grocery and general trade. Fast to learn, sells loose goods by weight, keeps a customer due ledger (baki khata).",
    "family": "counter_retail",
    "sort_order": 1,
    "definition": {
        "capabilities": ["barcode_scanning", "fractional_qty", "multi_uom", "customer_credit"],
        "terminology": {"customer": "Customer", "product": "Item", "supplier": "Supplier", "due": "Baki"},
        "attributes": [
            attr("brand", "Brand", "select_or_create", label_bn="ব্র্যান্ড", searchable=True, filterable=True, show_in_pos=True),
            attr("shelf", "Shelf / rack", "text", label_bn="তাক", group="Storage"),
        ],
        "variant_axes": [],
        "stock": {"costing": "weighted_average", "allocation": "none", "allow_negative": True, "reorder_default": 5},
        "units": [
            unit("pc", "Piece", "পিস"), unit("kg", "Kilogram", "কেজি", True), unit("g", "Gram", "গ্রাম", True),
            unit("litre", "Litre", "লিটার", True), unit("dozen", "Dozen", "ডজন"), unit("pack", "Packet", "প্যাকেট"),
            unit("bag", "Bag", "বস্তা"), unit("carton", "Carton", "কার্টন"),
        ],
        "tax": {"mode": "inclusive", "cash_rounding": "none", "classes": [{"name": "No VAT", "rate": 0, "default": True}]},
        "categories": ["Rice & Grains", "Pulses", "Oil & Ghee", "Spices", "Vegetables", "Dairy & Eggs", "Beverages",
                       "Snacks & Biscuits", "Soap & Cleaning", "Stationery", "Others"],
        "pos": {"layout": "quick", "quick_keys": True, "hold_resume": True, "require_customer": False},
        "loyalty": {"enabled": False, "points_per_100": 0},
        "theme": {"accent": "#E8A200", "icon": "store"},
        "dashboard": ["today_sales", "sales_trend", "dues", "low_stock", "payment_mix", "top_products"],
        "reports": ["sales_summary", "daily_book", "due_ageing", "stock_valuation", "expenses"],
        "documents": {"receipt": "thermal_80", "invoice": "a4_simple"},
    },
}

SUPERSHOP = {
    "key": "supershop",
    "label": "Supershop",
    "description": "Barcode-first self-service retail: weighed goods with scale labels, VAT, expiry and FEFO, loyalty and cash-register shifts.",
    "family": "counter_retail",
    "sort_order": 2,
    "definition": {
        "capabilities": ["barcode_scanning", "fractional_qty", "multi_uom", "scale_barcode", "item_tax",
                         "customer_credit", "loyalty", "shifts", "lot_tracking", "expiry_tracking"],
        "terminology": {"customer": "Member", "product": "Item", "supplier": "Supplier", "due": "Credit"},
        "attributes": [
            attr("brand", "Brand", "select_or_create", label_bn="ব্র্যান্ড", searchable=True, filterable=True, show_in_pos=True),
            attr("origin", "Country of origin", "text", label_bn="উৎপত্তিস্থল", group="Sourcing"),
            attr("shelf_life_days", "Shelf life", "int", label_bn="মেয়াদ (দিন)", unit="days", group="Storage"),
            attr("aisle", "Aisle / section", "text", label_bn="সেকশন", group="Storage", filterable=True),
        ],
        "variant_axes": [],
        "stock": {"costing": "weighted_average", "allocation": "fefo", "allow_negative": False,
                  "reorder_default": 10, "expiry_alert_days": [90, 30]},
        "units": [
            unit("pc", "Piece", "পিস"), unit("kg", "Kilogram", "কেজি", True), unit("g", "Gram", "গ্রাম", True),
            unit("litre", "Litre", "লিটার", True), unit("dozen", "Dozen", "ডজন"), unit("pack", "Packet", "প্যাকেট"),
            unit("carton", "Carton", "কার্টন"),
        ],
        "tax": {"mode": "inclusive", "cash_rounding": "nearest_1", "classes": [
            {"name": "No VAT", "rate": 0, "default": True}, {"name": "VAT 5%", "rate": 5}, {"name": "VAT 15%", "rate": 15}]},
        "categories": ["Fresh Produce", "Meat & Fish", "Dairy & Eggs", "Bakery", "Rice, Oil & Staples", "Spices",
                       "Beverages", "Snacks", "Frozen", "Personal Care", "Household", "Baby"],
        "pos": {"layout": "counter", "quick_keys": True, "hold_resume": True, "require_customer": False},
        "loyalty": {"enabled": True, "points_per_100": 1},
        "theme": {"accent": "#0E9F6E", "icon": "shopping_basket"},
        "dashboard": ["today_sales", "sales_trend", "shift_status", "expiring_soon", "low_stock", "payment_mix", "top_products"],
        "reports": ["sales_summary", "department_margin", "tax_summary", "shift_z", "expiry_loss", "stock_valuation"],
        "documents": {"receipt": "thermal_80", "invoice": "a4_tax"},
    },
}

COSMETIC = {
    "key": "cosmetic",
    "label": "Cosmetic Shop",
    "description": "Beauty and personal care: shade and size variants, batches with expiry and period-after-opening, brands and loyalty.",
    "family": "counter_retail",
    "sort_order": 3,
    "definition": {
        "capabilities": ["barcode_scanning", "variants", "lot_tracking", "expiry_tracking", "item_tax",
                         "customer_credit", "loyalty", "shifts"],
        "terminology": {"customer": "Customer", "product": "Product", "supplier": "Supplier", "due": "Due"},
        "attributes": [
            attr("brand", "Brand", "select_or_create", label_bn="ব্র্যান্ড", required=True, searchable=True, filterable=True, show_in_pos=True),
            attr("skin_type", "Skin type", "multiselect", label_bn="ত্বকের ধরন", options=["Oily", "Dry", "Combination", "Sensitive", "All"], filterable=True),
            attr("pao_months", "Period after opening", "int", label_bn="খোলার পর মেয়াদ", unit="months", show_on_label=True),
            attr("origin", "Country of origin", "text", label_bn="উৎপত্তিস্থল", group="Sourcing"),
            attr("ingredients", "Key ingredients", "textarea", label_bn="উপাদান"),
        ],
        "variant_axes": [{"key": "shade", "label": "Shade", "label_bn": "শেড"}, {"key": "size", "label": "Size", "label_bn": "সাইজ"}],
        "stock": {"costing": "weighted_average", "allocation": "fefo", "allow_negative": False,
                  "reorder_default": 3, "expiry_alert_days": [90, 30]},
        "units": [unit("pc", "Piece", "পিস"), unit("set", "Set", "সেট")],
        "tax": {"mode": "inclusive", "cash_rounding": "nearest_1", "classes": [
            {"name": "No VAT", "rate": 0, "default": True}, {"name": "VAT 15%", "rate": 15}]},
        "categories": ["Skincare", "Makeup", "Hair Care", "Fragrance", "Bath & Body", "Nails", "Tools & Accessories"],
        "pos": {"layout": "counter", "quick_keys": False, "hold_resume": True, "require_customer": False},
        "loyalty": {"enabled": True, "points_per_100": 2},
        "theme": {"accent": "#D6336C", "icon": "spa"},
        "dashboard": ["today_sales", "sales_trend", "expiring_soon", "low_stock", "top_products", "payment_mix"],
        "reports": ["sales_summary", "brand_performance", "expiry_loss", "stock_valuation"],
        "documents": {"receipt": "thermal_80", "invoice": "a4_simple"},
    },
}

FURNITURE = {
    "key": "furniture",
    "label": "Furniture Showroom",
    "description": "High-value, order-based selling: quotations, advance deposits, delivery scheduling, finishes and warranty.",
    "family": "order_based",
    "sort_order": 4,
    "definition": {
        "capabilities": ["barcode_scanning", "variants", "warranty", "item_tax", "customer_credit", "document_flow",
                         "delivery", "custom_orders", "salesperson_commission"],
        "terminology": {"customer": "Client", "product": "Piece", "supplier": "Supplier", "due": "Balance"},
        "attributes": [
            attr("material", "Material", "select_or_create", label_bn="উপাদান", searchable=True, filterable=True, show_in_pos=True),
            attr("dimensions", "Dimensions (L × W × H)", "text", label_bn="মাপ", show_in_pos=True, show_on_receipt=True),
            attr("weight_kg", "Weight", "decimal", label_bn="ওজন", unit="kg", group="Logistics"),
            attr("assembly", "Assembly required", "bool", label_bn="জোড়া লাগাতে হবে", group="Logistics"),
            attr("lead_time_days", "Lead time", "int", label_bn="সময় (দিন)", unit="days", group="Logistics"),
            attr("room", "Room", "select", label_bn="কক্ষ", options=["Living", "Bedroom", "Dining", "Office", "Outdoor"], filterable=True),
        ],
        "variant_axes": [{"key": "finish", "label": "Finish / colour", "label_bn": "রং"}, {"key": "size", "label": "Size", "label_bn": "সাইজ"}],
        "stock": {"costing": "weighted_average", "allocation": "none", "allow_negative": True, "reorder_default": 1},
        "units": [unit("pc", "Piece", "পিস"), unit("set", "Set", "সেট")],
        "tax": {"mode": "exclusive", "cash_rounding": "nearest_1", "classes": [
            {"name": "No VAT", "rate": 0}, {"name": "VAT 15%", "rate": 15, "default": True}]},
        "categories": ["Sofas", "Beds", "Dining", "Wardrobes", "Tables", "Chairs", "Office", "Storage", "Decor"],
        "pos": {"layout": "order", "quick_keys": False, "hold_resume": True, "require_customer": True},
        "loyalty": {"enabled": False, "points_per_100": 0},
        "deposit_percent_min": 30,
        "theme": {"accent": "#B5651D", "icon": "chair"},
        "dashboard": ["today_sales", "open_orders", "deliveries_due", "dues", "sales_trend", "top_products"],
        "reports": ["sales_summary", "order_pipeline", "receivables", "delivery_schedule"],
        "documents": {"receipt": "thermal_80", "invoice": "a4_tax", "quotation": "a4_quote"},
    },
}

PHARMACY = {
    "key": "pharmacy",
    "label": "Pharmacy",
    "description": "Medicines with batches and expiry, strip/box units, generic names and prescription-only flags.",
    "family": "counter_retail",
    "sort_order": 5,
    "definition": {
        "capabilities": ["barcode_scanning", "multi_uom", "lot_tracking", "expiry_tracking", "regulated_items",
                         "item_tax", "customer_credit", "shifts"],
        "terminology": {"customer": "Patient", "product": "Medicine", "supplier": "Supplier", "due": "Due"},
        "attributes": [
            attr("generic_name", "Generic name", "text", label_bn="জেনেরিক নাম", searchable=True, show_in_pos=True),
            attr("strength", "Strength", "text", label_bn="শক্তি", show_on_receipt=True),
            attr("dosage_form", "Dosage form", "select", label_bn="ধরন", options=["Tablet", "Capsule", "Syrup", "Injection", "Cream", "Drops", "Inhaler", "Other"], filterable=True),
            attr("manufacturer", "Manufacturer", "select_or_create", label_bn="প্রস্তুতকারক", searchable=True, filterable=True),
            attr("rx_only", "Prescription only", "bool", label_bn="প্রেসক্রিপশন লাগবে", show_in_pos=True),
            attr("storage_condition", "Storage condition", "text", label_bn="সংরক্ষণ", group="Storage"),
        ],
        "variant_axes": [],
        "stock": {"costing": "weighted_average", "allocation": "fefo", "allow_negative": False,
                  "reorder_default": 10, "expiry_alert_days": [90, 30]},
        "units": [unit("tablet", "Tablet", "ট্যাবলেট"), unit("capsule", "Capsule", "ক্যাপসুল"), unit("ml", "Millilitre", "মিলি", True),
                  unit("strip", "Strip", "স্ট্রিপ"), unit("box", "Box", "বক্স"), unit("pc", "Piece", "পিস"), unit("bottle", "Bottle", "বোতল")],
        "tax": {"mode": "inclusive", "cash_rounding": "none", "classes": [{"name": "No VAT", "rate": 0, "default": True}]},
        "categories": ["Tablets", "Capsules", "Syrups", "Injections", "Topicals", "Baby Care", "Devices", "Supplements"],
        "pos": {"layout": "counter", "quick_keys": False, "hold_resume": True, "require_customer": False},
        "loyalty": {"enabled": False, "points_per_100": 0},
        "theme": {"accent": "#0891B2", "icon": "medication"},
        "dashboard": ["today_sales", "sales_trend", "expiring_soon", "low_stock", "dues", "top_products"],
        "reports": ["sales_summary", "expiry_loss", "stock_valuation", "due_ageing"],
        "documents": {"receipt": "thermal_80", "invoice": "a4_simple"},
    },
}

BUSINESS_TYPES = [LOCAL_SHOP, SUPERSHOP, COSMETIC, FURNITURE, PHARMACY]
