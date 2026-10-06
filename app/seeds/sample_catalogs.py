"""Demo catalogues — realistic Bangladesh product data per business type (BDT, Bangla names where it helps).

Loaded through the same `create_product` service the API uses, so the data is validated by the
business type exactly like anything a shop owner would type in.
"""
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalogue import Category, TaxClass
from app.models.platform import BusinessType
from app.schemas.catalogue import AltUnit, BarcodeIn, OpeningStockIn, ProductIn, VariantIn
from app.services import catalogue as catalogue_service


def ean13(n: int, prefix: str = "890") -> str:
    body = f"{prefix}{n:09d}"[:12]
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(body))
    return body + str((10 - total % 10) % 10)


def days(n: int) -> date:
    return date.today() + timedelta(days=n)


def V(price, cost, stock, *, options=None, bc=None, sku=None, expiry=None, lot="OPENING", plu=None, mrp=None):
    return dict(price=price, cost=cost, stock=stock, options=options or {}, bc=bc, sku=sku, expiry=expiry, lot=lot, plu=plu, mrp=mrp)


def item(name, cat, variants, *, bn="", unit="pc", frac=False, alt=None, attrs=None, tax=None, lot=False, reorder=5, warranty=0, type_="stockable"):
    return dict(name=name, cat=cat, variants=variants, bn=bn, unit=unit, frac=frac, alt=alt or [], attrs=attrs or {}, tax=tax,
                lot=lot, reorder=reorder, warranty=warranty, type=type_)


def one(price, cost, stock, **kw):
    return [V(price, cost, stock, **kw)]


