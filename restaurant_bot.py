"""
WhatsApp Chatbot — BTT (Bite Tongue Tingling) Restaurant
=========================================================
Handles:
  - Menu queries (category-wise)
  - Order taking (Claude parses free text → bill → owner notified)
  - Table booking (collect details → owner notified)
  - Location & hours

Conversation states per phone number:
  None             → new user / welcome
  "main_menu"      → shown main options, waiting for reply
  "menu_browse"    → browsing menu categories
  "ordering"       → customer adding items to cart
  "order_confirm"  → reviewing cart before submitting
  "booking_name"   → table booking: collecting name
  "booking_date"   → table booking: collecting date
  "booking_time"   → table booking: collecting time
  "booking_people" → table booking: collecting party size
  "booking_phone"  → table booking: collecting contact number
  "booking_confirm"→ confirming table booking details
"""

import os
import json
import asyncio
import httpx
import anthropic
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

# ── Config ─────────────────────────────────────────────────────────────────────
WHATSAPP_TOKEN  = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
OWNER_WHATSAPP  = os.getenv("RESTAURANT_OWNER_WA", os.getenv("OWNER_WHATSAPP", "919953910987"))
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

WA_API_BASE = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}"

# In-memory sessions: phone → {state, cart, booking, ...}
sessions: dict = {}

# ── Restaurant Info ────────────────────────────────────────────────────────────
RESTAURANT_NAME    = "BTT - Bite Tongue Tingling"
RESTAURANT_PHONE   = "7428136136"
RESTAURANT_ADDRESS = "P9A, Opposite Street Number 18, Pratap Nagar, New Delhi - 110007"
RESTAURANT_HOURS   = "11:00 AM – 11:00 PM (All days)"
ZOMATO_LINK        = "https://zomato.com"  # update with actual Zomato link

