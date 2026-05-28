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
import re
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
RESTAURANT_PHONE   = "9953910987"
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
            "Mocktails": {
                "Mojito": 70, "Blue Lagoon": 75,
                "Passion Fruit": 75, "Raspberry Mojito": 80,
            },
            "Shakes & Coffee": {
                "Chocolate Thick Shake": 100, "Nutella Thick Shake": 110,
                "Cold Coffee": 80, "Caramel Cold Coffee": 90,
            },
            "Tea & Beer": {
                "Masala Tea": 30, "Peach Iced Tea": 75,
                "Raspberry Beer": 85, "Watermelon Beer": 65,
            },
        }
    },
    "2": {
        "title": "🧇 Desserts",
        "sections": {
            "Waffles": {
                "Classic Waffle": 90, "Chocolate Waffle": 100,
                "Nutella Waffle": 110, "BTT Special Waffle": 130,
                "Waffle Platter (Any 4)": 250,
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
            "Momos": {
                "Veg Steam Momos": 70, "Paneer Steam Momos": 80,
                "Veg Fried Momos": 80, "Paneer Fried Momos": 110,
                "Chilli Momos": 120,
            },
            "Noodles": {
                "Veg Noodles": 90, "Hakka Noodles": 120,
                "Chilli Garlic Noodles": 100, "Paneer Noodles": 120,
            },
            "Chinese Snacks": {
                "Veg Manchurian Gravy": 140, "Chilli Paneer": 190,
                "Honey Chilli Potato": 120, "Kurkure Spring Roll": 100,
            },
        }
    },
    "4": {
        "title": "🍚 Rice & South Indian",
        "sections": {
            "Rice": {
                "Veg Fried Rice": 100, "Schezwan Fried Rice": 110,
                "Paneer Fried Rice": 120, "Lemon Rice": 90,
            },
            "Dosa": {
                "Masala Dosa": 100, "Mysore Masala Dosa": 110,
                "Schezwan Dosa": 110, "Paneer Dosa": 140,
            },
            "South Indian Snacks": {
                "Sambhar Idli": 60, "Sambhar Vada": 70,
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
            "Pasta & Fries": {
                "Red Sauce Pasta": 100, "White Sauce Pasta": 110,
                "Cheese Loaded Fries": 90, "Peri-Peri Fries": 90,
            },
            "Wraps & Sandwiches": {
                "Paneer Tikka Wrap": 95, "Crispy Paneer Wrap": 110,
                "Paneer Tikka Sandwich": 85, "Veggie Sandwich": 70,
            },
        }
    },
}


def _dual_suffix(section_name: str) -> tuple[str, str]:
    """Extract (s1, s2) from section titles like 'Momos (Half / Full)'."""
    m = re.search(r'\(([^/]+)/\s*([^)]+)\)', section_name)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "Small", "Large"


def get_numbered_menu(cat_num: str) -> tuple[str, dict]:
    """
    Returns (menu_text, items_dict).
    items_dict maps str(number) → {"name": ..., "price": int}.
    Dual-price items (Half/Full, Single/Double, 2pcs/4pcs) become TWO numbered entries.
    """
    if cat_num not in SIMPLE_MENU:
        return "", {}

    cat = SIMPLE_MENU[cat_num]
    lines = [f"*{cat['title']}*\n"]
    items_dict: dict = {}
    counter = 1

    for section_name, items in cat["sections"].items():
        # Strip "(Half / Full)", "(Single / Double)", "(2pcs / 4pcs)" etc. from header
        clean_header = re.sub(r'\s*\([^)]*\/[^)]*\)', '', section_name).strip()
        lines.append(f"📌 *{clean_header}*")
        s1, s2 = _dual_suffix(section_name)
        for item_name, price in items.items():
            if isinstance(price, str) and " / " in price:
                # Only show the full/large size (second price), no size label
                p2 = int(price.split("/")[1].strip())
                items_dict[str(counter)] = {"name": item_name, "price": p2}
                lines.append(f"  *{counter}.* {item_name} — ₹{p2}")
                counter += 1
            else:
                items_dict[str(counter)] = {"name": item_name, "price": int(price)}
                lines.append(f"  *{counter}.* {item_name} — ₹{price}")
                counter += 1
        lines.append("")

    lines.append("💡 *Order by number:* type `1 3` or `2x2 5` (2x = qty 2)")
    lines.append("Type *back* for categories | *done* to checkout")
    return "\n".join(lines), items_dict


def format_category_menu(cat_num: str) -> str:
    """Backward-compat wrapper — returns only the text."""
    text, _ = get_numbered_menu(cat_num)
    return text


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
    "_Reply with a number (1–5) to see that section_\n"
    "_Then order by item number — type *done* to checkout_"
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
        if text.lower() in ("hi", "hello", "hey", "start", "help", "hlo", "hii"):
            sessions[from_phone] = {"state": "main_menu", "cart": [], "booking": {}}
            await send_text(from_phone, WELCOME_MSG)
            return

        _BACK = "\n\n_Type *hi* to go back to main menu_"

        # ── Main menu ───────────────────────────────────────────────────
        if state in (None, "main_menu"):
            if text == "1" or any(w in text.lower() for w in ("view menu", "see menu", "show menu", "menu")):
                sessions[from_phone] = {**session, "state": "menu_browse"}
                await send_text(from_phone, MENU_CATEGORY_MSG)

            elif text == "2" or any(w in text.lower() for w in ("order", "want to order", "place order")):
                sessions[from_phone] = {**session, "state": "menu_browse", "cart": [],
                                         "in_category": False, "menu_items": {}}
                await send_text(from_phone,
                    "🛒 *Place Your Order*\n\n"
                    "Browse a category, then tap item numbers to add to cart!\n\n"
                    + MENU_CATEGORY_MSG
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
                    f"🛵 *Home Delivery* available on Zomato!" + _BACK
                )
            else:
                # Unknown input — show welcome again
                await send_text(from_phone, WELCOME_MSG)

        # ── Menu browsing (number-based ordering) ───────────────────────
        elif state == "menu_browse":
            in_cat     = session.get("in_category", False)
            menu_items = session.get("menu_items", {})
            cart       = session.get("cart", [])
            tl         = text.lower()

            # Always-available commands
            if tl in ("back", "categories", "all", "0", "menu"):
                sessions[from_phone] = {**session, "in_category": False, "menu_items": {}}
                await send_text(from_phone, MENU_CATEGORY_MSG)

            elif tl in ("done", "checkout", "confirm order", "order done"):
                if not cart:
                    await send_text(from_phone,
                        "🛒 Your cart is empty!\n\n"
                        "Browse a category (1–5) and tap item numbers to add them." + _BACK
                    )
                else:
                    sessions[from_phone] = {**session, "state": "order_name"}
                    await send_text(from_phone,
                        "Almost done! 😊\n\n"
                        "What's your *name* for the order?" + _BACK
                    )

            elif text in ("1", "2", "3", "4", "5") and not in_cat:
                # Category selection
                menu_text, items_dict = get_numbered_menu(text)
                sessions[from_phone] = {**session, "in_category": True, "menu_items": items_dict}
                cart_hint = (f"\n\n🛒 *Cart so far: ₹{cart_total(cart)}* — type *done* to checkout"
                             if cart else "")
                await send_text(from_phone, menu_text + cart_hint)

            elif in_cat:
                # ── Try to parse as item numbers like "1 3 5" or "2x2 5" ──
                _NUM = re.compile(r'^(\d+)(?:[xX](\d+))?$')
                tokens = text.strip().split()
                parsed = []
                all_nums = bool(tokens)
                for tok in tokens:
                    m = _NUM.match(tok)
                    if m:
                        parsed.append((m.group(1), int(m.group(2)) if m.group(2) else 1))
                    else:
                        all_nums = False
                        break

                if all_nums and parsed:
                    added, not_found = [], []
                    for item_num, qty in parsed:
                        if item_num in menu_items:
                            it = menu_items[item_num]
                            cart.append({
                                "name": it["name"], "qty": qty,
                                "unit_price": it["price"],
                                "subtotal": it["price"] * qty,
                            })
                            added.append(f"  ✅ {it['name']} × {qty} = ₹{it['price'] * qty}")
                        else:
                            not_found.append(item_num)

                    sessions[from_phone] = {**session, "cart": cart}
                    parts = []
                    if added:
                        parts.append("Added to cart:\n" + "\n".join(added))
                        parts.append(f"\n🛒 *Cart Total: ₹{cart_total(cart)}*")
                    if not_found:
                        parts.append(f"\n⚠️ Item(s) {', '.join(not_found)} not found — check the numbers above.")
                    parts.append("\nAdd more items or type *done* to checkout | *back* for categories")
                    await send_text(from_phone, "\n".join(parts))

                elif text in ("1", "2", "3", "4", "5"):
                    # Switch category while already viewing one
                    menu_text, items_dict = get_numbered_menu(text)
                    sessions[from_phone] = {**session, "in_category": True, "menu_items": items_dict}
                    cart_hint = (f"\n\n🛒 *Cart so far: ₹{cart_total(cart)}* — type *done* to checkout"
                                 if cart else "")
                    await send_text(from_phone, menu_text + cart_hint)

                else:
                    await send_text(from_phone,
                        "Type item numbers to add (e.g. *1 3* or *2x2 5*)\n"
                        "*back* — categories | *done* — checkout | *hi* — main menu"
                    )

            else:
                await send_text(from_phone,
                    "Reply with a number (1–5) to browse a category,\n"
                    "or type *done* to checkout, *hi* for main menu."
                )

        # ── Order name collection ───────────────────────────────────────
        elif state == "order_name":
            sessions[from_phone] = {**session, "state": "order_confirm", "order_name": text}
            cart = session.get("cart", [])
            await send_text(from_phone,
                f"🧾 *Order Summary for {text}:*\n\n"
                f"{format_cart(cart)}\n\n"
                "Reply *yes* to confirm or *edit* to add more items." + _BACK
            )

        # ── Ordering (text fallback) ─────────────────────────────────────
        elif state == "ordering":
            # Redirect to menu_browse — number-based is the primary flow now
            sessions[from_phone] = {**session, "state": "menu_browse",
                                     "in_category": False, "menu_items": {}}
            await send_text(from_phone,
                "🛒 *Browse & Order by Number*\n\n" + MENU_CATEGORY_MSG
            )

        # ── Order confirmation ──────────────────────────────────────────
        elif state == "order_confirm":
            if text.lower() in ("yes", "confirm", "ok", "okay", "place order", "proceed"):
                cart = session.get("cart", [])
                name = session.get("order_name", "")

                # Notify owner
                asyncio.create_task(notify_owner_order(from_phone, cart, customer_name=name))

                # Confirm to customer
                greeting = f"Hi *{name}*, your" if name else "Your"
                await send_text(from_phone,
                    f"✅ *Order Placed Successfully!*\n\n"
                    f"👤 *Name:* {name}\n\n"
                    f"{format_cart(cart)}\n\n"
                    f"📍 *Pick up / Dine in at:*\n{RESTAURANT_ADDRESS}\n\n"
                    f"📞 *For queries:* {RESTAURANT_PHONE}\n\n"
                    f"Thank you for ordering from *{RESTAURANT_NAME}*! 🙏\n"
                    f"_Type *hi* to start a new order_"
                )
                sessions.pop(from_phone, None)

            elif text.lower() in ("edit", "change", "modify", "no"):
                sessions[from_phone] = {**session, "state": "menu_browse",
                                         "in_category": False, "menu_items": {}}
                await send_text(from_phone,
                    f"No problem! Current cart:\n{format_cart(session.get('cart', []))}\n\n"
                    "Browse more items below 👇\n\n" + MENU_CATEGORY_MSG
                )
            else:
                cart = session.get("cart", [])
                await send_text(from_phone,
                    f"Please reply *yes* to confirm or *edit* to add more.\n\n"
                    f"{format_cart(cart)}" + _BACK
                )

        # ── Table booking flow ──────────────────────────────────────────
        elif state == "booking_name":
            sessions[from_phone] = {**session, "state": "booking_date", "booking": {"name": text}}
            await send_text(from_phone,
                f"Great, *{text}*! 😊\n\n"
                f"What *date* would you like to book?\n"
                f"_(e.g. Tomorrow, 30 May, Saturday)_" + _BACK
            )

        elif state == "booking_date":
            booking = {**session.get("booking", {}), "date": text}
            sessions[from_phone] = {**session, "state": "booking_time", "booking": booking}
            await send_text(from_phone,
                f"Perfect! What *time* would you prefer?\n"
                f"_(e.g. 7:30 PM, 8 PM)_\n\n"
                f"⏰ We're open: {RESTAURANT_HOURS}" + _BACK
            )

        elif state == "booking_time":
            booking = {**session.get("booking", {}), "time": text}
            sessions[from_phone] = {**session, "state": "booking_people", "booking": booking}
            await send_text(from_phone, "How many people will be joining? 👥" + _BACK)

        elif state == "booking_people":
            booking = {**session.get("booking", {}), "people": text}
            sessions[from_phone] = {**session, "state": "booking_phone", "booking": booking}
            await send_text(from_phone,
                "What's your *contact number* for confirmation?\n"
                "_(We'll call to confirm your booking)_" + _BACK
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
                f"Reply *yes* to confirm or *edit* to change." + _BACK
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
                await send_text(from_phone, "Let's start over. What's your *name*?" + _BACK)
            else:
                await send_text(from_phone, "Please reply *yes* to confirm or *edit* to change." + _BACK)

    except Exception as e:
        print(f"[BTT] Error: {e}")
        import traceback
        traceback.print_exc()
        await send_text(from_phone,
            f"Sorry, something went wrong! 😅\n"
            f"Please call us directly: 📞 {RESTAURANT_PHONE}\n"
            f"Or type *hi* to restart."
        )