# ------------------------------------------------------------------------------------------------ local shop
_n = iter(range(1, 10000))
LOCAL_SHOP = [
    item("Miniket Rice", "Rice & Grains", one(78, 70, 220, plu=None), bn="মিনিকেট চাল", unit="kg", frac=True, alt=[("bag", 50)], reorder=40, attrs={"brand": "Loose"}),
    item("Nazirshail Rice", "Rice & Grains", one(85, 76, 160), bn="নাজিরশাইল চাল", unit="kg", frac=True, alt=[("bag", 50)], reorder=40, attrs={"brand": "Loose"}),
    item("Atta (Flour)", "Rice & Grains", one(55, 48, 90), bn="আটা", unit="kg", frac=True, reorder=20, attrs={"brand": "Loose"}),
    item("Masoor Dal", "Pulses", one(125, 112, 60), bn="মসুর ডাল", unit="kg", frac=True, reorder=15),
    item("Mug Dal", "Pulses", one(140, 125, 45), bn="মুগ ডাল", unit="kg", frac=True, reorder=15),
    item("Soybean Oil 1 L", "Oil & Ghee", one(185, 172, 48, bc=ean13(next(_n))), bn="সয়াবিন তেল ১ লিটার", alt=[("carton", 12)], reorder=12, attrs={"brand": "Fresh"}),
    item("Soybean Oil (loose)", "Oil & Ghee", one(170, 158, 80), bn="সয়াবিন তেল (খোলা)", unit="litre", frac=True, reorder=20),
    item("Mustard Oil 500 ml", "Oil & Ghee", one(150, 135, 24, bc=ean13(next(_n))), bn="সরিষার তেল", reorder=6, attrs={"brand": "Radhuni"}),
    item("Sugar", "Rice & Grains", one(130, 122, 100), bn="চিনি", unit="kg", frac=True, reorder=25),
    item("Salt 1 kg", "Spices", one(40, 34, 60, bc=ean13(next(_n))), bn="লবণ ১ কেজি", reorder=15, attrs={"brand": "Molla"}),
    item("Turmeric Powder 100 g", "Spices", one(55, 45, 40, bc=ean13(next(_n))), bn="হলুদ গুঁড়া", reorder=10, attrs={"brand": "Radhuni"}),
    item("Chili Powder 100 g", "Spices", one(70, 58, 38, bc=ean13(next(_n))), bn="মরিচ গুঁড়া", reorder=10, attrs={"brand": "Radhuni"}),
    item("Cumin (Jira) 100 g", "Spices", one(95, 82, 30, bc=ean13(next(_n))), bn="জিরা", reorder=8),
    item("Onion", "Vegetables", one(65, 55, 120), bn="পেঁয়াজ", unit="kg", frac=True, reorder=25),
    item("Potato", "Vegetables", one(38, 30, 150), bn="আলু", unit="kg", frac=True, reorder=30),
    item("Garlic", "Vegetables", one(220, 190, 18), bn="রসুন", unit="kg", frac=True, reorder=5),
    item("Egg (farm)", "Dairy & Eggs", one(13, 11.5, 360), bn="ডিম", alt=[("dozen", 12)], reorder=60),
    item("Milk Powder 500 g", "Dairy & Eggs", one(520, 488, 14, bc=ean13(next(_n))), bn="গুঁড়া দুধ", reorder=4, attrs={"brand": "Diploma"}),
    item("Tea Leaf 400 g", "Beverages", one(215, 195, 22, bc=ean13(next(_n))), bn="চা পাতা", reorder=6, attrs={"brand": "Ispahani"}),
    item("Mineral Water 1 L", "Beverages", one(25, 20, 96, bc=ean13(next(_n))), bn="মিনারেল ওয়াটার", alt=[("carton", 12)], reorder=24),
    item("Soft Drink 500 ml", "Beverages", one(40, 34, 72, bc=ean13(next(_n))), bn="কোমল পানীয়", alt=[("carton", 24)], reorder=24),
    item("Cream Biscuit", "Snacks & Biscuits", one(20, 16, 120, bc=ean13(next(_n))), bn="বিস্কুট", alt=[("carton", 48)], reorder=30),
    item("Instant Noodles", "Snacks & Biscuits", one(25, 20, 90, bc=ean13(next(_n))), bn="নুডলস", alt=[("carton", 40)], reorder=24),
    item("Bread (loaf)", "Snacks & Biscuits", one(55, 48, 12), bn="পাউরুটি", reorder=6),
    item("Bath Soap", "Soap & Cleaning", one(45, 38, 60, bc=ean13(next(_n))), bn="সাবান", alt=[("dozen", 12)], reorder=12),
    item("Detergent Powder 500 g", "Soap & Cleaning", one(75, 64, 40, bc=ean13(next(_n))), bn="ডিটারজেন্ট", reorder=10),
    item("Toothpaste 100 g", "Soap & Cleaning", one(95, 80, 30, bc=ean13(next(_n))), bn="টুথপেস্ট", reorder=8),
    item("Matchbox", "Others", one(5, 4, 200), bn="দেশলাই", alt=[("dozen", 12)], reorder=50),
    item("Candle (pack of 6)", "Others", one(30, 24, 35), bn="মোমবাতি", reorder=10),
    item("Exercise Notebook", "Stationery", one(60, 48, 50, bc=ean13(next(_n))), bn="খাতা", reorder=12),
    item("Ballpoint Pen", "Stationery", one(10, 7, 150, bc=ean13(next(_n))), bn="কলম", alt=[("dozen", 12)], reorder=40),
]