# ── Full Menu ──────────────────────────────────────────────────────────────────
MENU = {
    "mocktails": {
        "Mojito": 70, "Lemon Soda": 65, "Blue Lagoon": 75,
        "Green Apple": 75, "Passion Fruit": 75, "Raspberry": 75,
        "Melon Blossom": 75, "Raspberry Mojito": 80, "Water Melon": 80,
        "Green Apple Mojito": 80, "Malt Xxx": 75,
    },
    "fruit_beer": {
        "Raspberry Beer": 85, "Watermelon Beer": 65,
        "Strawberry Beer": 85, "Berries Beer": 85,
    },
    "thick_shakes": {
        "Chocolate Thick Shake": 100, "Choco-Oreo Thick Shake": 100,
        "Choco Mint Oreo Thick Shake": 100, "Kitkat Thick Shake": 100,
        "Choco Brownie Thick Shake": 100, "Nutella Thick Shake": 110,
        "Black Forest Thick Shake": 110, "Protein Smoothie": 129,
    },
    "milk_shakes": {
        "Vanilla Milkshake": 79, "Pineapple Milkshake": 79,
        "Mango Milkshake": 79, "Butter Scotch Milkshake": 79,
        "Strawberry Milkshake": 79, "Berry Berry Milkshake": 79,
        "Banana Strawberry Milkshake": 89,
    },
    "tea": {
        "Assam Tea": 30, "Masala Tea": 30, "Green Tea": 30,
    },
    "iced_tea": {
        "Peach Iced Tea": 75, "Lemon Iced Tea": 75, "Strawberry Iced Tea": 75,
    },
    "coffee": {
        "Cold Coffee": 80, "Caramel Cold Coffee": 90,
        "Irish Cold Coffee": 90, "Hazelnut Cold Coffee": 90,
        "Hot Coffee": 40, "Black Coffee": 25,
    },
    "extras": {
        "Any Ice Cream": 39, "Any Syrup": 15,
    },
    "waffles_single": {
        "Classic Waffle": 90, "Chocolate Waffle": 100, "Nutella Waffle": 110,
        "Strawberry Waffle": 100, "Blueberry Waffle": 100, "Oreo Waffle": 100,
        "White Waffle": 100, "Choco-Brownie Waffle": 120,
        "Banana Honey Waffle": 110, "BTT Special Waffle": 130,
    },
    "waffles_double": {
        "Classic Waffle (Double)": 170, "Chocolate Waffle (Double)": 190,
        "Nutella Waffle (Double)": 200, "Strawberry Waffle (Double)": 190,
        "Blueberry Waffle (Double)": 190, "Oreo Waffle (Double)": 190,
        "White Waffle (Double)": 190, "Choco-Brownie Waffle (Double)": 230,
        "Banana Honey Waffle (Double)": 200, "BTT Special Waffle (Double)": 240,
        "Waffle Platter (Any 4)": 250,
    },
    "brownies": {
        "Hot Brownie with Icecream": 100,
        "Hot Brownie Fudge": 110,
        "Hot Chocolate Brownie": 80,
    },
    "chinese_snacks_half": {
        "Veg Manchurian Gravy (Half)": 70, "Veg Manchurian Dry (Half)": 80,
        "Paneer Manchurian (Half)": 110, "Chilli Paneer Dry (Half)": 100,
        "Chilli Paneer Gravy (Half)": 100, "Chilli Mushroom (Half)": 80,
        "Chilli Potato (Half)": 60, "Honey Chilli Potato (Half)": 80,
        "Veg Spring Roll": 70, "Kurkure Spring Roll": 100,
        "Vey Chopsey": 100, "Paneer Chopsey": 120, "American Chopsey": 140,
    },
    "chinese_snacks_full": {
        "Veg Manchurian Gravy (Full)": 140, "Veg Manchurian Dry (Full)": 150,
        "Paneer Manchurian (Full)": 200, "Chilli Paneer Dry (Full)": 190,
        "Chilli Paneer Gravy (Full)": 190, "Chilli Mushroom (Full)": 150,
        "Chilli Potato (Full)": 100, "Honey Chilli Potato (Full)": 120,
    },
    "noodles_half": {
        "Veg Noodles (Half)": 60, "Veg Singapore Noodles (Half)": 80,
        "Chilli Garlic Noodles (Half)": 70, "Garlic Noodles (Half)": 70,
        "Paneer Noodles (Half)": 80, "Hakka Noodles (Half)": 80,
        "Butter Noodles (Half)": 80,
    },
    "noodles_full": {
        "Veg Noodles (Full)": 90, "Veg Singapore Noodles (Full)": 120,
        "Chilli Garlic Noodles (Full)": 100, "Garlic Noodles (Full)": 100,
        "Paneer Noodles (Full)": 120, "Hakka Noodles (Full)": 120,
        "Butter Noodles (Full)": 110, "Chilli Oil Noodles": 130,
    },
    "momos": {
        "Veg Steam Momos (Half)": 40, "Veg Steam Momos (Full)": 70,
        "Paneer Steam Momos (Half)": 50, "Paneer Steam Momos (Full)": 80,
        "Veg Fried Momos (Half)": 45, "Veg Fried Momos (Full)": 80,
        "Paneer Fried Momos (Half)": 60, "Paneer Fried Momos (Full)": 110,
        "Veg Kurkure Momos (Half)": 60, "Veg Kurkure Momos (Full)": 90,
        "Paneer Kurkure Momos (Half)": 70, "Paneer Kurkure Momos (Full)": 130,
        "Masala Gravy Veg Fried Momos (Half)": 50, "Masala Gravy Veg Fried Momos (Full)": 90,
        "Masala Gravy Paneer Fried Momos (Half)": 60, "Masala Gravy Paneer Fried Momos (Full)": 120,
        "Peri-Peri Gravy Veg Fried Momos (Half)": 50, "Peri-Peri Gravy Veg Fried Momos (Full)": 90,
        "Peri-Peri Gravy Paneer Fried Momos (Half)": 60, "Peri-Peri Gravy Paneer Fried Momos (Full)": 120,
        "Cheese Gravy Veg Momos (Half)": 55, "Cheese Gravy Veg Momos (Full)": 90,
        "Cheese Gravy Paneer Momos (Half)": 65, "Cheese Gravy Paneer Momos (Full)": 120,
        "Afghani Veg Fried Momos (Half)": 60, "Afghani Veg Fried Momos (Full)": 110,
        "Afghani Paneer Fried Momos (Half)": 70, "Afghani Paneer Fried Momos (Full)": 130,
        "Chilli Momos": 120,
    },
    "rice": {
        "Veg Fried Rice (Half)": 60, "Veg Fried Rice (Full)": 100,
        "Chilli Garlic Fried Rice (Half)": 70, "Chilli Garlic Fried Rice (Full)": 110,
        "Veg Singapore Fried Rice (Half)": 80, "Veg Singapore Fried Rice (Full)": 120,
        "Schezwan Fried Rice (Half)": 70, "Schezwan Fried Rice (Full)": 110,
        "Paneer Fried Rice (Half)": 80, "Paneer Fried Rice (Full)": 120,
        "Lemon Rice": 90, "Curd Rice": 90,
    },
    "dosa": {
        "Paper Dosa": 80, "Masala Dosa": 100, "Onion Plain Dosa": 90,
        "Onion Masala Dosa": 110, "Mysore Masala Dosa": 110,
        "Schezwan Dosa": 110, "Cheese Plain Dosa": 120,
        "Cheese Masala Dosa": 130, "Butter Masala Dosa": 110,
        "Paneer Dosa": 140, "Chilli Paneer Dosa": 140,
        "Manchurian Dosa": 130, "Family Dosa": 160,
    },
    "uttapam": {
        "Onion Uttapam": 100, "Tomato Uttapam": 100,
        "Onion and Tomato Uttapam": 110, "Mix Vegetable Uttapam": 120,
        "Paneer Uttapam": 140, "Coconut Uttapam": 110,
    },
    "rawa_dosa": {
        "Rawa Dosa": 100, "Rawa Masala Dosa": 110,
        "Rawa Onion Masala Dosa": 120, "Rawa Butter Masala Dosa": 120,
        "Rawa Paneer Masala Dosa": 140, "Coconut Rawa Masala Dosa": 130,
    },
    "south_indian_snacks": {
        "Sambhar Vada": 70, "Sambhar Idli": 60, "Fried Idli": 70,
        "Chilli Idli": 90, "Dahi Vada": 80, "Upma": 80,
    },
    "burgers": {
        "Aloo Tikki Burger": 50, "Veg Surprise Burger": 75,
        "Crispy Paneer Surprise Burger": 95, "Veg Chilli Lava Burger": 75,
        "Crispy Paneer Chilli Lava Burger": 95, "Veg Cheese Shot Burger": 100,
        "Paneer Maharaja Burger": 130,
    },
    "pasta": {
        "Mix Sauce Pasta": 110, "Red Sauce Pasta": 100,
        "White Sauce Pasta": 110, "Cheese Sauce Pasta": 130,
        "Mac N Cheese Pasta": 150,
    },
    "sandwiches": {
        "Veggie Sandwich (2pcs)": 70, "Veggie Sandwich (4pcs)": 130,
        "Paneer Tikka Sandwich (2pcs)": 85, "Paneer Tikka Sandwich (4pcs)": 150,
        "Paneer Makhni Sandwich (2pcs)": 85, "Paneer Makhni Sandwich (4pcs)": 150,
        "Paneer Chilli Lava Sandwich (2pcs)": 85, "Paneer Chilli Lava Sandwich (4pcs)": 150,
    },
    "fries": {
        "Classic Fries (Salted)": 70, "Peri-Peri Fries (Dry)": 90,
        "Peri-Peri Fries (Gravy)": 95, "Cheese Loaded Fries": 90,
        "Peri-Peri Cheesy Fries": 100,
    },
    "wraps": {
        "Jalapeno Wrap": 85, "Fajita Wrap": 85,
        "Crispy Paneer Wrap": 110, "Chilly Patty Wrap": 80,
        "Paneer Makhni Wrap": 95, "Paneer Tikka Wrap": 95,
        "Paneer Chilli Lava Wrap": 95,
    },
}

