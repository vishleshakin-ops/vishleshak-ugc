"""
Generate a WhatsApp-shareable rate card image for Vishleshak AI Video Ads
"""
from PIL import Image, ImageDraw, ImageFont
import os

W, H = 1080, 1600
img = Image.new("RGB", (W, H), "#0a0a1a")
draw = ImageDraw.Draw(img)

# ── Fonts ──────────────────────────────────────────────────────────────────
def get_font(size, bold=False):
    candidates = [
        f"C:/Windows/Fonts/{'arialbd' if bold else 'arial'}.ttf",
        f"C:/Windows/Fonts/{'calibrib' if bold else 'calibri'}.ttf",
        f"C:/Windows/Fonts/{'seguisb' if bold else 'segoeui'}.ttf",
    ]
    for fp in candidates:
        if os.path.exists(fp):
            return ImageFont.truetype(fp, size)
    return ImageFont.load_default()

# Segoe UI Emoji for emoji support
def get_emoji_font(size):
    for fp in ["C:/Windows/Fonts/seguiemj.ttf", "C:/Windows/Fonts/segoeui.ttf"]:
        if os.path.exists(fp):
            return ImageFont.truetype(fp, size)
    return get_font(size)

# ── Helpers ────────────────────────────────────────────────────────────────
def cx(text, font, y, color="white"):
    bbox = draw.textbbox((0, 0), text, font=font)
    x = (W - (bbox[2] - bbox[0])) // 2
    draw.text((x, y), text, font=font, fill=color)

def rr(xy, r=24, fill=None, outline=None, lw=2):
    draw.rounded_rectangle(xy, radius=r, fill=fill, outline=outline, width=lw)

# ── Background decoration ─────────────────────────────────────────────────
draw.ellipse([-150, -150, 300, 300], fill="#111130")
draw.ellipse([820, -100, 1200, 280], fill="#111130")
draw.rectangle([0, 0, W, 10], fill="#f5c518")          # top bar

# ── LOGO / HEADER ─────────────────────────────────────────────────────────
# Camera icon block
rr([460, 48, 620, 145], r=20, fill="#1e1e4a")
draw.text((484, 58), "VIDEO", font=get_font(32, bold=True), fill="#f5c518")
draw.text((476, 96), "STUDIO", font=get_font(28, bold=True), fill="#8888cc")

cx("VISHLESHAK", get_font(82, bold=True), 168, "#f5c518")
cx("AI VIDEO ADS", get_font(54, bold=True), 262, "white")

# Tag line pill
rr([200, 332, W-200, 386], r=27, fill="#f5c518")
cx("Photo bhejiye  ->  Video paaiye", get_font(30, bold=True), 344, "#0a0a1a")

# ── DIVIDER ───────────────────────────────────────────────────────────────
draw.line([80, 416, W-80, 416], fill="#2a2a5a", width=2)

# ── HOW IT WORKS ──────────────────────────────────────────────────────────
cx("HOW IT WORKS", get_font(32, bold=True), 436, "#8888cc")

steps = [
    ("01", "Product ki photo WhatsApp pe bhejiye"),
    ("02", "AI 10 minute mein video banata hai"),
    ("03", "Ready-to-post video milti hai aapko"),
]
sy = 488
for num, text in steps:
    rr([80, sy, W-80, sy+68], r=16, fill="#13133a")
    # Number badge
    rr([100, sy+14, 148, sy+54], r=10, fill="#f5c518")
    draw.text((113, sy+20), num, font=get_font(24, bold=True), fill="#0a0a1a")
    draw.text((168, sy+18), text, font=get_font(30), fill="#e0e0ff")
    sy += 82

# ── DIVIDER ───────────────────────────────────────────────────────────────
draw.line([80, sy+10, W-80, sy+10], fill="#2a2a5a", width=2)

# ── PRICING ───────────────────────────────────────────────────────────────
cy = sy + 38
cx("PRICING", get_font(32, bold=True), cy, "#8888cc")
cy += 54

plans = [
    ("1 Video",            "Rs. 499",   "",            "#1e1e4a", "white",   "#f5c518", False),
    ("5 Videos / Month",   "Rs. 1,999", "SAVE Rs.500", "#1e1e4a", "white",   "white",   False),
    ("10 Videos / Month",  "Rs. 3,499", "BEST VALUE",  "#f5c518", "#0a0a1a", "#0a0a1a", True),
]

for label, price, badge, bg, text_col, price_col, highlight in plans:
    bh = 118 if highlight else 102
    rr([80, cy, W-80, cy+bh], r=20, fill=bg,
       outline="#f5c518" if highlight else "#2a2a5a", lw=3 if highlight else 1)

    draw.text((120, cy+18), label, font=get_font(34, bold=highlight), fill=text_col)

    # Price right-aligned
    pb = draw.textbbox((0,0), price, font=get_font(46, bold=True))
    px = W - 120 - (pb[2]-pb[0])
    draw.text((px, cy+14), price, font=get_font(46, bold=True), fill=price_col)

    # Badge
    if badge:
        bb = draw.textbbox((0,0), badge, font=get_font(22, bold=True))
        bw = bb[2]-bb[0]+24
        bx = W - 120 - bw
        by2 = cy + bh - 36
        badge_bg = "#0a0a1a" if highlight else "#f5c518"
        badge_fg = "#f5c518" if highlight else "#0a0a1a"
        rr([bx-2, by2-2, bx+bw+2, by2+28], r=8, fill=badge_bg)
        draw.text((bx+8, by2+2), badge, font=get_font(22, bold=True), fill=badge_fg)

    cy += bh + 16

# ── FEATURES ──────────────────────────────────────────────────────────────
cy += 8
feats = [
    ("Hindi & English voiceover included",),
    ("Instagram, Facebook, Reels ready",),
    ("9:16 portrait + 16:9 landscape",),
    ("AI presenter + your product",),
]
for (feat,) in feats:
    draw.text((108, cy), "->", font=get_font(28, bold=True), fill="#f5c518")
    draw.text((152, cy), feat, font=get_font(28), fill="#c0c0e0")
    cy += 44

# ── CONTACT FOOTER ────────────────────────────────────────────────────────
cy += 16
draw.rectangle([0, cy, W, H], fill="#f5c518")

cx("+91 99539 10987", get_font(46, bold=True), cy+26, "#0a0a1a")
cx("vishleshak.in", get_font(36), cy+86, "#1a1a00")

# Divider inside footer
draw.line([120, cy+136, W-120, cy+136], fill="#c8a000", width=1)

cx("WhatsApp pe photo bhejiye — PEHLA VIDEO FREE!", get_font(28, bold=True), cy+152, "#0a0a1a")

# Bottom dark strip
draw.rectangle([0, H-14, W, H], fill="#0a0a1a")

# ── SAVE ──────────────────────────────────────────────────────────────────
out = os.path.join(os.path.dirname(__file__), "static", "rate_card.png")
img.save(out, "PNG")
print(f"Saved: {out}  ({os.path.getsize(out)//1024} KB)")