# ------------------------------------------------------------------------------------------------ supershop
_s = iter(range(5000, 20000))
SUPERSHOP = [
    # weighed produce — sold from a scale label (PLU) or by tapping the tile
    item("Onion", "Fresh Produce", one(65, 55, 90, plu="10001"), bn="পেঁয়াজ", unit="kg", frac=True, reorder=20, attrs={"aisle": "Produce"}),
    item("Potato", "Fresh Produce", one(38, 30, 120, plu="10002"), bn="আলু", unit="kg", frac=True, reorder=25, attrs={"aisle": "Produce"}),
    item("Tomato", "Fresh Produce", one(90, 72, 40, plu="10003"), bn="টমেটো", unit="kg", frac=True, reorder=10, attrs={"aisle": "Produce"}),
    item("Carrot", "Fresh Produce", one(80, 62, 30, plu="10004"), bn="গাজর", unit="kg", frac=True, reorder=8, attrs={"aisle": "Produce"}),
    item("Green Chili", "Fresh Produce", one(160, 130, 9, plu="10005"), bn="কাঁচা মরিচ", unit="kg", frac=True, reorder=3, attrs={"aisle": "Produce"}),
    item("Broiler Chicken", "Meat & Fish", one(235, 205, 26, plu="10010"), bn="ব্রয়লার মুরগি", unit="kg", frac=True, reorder=8, attrs={"aisle": "Meat"}),
    item("Rohu Fish", "Meat & Fish", one(340, 290, 14, plu="10011"), bn="রুই মাছ", unit="kg", frac=True, reorder=5, attrs={"aisle": "Fish"}),
    # dairy & bakery: lots with expiry (FEFO)
    item("Fresh Milk 1 L", "Dairy & Eggs", one(105, 92, 40, bc=ean13(next(_s)), expiry=days(6), lot="MK-0904"), bn="তরল দুধ", lot=True, reorder=12, attrs={"brand": "Aarong Dairy", "shelf_life_days": 7}),
    item("Set Yoghurt 500 g", "Dairy & Eggs", one(160, 138, 24, bc=ean13(next(_s)), expiry=days(14), lot="YG-5521"), bn="মিষ্টি দই", lot=True, reorder=8, attrs={"brand": "Milk Vita", "shelf_life_days": 14}),
    item("Butter 200 g", "Dairy & Eggs", one(310, 270, 18, bc=ean13(next(_s)), expiry=days(120), lot="BT-210"), bn="মাখন", lot=True, reorder=6, tax="VAT 15%", attrs={"brand": "Danish"}),
    item("Farm Eggs (12)", "Dairy & Eggs", one(150, 132, 30, expiry=days(18), lot="EG-77"), bn="ডিম (১ ডজন)", lot=True, reorder=10),
    item("Sandwich Bread", "Bakery", one(65, 55, 25, bc=ean13(next(_s)), expiry=days(3), lot="BR-SUN"), bn="স্যান্ডউইচ ব্রেড", lot=True, reorder=10, attrs={"shelf_life_days": 4}),
    item("Chocolate Cake Slice", "Bakery", one(120, 85, 12, expiry=days(2), lot="CK-1"), bn="কেক", lot=True, reorder=4),
    # staples
    item("Miniket Rice 5 kg", "Rice, Oil & Staples", one(430, 395, 40, bc=ean13(next(_s))), bn="মিনিকেট চাল ৫ কেজি", reorder=10, attrs={"brand": "Chashi"}),
    item("Soybean Oil 2 L", "Rice, Oil & Staples", one(365, 338, 36, bc=ean13(next(_s)), expiry=days(300), lot="SO-22"), bn="সয়াবিন তেল ২ লিটার", lot=True, reorder=10, attrs={"brand": "Fresh"}),
    item("Sugar 1 kg", "Rice, Oil & Staples", one(130, 122, 60, bc=ean13(next(_s))), bn="চিনি", reorder=15),
    item("Atta 2 kg", "Rice, Oil & Staples", one(120, 104, 40, bc=ean13(next(_s))), bn="আটা ২ কেজি", reorder=10, attrs={"brand": "Teer"}),
    item("Masoor Dal 1 kg", "Rice, Oil & Staples", one(135, 118, 36, bc=ean13(next(_s))), bn="মসুর ডাল", reorder=10),
    item("Turmeric Powder 200 g", "Spices", one(105, 88, 30, bc=ean13(next(_s)), expiry=days(400), lot="TP-1"), bn="হলুদ গুঁড়া", lot=True, reorder=8, attrs={"brand": "Radhuni"}),
    # beverages & snacks (VAT)
    item("Mineral Water 1.5 L", "Beverages", one(35, 27, 120, bc=ean13(next(_s)), expiry=days(340), lot="MW-3"), bn="মিনারেল ওয়াটার", lot=True, alt=[("carton", 12)], reorder=36, tax="VAT 15%", attrs={"brand": "Mum"}),
    item("Cola 500 ml", "Beverages", one(45, 36, 96, bc=ean13(next(_s)), expiry=days(180), lot="CL-9"), bn="কোলা", lot=True, alt=[("carton", 24)], reorder=24, tax="VAT 15%"),
    item("Orange Juice 1 L", "Beverages", one(150, 124, 20, bc=ean13(next(_s)), expiry=days(40), lot="OJ-4"), bn="কমলার জুস", lot=True, reorder=6, tax="VAT 15%"),
    item("Potato Chips 50 g", "Snacks", one(40, 31, 80, bc=ean13(next(_s)), expiry=days(120), lot="PC-5"), bn="আলুর চিপস", lot=True, alt=[("carton", 24)], reorder=20, tax="VAT 15%"),
    item("Salted Peanuts 200 g", "Snacks", one(95, 76, 24, bc=ean13(next(_s)), expiry=days(25), lot="PN-2"), bn="চিনাবাদাম", lot=True, reorder=8, tax="VAT 15%"),
    item("Chocolate Bar", "Snacks", one(60, 46, 50, bc=ean13(next(_s)), expiry=days(210), lot="CH-7"), bn="চকোলেট", lot=True, reorder=15, tax="VAT 15%"),
    item("Frozen Paratha (10 pcs)", "Frozen", one(190, 158, 20, bc=ean13(next(_s)), expiry=days(150), lot="FP-3"), bn="ফ্রোজেন পরোটা", lot=True, reorder=6),
    item("Frozen Chicken Nuggets", "Frozen", one(420, 360, 10, bc=ean13(next(_s)), expiry=days(75), lot="FN-8"), bn="চিকেন নাগেটস", lot=True, reorder=4),
    # household / personal care (VAT)
    item("Dishwash Liquid 500 ml", "Household", one(135, 108, 28, bc=ean13(next(_s))), bn="ডিশ ওয়াশ", reorder=6, tax="VAT 15%"),
    item("Detergent Powder 1 kg", "Household", one(160, 132, 30, bc=ean13(next(_s))), bn="ডিটারজেন্ট ১ কেজি", reorder=8, tax="VAT 15%"),
    item("Tissue Box", "Household", one(80, 60, 40, bc=ean13(next(_s))), bn="টিস্যু", reorder=10, tax="VAT 15%"),
    item("Shampoo 340 ml", "Personal Care", one(320, 262, 18, bc=ean13(next(_s))), bn="শ্যাম্পু", reorder=5, tax="VAT 15%"),
    item("Toothpaste 150 g", "Personal Care", one(140, 112, 36, bc=ean13(next(_s))), bn="টুথপেস্ট", reorder=8, tax="VAT 15%"),
    item("Baby Diaper (M, 44)", "Baby", one(1250, 1080, 8, bc=ean13(next(_s))), bn="বেবি ডায়াপার", reorder=3, tax="VAT 15%"),
    item("Infant Formula 400 g", "Baby", one(780, 705, 9, bc=ean13(next(_s)), expiry=days(260), lot="IF-6"), bn="শিশু খাদ্য", lot=True, reorder=3),
]