# Flat menu for Claude to search through
FLAT_MENU = {}
for category, items in MENU.items():
    FLAT_MENU.update(items)

# ── Simplified menu for WhatsApp display (popular items only) ─────────────────
SIMPLE_MENU = {
    "1": {
        "title": "🥤 Beverages",
        "sections": {
            "Mocktails": {"Mojito": 70, "Blue Lagoon": 75, "Passion Fruit": 75, "Raspberry Mojito": 80},
            "Thick Shakes": {"Chocolate": 100, "Nutella": 110, "Kitkat": 100, "Black Forest": 110},
            "Milk Shakes": {"Vanilla": 79, "Mango": 79, "Strawberry": 79, "Butter Scotch": 79},
            "Coffee": {"Cold Coffee": 80, "Caramel Cold Coffee": 90, "Hot Coffee": 40, "Black Coffee": 25},
            "Tea & Iced Tea": {"Masala Tea": 30, "Green Tea": 30, "Peach Iced Tea": 75, "Lemon Iced Tea": 75},
        }
    },
    "2": {
        "title": "🧇 Desserts",
        "sections": {
            "Waffles (Single / Double)": {
                "Classic": "90 / 170", "Chocolate": "100 / 190",
                "Nutella": "110 / 200", "Strawberry": "100 / 190",
                "BTT Special": "130 / 240", "Waffle Platter (Any 4)": 250,
            },
            "Brownies": {
                "Hot Brownie with Icecream": 100,
                "Hot Brownie Fudge": 110,
                "Hot Chocolate Brownie": 80,
            },
        }
    },
    "3": {
        "title": "🍜 Chinese",
        "sections": {
            "Momos (Half / Full)": {
                "Veg Steam": "40 / 70", "Paneer Steam": "50 / 80",
                "Veg Fried": "45 / 80", "Paneer Fried": "60 / 110",
                "Veg Kurkure": "60 / 90", "Afghani Paneer": "70 / 130",
            },
            "Noodles (Half / Full)": {
                "Veg Noodles": "60 / 90", "Hakka Noodles": "80 / 120",
                "Chilli Garlic": "70 / 100", "Paneer Noodles": "80 / 120",
            },
            "Snacks": {
                "Honey Chilli Potato": 80, "Chilli Paneer": 100,
                "Veg Manchurian": 70, "Kurkure Spring Roll": 100,
            },
        }
    },
    "4": {
        "title": "🍚 Rice & South Indian",
        "sections": {
            "Rice (Half / Full)": {
                "Veg Fried Rice": "60 / 100", "Schezwan Fried Rice": "70 / 110",
                "Paneer Fried Rice": "80 / 120", "Veg Singapore Rice": "80 / 120",
            },
            "Dosa": {
                "Masala Dosa": 100, "Paneer Dosa": 140,
                "Schezwan Dosa": 110, "Family Dosa": 160,
            },
            "South Indian Snacks": {
                "Sambhar Idli": 60, "Fried Idli": 70,
                "Chilli Idli": 90, "Dahi Vada": 80,
            },
        }
    },
    "5": {
        "title": "🍔 Continental",
        "sections": {
            "Burgers": {
                "Aloo Tikki Burger": 50, "Veg Chilli Lava Burger": 75,
                "Crispy Paneer Burger": 95, "Paneer Maharaja Burger": 130,
            },
            "Pasta": {
                "Red Sauce Pasta": 100, "White Sauce Pasta": 110,
                "Cheese Sauce Pasta": 130, "Mac N Cheese": 150,
            },
            "Fries & Wraps": {
                "Classic Fries": 70, "Peri-Peri Fries": 90,
                "Cheese Loaded Fries": 90, "Paneer Tikka Wrap": 95,
            },
        }
    },
}


def format_category_menu(cat_num: str) -> str:
    if cat_num not in SIMPLE_MENU:
        return ""
    cat = SIMPLE_MENU[cat_num]
    lines = [f"*{cat['title']}*\n"]
    for section_name, items in cat["sections"].items():
        lines.append(f"📌 *{section_name}*")
        for item, price in items.items():
            price_str = f"₹{price}" if isinstance(price, int) else f"₹{price}"
            lines.append(f"  • {item} — {price_str}")
        lines.append("")
    lines.append("_To order, just type what you want! E.g: '2 nutella waffles and 1 cold coffee'_")
    lines.append("_Type *back* to see all categories_")
    return "\n".join(lines)


# ── WhatsApp helpers ───────────────────────────────────────────────────────────

async def send_text(to: str, message: str):
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        print(f"[BTT/WA] Not configured — would send to {to}:\n{message[:100]}")
        return
    to = to.lstrip("+").replace(" ", "")
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            f"{WA_API_BASE}/messages",
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"},
            json={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": message}},
        )
    print(f"[BTT/WA] → {to}: {resp.status_code}")


# ── Welcome & navigation ───────────────────────────────────────────────────────

WELCOME_MSG = (
    f"👋 Welcome to *{RESTAURANT_NAME}*! 🍽️\n\n"
    f"I'm your virtual assistant. How can I help you today?\n\n"
    f"1️⃣  View Menu\n"
    f"2️⃣  Place an Order\n"
    f"3️⃣  Book a Table\n"
    f"4️⃣  Location & Hours\n\n"
    f"_Just reply with a number or type your question!_"
)