# ------------------------------------------------------------------------------------------------ cosmetic
_c = iter(range(9000, 30000))


def shades(price, cost, names, stock=12, expiry_months=24, bc=True, label="shade"):
    return [V(price, cost, stock, options={label: n}, bc=ean13(next(_c), "880") if bc else None, expiry=days(30 * expiry_months), lot=f"B{expiry_months:02d}-{i + 1}") for i, n in enumerate(names)]


COSMETIC = [
    item("Velvet Matte Lipstick", "Makeup", shades(850, 520, ["Rose", "Coral", "Berry", "Nude"], expiry_months=30), lot=True, tax="VAT 15%", reorder=4,
         attrs={"brand": "Lumé", "skin_type": ["All"], "pao_months": 18, "origin": "Korea"}),
    item("Liquid Foundation 30 ml", "Makeup", shades(1650, 1040, ["Ivory", "Beige", "Sand", "Caramel"], stock=8, expiry_months=24), lot=True, tax="VAT 15%", reorder=3,
         attrs={"brand": "Noor Beauty", "skin_type": ["Oily", "Combination"], "pao_months": 12, "origin": "France"}),
    item("Eyeshadow Palette (12)", "Makeup", shades(1980, 1290, ["Sunset", "Nude Dream"], stock=6, expiry_months=36), lot=True, tax="VAT 15%", reorder=2,
         attrs={"brand": "Maison Rêve", "pao_months": 24, "origin": "Italy"}),
    item("Waterproof Kajal", "Makeup", one(280, 170, 40, bc=ean13(next(_c), "880"), expiry=days(700), lot="K1"), lot=True, tax="VAT 15%", reorder=8,
         attrs={"brand": "Lumé", "pao_months": 12, "origin": "India"}),
    item("Gel Nail Polish", "Nails", shades(320, 190, ["Red", "Pink", "Navy", "Mint", "Nude"], stock=14, expiry_months=30), lot=True, tax="VAT 15%", reorder=4,
         attrs={"brand": "Dew & Dune", "pao_months": 24, "origin": "Korea"}),
    item("Hydra Glow Moisturizer", "Skincare", [
        V(1250, 760, 14, options={"size": "50 ml"}, bc=ean13(next(_c), "880"), expiry=days(600), lot="HG50"),
        V(2100, 1300, 8, options={"size": "100 ml"}, bc=ean13(next(_c), "880"), expiry=days(600), lot="HG100")], lot=True, tax="VAT 15%", reorder=3,
         attrs={"brand": "Kaya Botanics", "skin_type": ["Dry", "Sensitive"], "pao_months": 12, "origin": "Korea", "ingredients": "Hyaluronic acid, Aloe vera"}),
    item("Vitamin C Serum 30 ml", "Skincare", one(1890, 1120, 12, bc=ean13(next(_c), "880"), expiry=days(300), lot="VC30"), lot=True, tax="VAT 15%", reorder=3,
         attrs={"brand": "Kaya Botanics", "skin_type": ["All"], "pao_months": 6, "origin": "Korea", "ingredients": "Ascorbic acid, Ferulic acid"}),
    item("Sunshield SPF 50", "Skincare", one(990, 590, 18, bc=ean13(next(_c), "880"), expiry=days(45), lot="SS50"), lot=True, tax="VAT 15%", reorder=4,
         attrs={"brand": "Dew & Dune", "skin_type": ["All"], "pao_months": 12, "origin": "Japan"}),
    item("Rose Water Toner 150 ml", "Skincare", one(560, 330, 20, bc=ean13(next(_c), "880"), expiry=days(500), lot="RW1"), lot=True, tax="VAT 15%", reorder=4,
         attrs={"brand": "Kaya Botanics", "skin_type": ["Oily", "Combination"], "pao_months": 12, "origin": "Bangladesh"}),
    item("Gentle Foaming Cleanser", "Skincare", [
        V(690, 410, 16, options={"size": "100 ml"}, bc=ean13(next(_c), "880"), expiry=days(520), lot="GC100"),
        V(1190, 720, 9, options={"size": "200 ml"}, bc=ean13(next(_c), "880"), expiry=days(520), lot="GC200")], lot=True, tax="VAT 15%", reorder=3,
         attrs={"brand": "Noor Beauty", "skin_type": ["Sensitive", "Dry"], "pao_months": 12, "origin": "France"}),
    item("Argan Hair Oil", "Hair Care", [
        V(780, 470, 12, options={"size": "100 ml"}, bc=ean13(next(_c), "880"), expiry=days(560), lot="AH100"),
        V(1380, 840, 6, options={"size": "200 ml"}, bc=ean13(next(_c), "880"), expiry=days(560), lot="AH200")], lot=True, tax="VAT 15%", reorder=3,
         attrs={"brand": "Maison Rêve", "pao_months": 18, "origin": "Morocco"}),
    item("Keratin Shampoo", "Hair Care", [
        V(620, 370, 14, options={"size": "250 ml"}, bc=ean13(next(_c), "880"), expiry=days(480), lot="KS250"),
        V(1090, 660, 8, options={"size": "500 ml"}, bc=ean13(next(_c), "880"), expiry=days(480), lot="KS500")], lot=True, tax="VAT 15%", reorder=3,
         attrs={"brand": "Dew & Dune", "pao_months": 12, "origin": "Turkey"}),
    item("Perfume Mist 100 ml", "Fragrance", shades(1450, 880, ["Jasmine", "Oud", "Vanilla"], stock=7, expiry_months=36), lot=True, tax="VAT 15%", reorder=2,
         attrs={"brand": "Maison Rêve", "pao_months": 36, "origin": "UAE"}),
    item("Body Lotion", "Bath & Body", [
        V(480, 280, 20, options={"size": "200 ml"}, bc=ean13(next(_c), "880"), expiry=days(500), lot="BL200"),
        V(820, 500, 10, options={"size": "400 ml"}, bc=ean13(next(_c), "880"), expiry=days(500), lot="BL400")], lot=True, tax="VAT 15%", reorder=4,
         attrs={"brand": "Kaya Botanics", "skin_type": ["Dry"], "pao_months": 12, "origin": "Bangladesh"}),
    item("Makeup Brush Set (12)", "Tools & Accessories", one(1350, 780, 9, bc=ean13(next(_c), "880")), tax="VAT 15%", reorder=2, attrs={"brand": "Noor Beauty", "origin": "China"}),
    item("Beauty Blender", "Tools & Accessories", one(450, 240, 24, bc=ean13(next(_c), "880")), tax="VAT 15%", reorder=5, attrs={"brand": "Lumé", "origin": "China"}),
    item("Sheet Face Mask (pack of 5)", "Skincare", one(520, 300, 30, bc=ean13(next(_c), "880"), expiry=days(240), lot="SM5"), lot=True, tax="VAT 15%", reorder=6,
         attrs={"brand": "Dew & Dune", "skin_type": ["All"], "pao_months": 1, "origin": "Korea"}),
]