MENU_CATEGORY_MSG = (
    "📋 *BTT Menu — Choose a Category:*\n\n"
    "1️⃣  🥤 Beverages\n"
    "2️⃣  🧇 Desserts\n"
    "3️⃣  🍜 Chinese\n"
    "4️⃣  🍚 Rice & South Indian\n"
    "5️⃣  🍔 Continental\n\n"
    "_Reply with a number to see that section's menu_"
)




# ── Order parsing with Claude ──────────────────────────────────────────────────

async def parse_order_with_claude(text: str) -> list[dict]:
    """
    Use Claude to parse customer's free-text order into structured items.
    Returns list of {name, qty, unit_price, subtotal}
    """
    if not ANTHROPIC_API_KEY:
        return []

    menu_json = json.dumps(FLAT_MENU, ensure_ascii=False)
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = (
        f"You are a restaurant order parser for BTT - Bite Tongue Tingling.\n\n"
        f"Customer said: \"{text}\"\n\n"
        f"Available menu items and prices (in ₹):\n{menu_json}\n\n"
        f"Parse the customer's order and match each item to the closest menu item.\n"
        f"Return a JSON array only, no explanation. Format:\n"
        f'[{{"name": "exact menu item name", "qty": 1, "unit_price": 100, "subtotal": 100}}]\n\n'
        f"If nothing matches the menu, return an empty array [].\n"
        f"Match partial names intelligently (e.g. 'nutella waffle' → 'Nutella Waffle', "
        f"'cold coffee' → 'Cold Coffee', 'veg momos' → 'Veg Steam Momos (Half)')."
    )

    try:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        # Extract JSON array
        start = raw.find("[")
        end = raw.rfind("]") + 1
        if start == -1 or end == 0:
            return []
        parsed = json.loads(raw[start:end])
        return parsed if isinstance(parsed, list) else []
    except Exception as e:
        print(f"[BTT] Order parse error: {e}")
        return []


def format_cart(cart: list) -> str:
    if not cart:
        return "_(empty)_"
    lines = []
    total = 0
    for item in cart:
        lines.append(f"  • {item['name']} × {item['qty']} = ₹{item['subtotal']}")
        total += item['subtotal']
    lines.append(f"\n*Total: ₹{total}*")
    return "\n".join(lines)


def cart_total(cart: list) -> int:
    return sum(item['subtotal'] for item in cart)


# ── Owner notifications ────────────────────────────────────────────────────────