# ------------------------------------------------------------------------------------------------ furniture
FURNITURE = [
    item("Oslo 3-Seater Sofa", "Sofas", [V(58000, 41000, 3, options={"finish": "Charcoal"}), V(58000, 41000, 2, options={"finish": "Sand"}), V(60500, 43000, 1, options={"finish": "Olive"})],
         tax="VAT 15%", warranty=24, reorder=1, attrs={"material": "Fabric", "dimensions": "210 × 90 × 85 cm", "weight_kg": "62", "assembly": False, "lead_time_days": 7, "room": "Living"}),
    item("Oslo 2-Seater Sofa", "Sofas", [V(43000, 30500, 2, options={"finish": "Charcoal"}), V(43000, 30500, 2, options={"finish": "Sand"})],
         tax="VAT 15%", warranty=24, reorder=1, attrs={"material": "Fabric", "dimensions": "160 × 90 × 85 cm", "weight_kg": "46", "assembly": False, "lead_time_days": 7, "room": "Living"}),
    item("Haven King Bed", "Beds", [V(72000, 51000, 2, options={"finish": "Walnut"}), V(69000, 49000, 1, options={"finish": "Oak"})],
         tax="VAT 15%", warranty=36, reorder=1, attrs={"material": "Solid wood", "dimensions": "200 × 180 × 110 cm", "weight_kg": "95", "assembly": True, "lead_time_days": 14, "room": "Bedroom"}),
    item("Haven Queen Bed", "Beds", [V(58000, 41500, 2, options={"finish": "Walnut"}), V(56000, 40000, 2, options={"finish": "Oak"})],
         tax="VAT 15%", warranty=36, reorder=1, attrs={"material": "Solid wood", "dimensions": "200 × 150 × 110 cm", "weight_kg": "82", "assembly": True, "lead_time_days": 14, "room": "Bedroom"}),
    item("Bedside Table (pair)", "Storage", one(9500, 6200, 6), tax="VAT 15%", warranty=12, attrs={"material": "Engineered wood", "dimensions": "45 × 40 × 55 cm", "room": "Bedroom"}),
    item("Aria Dining Table 6-Seater", "Dining", [V(46000, 32000, 2, options={"finish": "Teak"}), V(49000, 34500, 1, options={"finish": "Walnut"})],
         tax="VAT 15%", warranty=36, reorder=1, attrs={"material": "Teak veneer", "dimensions": "180 × 90 × 76 cm", "weight_kg": "74", "assembly": True, "lead_time_days": 10, "room": "Dining"}),
    item("Aria Dining Chair", "Chairs", [V(6500, 4200, 14, options={"finish": "Beige"}), V(6500, 4200, 12, options={"finish": "Grey"})],
         tax="VAT 15%", warranty=12, reorder=4, attrs={"material": "Fabric + wood", "dimensions": "45 × 52 × 92 cm", "room": "Dining"}),
    item("Compass Wardrobe 3-Door", "Wardrobes", [V(54000, 38000, 2, options={"finish": "White"}), V(57000, 40500, 1, options={"finish": "Walnut"})],
         tax="VAT 15%", warranty=36, reorder=1, attrs={"material": "MDF + laminate", "dimensions": "150 × 55 × 200 cm", "weight_kg": "110", "assembly": True, "lead_time_days": 14, "room": "Bedroom"}),
    item("Study Desk", "Office", one(18500, 12500, 5), tax="VAT 15%", warranty=12, attrs={"material": "Engineered wood", "dimensions": "120 × 60 × 75 cm", "assembly": True, "room": "Office"}),
    item("Ergo Office Chair", "Office", [V(14500, 9800, 6, options={"finish": "Black"}), V(14500, 9800, 4, options={"finish": "Grey"})],
         tax="VAT 15%", warranty=24, reorder=2, attrs={"material": "Mesh + steel", "dimensions": "62 × 62 × 110 cm", "room": "Office"}),
    item("Bookshelf 5-Tier", "Storage", one(12500, 8200, 4), tax="VAT 15%", warranty=12, attrs={"material": "Engineered wood", "dimensions": "80 × 30 × 180 cm", "assembly": True, "room": "Living"}),
    item("Coffee Table", "Tables", [V(11500, 7600, 4, options={"finish": "Walnut"}), V(11500, 7600, 3, options={"finish": "White"})],
         tax="VAT 15%", warranty=12, attrs={"material": "Wood + glass", "dimensions": "100 × 55 × 42 cm", "room": "Living"}),
    item("TV Cabinet", "Storage", one(22500, 15500, 3), tax="VAT 15%", warranty=24, attrs={"material": "Engineered wood", "dimensions": "160 × 40 × 50 cm", "weight_kg": "48", "room": "Living"}),
    item("Wall Mirror (round)", "Decor", one(5200, 3100, 8), tax="VAT 15%", attrs={"material": "Glass + metal", "dimensions": "80 cm dia.", "room": "Living"}),
]