async def notify_owner_order(from_phone: str, cart: list, customer_name: str = ""):
    total = cart_total(cart)
    name_part = f"👤 *Customer:* {customer_name}\n" if customer_name else ""
    items_text = "\n".join([f"  • {i['name']} × {i['qty']} = ₹{i['subtotal']}" for i in cart])
    msg = (
        f"🛎️ *New Order — {RESTAURANT_NAME}*\n\n"
        f"{name_part}"
        f"📱 *Phone:* {from_phone}\n"
        f"🕐 *Time:* {datetime.now(IST).strftime('%d %b %Y, %I:%M %p IST')}\n\n"
        f"📋 *Order:*\n{items_text}\n\n"
        f"💰 *Total: ₹{total}*"
    )
    await send_text(OWNER_WHATSAPP, msg)


async def notify_owner_booking(from_phone: str, booking: dict):
    msg = (
        f"🪑 *Table Booking — {RESTAURANT_NAME}*\n\n"
        f"👤 *Name:* {booking.get('name', '—')}\n"
        f"📱 *Phone:* {booking.get('contact', from_phone)}\n"
        f"📅 *Date:* {booking.get('date', '—')}\n"
        f"🕐 *Time:* {booking.get('time', '—')}\n"
        f"👥 *People:* {booking.get('people', '—')}\n\n"
        f"_Please confirm the booking with the customer._"
    )
    await send_text(OWNER_WHATSAPP, msg)


# ── Main message handler ───────────────────────────────────────────────────────