# ------------------------------------------------------------------------------------------------ pharmacy
_p = iter(range(20000, 40000))
PHARMACY = [
    item("Napa 500 mg", "Tablets", [V(1.2, 0.95, 2000, bc=ean13(next(_p), "894"), expiry=days(500), lot="NP-1")], unit="tablet", lot=True, alt=[("strip", 10), ("box", 100)], reorder=200,
         attrs={"generic_name": "Paracetamol", "strength": "500 mg", "dosage_form": "Tablet", "manufacturer": "Beximco", "rx_only": False}),
    item("Seclo 20 mg", "Capsules", [V(7, 5.6, 900, bc=ean13(next(_p), "894"), expiry=days(420), lot="SC-2")], unit="capsule", lot=True, alt=[("strip", 10)], reorder=100,
         attrs={"generic_name": "Omeprazole", "strength": "20 mg", "dosage_form": "Capsule", "manufacturer": "Square", "rx_only": False}),
    item("Monas 10 mg", "Tablets", [V(16, 12.5, 400, bc=ean13(next(_p), "894"), expiry=days(35), lot="MN-9")], unit="tablet", lot=True, alt=[("strip", 10)], reorder=80,
         attrs={"generic_name": "Montelukast", "strength": "10 mg", "dosage_form": "Tablet", "manufacturer": "ACME", "rx_only": True}),
    item("Amdocal 5 mg", "Tablets", [V(3.5, 2.6, 800, bc=ean13(next(_p), "894"), expiry=days(600), lot="AM-4")], unit="tablet", lot=True, alt=[("strip", 10)], reorder=100,
         attrs={"generic_name": "Amlodipine", "strength": "5 mg", "dosage_form": "Tablet", "manufacturer": "Square", "rx_only": True}),
    item("Zimax 500 mg", "Tablets", [V(42, 34, 300, bc=ean13(next(_p), "894"), expiry=days(380), lot="ZM-6")], unit="tablet", lot=True, alt=[("strip", 6)], reorder=60,
         attrs={"generic_name": "Azithromycin", "strength": "500 mg", "dosage_form": "Tablet", "manufacturer": "Square", "rx_only": True}),
    item("Ace Syrup 60 ml", "Syrups", [V(55, 44, 60, bc=ean13(next(_p), "894"), expiry=days(300), lot="AS-1")], unit="bottle", lot=True, reorder=10,
         attrs={"generic_name": "Paracetamol", "strength": "120 mg/5 ml", "dosage_form": "Syrup", "manufacturer": "Square", "rx_only": False}),
    item("ORS Saline (sachet)", "Supplements", [V(8, 6, 400, bc=ean13(next(_p), "894"), expiry=days(700), lot="OR-3")], unit="pc", lot=True, reorder=60,
         attrs={"generic_name": "Oral rehydration salts", "dosage_form": "Other", "manufacturer": "Square", "rx_only": False}),
    item("Digital Thermometer", "Devices", one(280, 190, 14, bc=ean13(next(_p), "894")), unit="pc", reorder=3, attrs={"manufacturer": "Omron"}),
]