async def handle_restaurant_message(body: dict):
    """
    Main entry point — called from POST /webhook in main.py when RESTAURANT_MODE=true.
    """
    try:
        entry    = body.get("entry", [{}])[0]
        changes  = entry.get("changes", [{}])[0]
        value    = changes.get("value", {})
        messages = value.get("messages", [])

        if not messages:
            return  # status update, not a message

        msg        = messages[0]
        from_phone = msg.get("from", "")
        msg_type   = msg.get("type", "")

        if msg_type == "text":
            text = msg["text"]["body"].strip()
        elif msg_type == "image":
            await send_text(from_phone, "📸 Thanks for the photo! For orders and bookings, please type your request. 😊")
            return
        else:
            return

        print(f"[BTT] Message from {from_phone}: {text[:80]}")

        session = sessions.get(from_phone, {"state": None, "cart": [], "booking": {}})
        state   = session.get("state")

        # ── Reset keywords ──────────────────────────────────────────────
        if text.lower() in ("hi", "hello", "hey", "start", "menu", "help", "hlo", "hii"):
            sessions[from_phone] = {"state": "main_menu", "cart": [], "booking": {}}
            await send_text(from_phone, WELCOME_MSG)
            return

        # ── Main menu ───────────────────────────────────────────────────
        if state in (None, "main_menu"):
            if text == "1" or any(w in text.lower() for w in ("view menu", "see menu", "show menu", "menu")):
                sessions[from_phone] = {**session, "state": "menu_browse"}
                await send_text(from_phone, MENU_CATEGORY_MSG)

            elif text == "2" or any(w in text.lower() for w in ("order", "want to order", "place order")):
                sessions[from_phone] = {**session, "state": "ordering", "cart": []}
                await send_text(from_phone,
                    "🛒 *Place Your Order*\n\n"
                    "Just tell me what you'd like!\n\n"
                    "Example:\n"
                    "_'2 nutella waffles, 1 cold coffee and veg momos half'_\n\n"
                    "Type *menu* anytime to browse, or *done* when finished."
                )

            elif text == "3" or any(w in text.lower() for w in ("book", "table", "reservation", "reserve")):
                sessions[from_phone] = {**session, "state": "booking_name", "booking": {}}
                await send_text(from_phone,
                    "🪑 *Table Booking*\n\n"
                    "Let's get your table reserved! First, what's your *name*?"
                )

            elif text == "4" or any(w in text.lower() for w in ("location", "address", "where", "hours", "timing")):
                await send_text(from_phone,
                    f"📍 *{RESTAURANT_NAME}*\n\n"
                    f"📌 *Address:*\n{RESTAURANT_ADDRESS}\n\n"
                    f"🕐 *Hours:* {RESTAURANT_HOURS}\n\n"
                    f"📞 *Call us:* {RESTAURANT_PHONE}\n"
                    f"🛵 *Home Delivery* available on Zomato!\n\n"
                    f"_Reply *hi* to go back to main menu_"
                )
            else:
                # Unknown input — show welcome again
                await send_text(from_phone, WELCOME_MSG)

        # ── Menu browsing ───────────────────────────────────────────────
        elif state == "menu_browse":
            if text in ("1", "2", "3", "4", "5"):
                menu_text = format_category_menu(text)
                await send_text(from_phone, menu_text)
            elif text.lower() in ("back", "categories", "all"):
                await send_text(from_phone, MENU_CATEGORY_MSG)
            elif text.lower() == "order" or any(w in text.lower() for w in ("want to order", "place order")):
                sessions[from_phone] = {**session, "state": "ordering", "cart": []}
                await send_text(from_phone,
                    "🛒 Great! What would you like to order?\n\n"
                    "_Type your items e.g: '1 chocolate waffle and 2 cold coffees'_\n\n"
                    "Type *done* when your order is complete."
                )
            else:
                await send_text(from_phone,
                    "Please reply with a number (1-5) to see that menu section.\n"
                    "Or type *order* to place an order, *hi* for main menu."
                )

        # ── Ordering ────────────────────────────────────────────────────
        elif state == "ordering":
            if text.lower() in ("done", "that's all", "thats all", "confirm", "submit"):
                cart = session.get("cart", [])
                if not cart:
                    await send_text(from_phone,
                        "Your cart is empty! Tell me what you'd like to order.\n"
                        "E.g: _'1 nutella waffle and cold coffee'_"
                    )
                else:
                    sessions[from_phone] = {**session, "state": "order_confirm"}
                    await send_text(from_phone,
                        f"🧾 *Your Order Summary:*\n\n"
                        f"{format_cart(cart)}\n\n"
                        f"Reply *yes* to confirm or *edit* to change your order."
                    )

            elif text.lower() in ("clear", "restart", "start over"):
                sessions[from_phone] = {**session, "cart": []}
                await send_text(from_phone, "🗑️ Cart cleared! What would you like to order?")

            else:
                # Parse order with Claude
                await send_text(from_phone, "⏳ Adding items to your cart...")
                items = await parse_order_with_claude(text)

                if not items:
                    await send_text(from_phone,
                        "Sorry, I couldn't find those items on our menu. 😅\n\n"
                        "Type *menu* to browse, or try again with the exact item name.\n"
                        "E.g: _'Nutella Waffle', 'Cold Coffee', 'Veg Momos Half'_"
                    )
                else:
                    cart = session.get("cart", [])
                    cart.extend(items)
                    sessions[from_phone] = {**session, "cart": cart}
                    added = "\n".join([f"  ✅ {i['name']} × {i['qty']} = ₹{i['subtotal']}" for i in items])
                    await send_text(from_phone,
                        f"Added to cart:\n{added}\n\n"
                        f"*Cart Total so far: ₹{cart_total(cart)}*\n\n"
                        f"Add more items, or type *done* to confirm your order."
                    )

        # ── Order confirmation ──────────────────────────────────────────
        elif state == "order_confirm":
            if text.lower() in ("yes", "confirm", "ok", "okay", "place order", "proceed"):
                cart = session.get("cart", [])
                total = cart_total(cart)

                # Notify owner
                asyncio.create_task(notify_owner_order(from_phone, cart))

                # Confirm to customer
                await send_text(from_phone,
                    f"✅ *Order Placed Successfully!*\n\n"
                    f"{format_cart(cart)}\n\n"
                    f"📍 *Pick up / Dine in at:*\n{RESTAURANT_ADDRESS}\n\n"
                    f"📞 *For queries:* {RESTAURANT_PHONE}\n\n"
                    f"Thank you for ordering from *{RESTAURANT_NAME}*! 🙏\n"
                    f"_Type *hi* to start a new order_"
                )
                sessions.pop(from_phone, None)

            elif text.lower() in ("edit", "change", "modify", "no"):
                sessions[from_phone] = {**session, "state": "ordering"}
                await send_text(from_phone,
                    f"No problem! Your current cart:\n{format_cart(session.get('cart', []))}\n\n"
                    "Type *clear* to start fresh, or add/change items."
                )
            else:
                cart = session.get("cart", [])
                await send_text(from_phone,
                    f"Please reply *yes* to confirm or *edit* to change.\n\n"
                    f"{format_cart(cart)}"
                )

        # ── Table booking flow ──────────────────────────────────────────
        elif state == "booking_name":
            sessions[from_phone] = {**session, "state": "booking_date", "booking": {"name": text}}
            await send_text(from_phone,
                f"Great, *{text}*! 😊\n\n"
                f"What *date* would you like to book?\n"
                f"_(e.g. Tomorrow, 30 May, Saturday)_"
            )

        elif state == "booking_date":
            booking = {**session.get("booking", {}), "date": text}
            sessions[from_phone] = {**session, "state": "booking_time", "booking": booking}
            await send_text(from_phone,
                f"Perfect! What *time* would you prefer?\n"
                f"_(e.g. 7:30 PM, 8 PM)_\n\n"
                f"⏰ We're open: {RESTAURANT_HOURS}"
            )

        elif state == "booking_time":
            booking = {**session.get("booking", {}), "time": text}
            sessions[from_phone] = {**session, "state": "booking_people", "booking": booking}
            await send_text(from_phone, "How many people will be joining? 👥")

        elif state == "booking_people":
            booking = {**session.get("booking", {}), "people": text}
            sessions[from_phone] = {**session, "state": "booking_phone", "booking": booking}
            await send_text(from_phone,
                "What's your *contact number* for confirmation?\n"
                "_(We'll call to confirm your booking)_"
            )

        elif state == "booking_phone":
            booking = {**session.get("booking", {}), "contact": text}
            sessions[from_phone] = {**session, "state": "booking_confirm", "booking": booking}
            await send_text(from_phone,
                f"📋 *Booking Summary:*\n\n"
                f"👤 *Name:* {booking.get('name')}\n"
                f"📅 *Date:* {booking.get('date')}\n"
                f"🕐 *Time:* {booking.get('time')}\n"
                f"👥 *People:* {booking.get('people')}\n"
                f"📞 *Contact:* {text}\n\n"
                f"Reply *yes* to confirm or *edit* to change."
            )

        elif state == "booking_confirm":
            if text.lower() in ("yes", "confirm", "ok", "okay"):
                booking = session.get("booking", {})
                asyncio.create_task(notify_owner_booking(from_phone, booking))
                await send_text(from_phone,
                    f"✅ *Table Booking Request Sent!*\n\n"
                    f"👤 {booking.get('name')}\n"
                    f"📅 {booking.get('date')} at {booking.get('time')}\n"
                    f"👥 {booking.get('people')} people\n\n"
                    f"Our team will call you at *{booking.get('contact')}* to confirm.\n\n"
                    f"📍 *{RESTAURANT_NAME}*\n{RESTAURANT_ADDRESS}\n\n"
                    f"_Type *hi* to go back to main menu_"
                )
                sessions.pop(from_phone, None)

            elif text.lower() in ("edit", "change", "no"):
                sessions[from_phone] = {**session, "state": "booking_name", "booking": {}}
                await send_text(from_phone, "Let's start over. What's your *name*?")
            else:
                await send_text(from_phone, "Please reply *yes* to confirm or *edit* to change.")

    except Exception as e:
        print(f"[BTT] Error: {e}")
        import traceback
        traceback.print_exc()
        await send_text(from_phone,
            f"Sorry, something went wrong! 😅\n"
            f"Please call us directly: 📞 {RESTAURANT_PHONE}\n"
            f"Or type *hi* to restart."
        )