CATALOGS = {"local_shop": LOCAL_SHOP, "supershop": SUPERSHOP, "cosmetic": COSMETIC, "furniture": FURNITURE, "pharmacy": PHARMACY}


def D(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"))


async def load_sample_catalog(session: AsyncSession, *, tenant, owner) -> int:
    bt = await session.get(BusinessType, tenant.business_type_id)
    specs = CATALOGS.get(bt.key if bt else "", [])
    cats = {c.name: c.id for c in (await session.execute(select(Category).where(Category.tenant_id == tenant.id))).scalars()}
    taxes = {t.name: t.id for t in (await session.execute(select(TaxClass).where(TaxClass.tenant_id == tenant.id))).scalars()}
    caps = set((tenant.capability_overrides or {}).get("base", []))
    created = 0
    for s in specs:
        variants = []
        for v in s["variants"]:
            stock = None
            if v["stock"]:
                stock = OpeningStockIn(qty=Decimal(str(v["stock"])), unit_cost=D(v["cost"]), lot_no=v["lot"], expiry=v["expiry"] if s["lot"] else None)
            variants.append(VariantIn(
                sku=v["sku"], option_values=v["options"] if "variants" in caps else {}, price=D(v["price"]), cost=D(v["cost"]),
                mrp=D(v["mrp"] or 0), plu=v["plu"] if "scale_barcode" in caps else None,
                barcodes=[BarcodeIn(code=v["bc"])] if v["bc"] else [], opening_stock=stock))
        if "variants" not in caps and len(variants) > 1:
            variants = variants[:1]
        payload = ProductIn(
            name=s["name"], name_local=s["bn"], category_id=cats.get(s["cat"]), type=s["type"], base_unit=s["unit"],
            alt_units=[AltUnit(unit=u, factor=Decimal(f)) for u, f in s["alt"]] if "multi_uom" in caps else [],
            allow_fraction=s["frac"] and "fractional_qty" in caps, tracking="lot" if s["lot"] and "lot_tracking" in caps else "none",
            track_expiry=s["lot"] and "expiry_tracking" in caps, tax_class_id=taxes.get(s["tax"]) if s["tax"] and "item_tax" in caps else None,
            attributes=s["attrs"], reorder_level=Decimal(str(s["reorder"])), warranty_months=s["warranty"] if "warranty" in caps else 0, variants=variants)
        await catalogue_service.create_product(session, tenant=tenant, payload=payload)
        created += 1
    return created
