"""
Instagram posting agent with Telegram approval (Instagram Login API).

Modes (set via the workflow's "mode" input):
  photo      - supply your own image(s), Claude writes caption + optional overlay
  card_stat  - Claude generates a flat dark branded card (no image needed)
  card_atm   - Claude generates a branded card over one of your background photos

Carousel: comma-separate filenames in the image field for photo mode.

Token refresh:
  python ig_agent.py --refresh

Env vars (GitHub Secrets or .env):
  ANTHROPIC_API_KEY, IG_ACCESS_TOKEN, IG_USER_ID
  S3_BUCKET, S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY, S3_PUBLIC_BASE
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import os, sys, time, uuid, glob, random, math
import requests
from dotenv import load_dotenv

load_dotenv()

GRAPH_VERSION = "v23.0"
GRAPH_BASE    = f"https://graph.instagram.com/{GRAPH_VERSION}"
REFRESH_URL   = "https://graph.instagram.com/refresh_access_token"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
IG_ACCESS_TOKEN   = os.environ.get("IG_ACCESS_TOKEN")
IG_USER_ID        = os.environ.get("IG_USER_ID")
TG_TOKEN          = os.environ.get("TELEGRAM_BOT_TOKEN")
TG_CHAT           = os.environ.get("TELEGRAM_CHAT_ID")
TG_BASE           = f"https://api.telegram.org/bot{TG_TOKEN}" if TG_TOKEN else None

APPROVAL_TIMEOUT = 30 * 60

# Brand palette
BRAND_BG    = (10,  10,  10)
BRAND_CARD  = (17,  17,  17)
BRAND_GOLD  = (201, 168,  76)
BRAND_CREAM = (232, 228, 220)
BRAND_MUTED = (155, 151, 143)

FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")
BG_DIR   = os.path.join(os.path.dirname(__file__), "backgrounds")
LOGO_LIGHT = os.path.join(os.path.dirname(__file__), "xlh-logo-transparent.png")
LOGO_DARK  = os.path.join(os.path.dirname(__file__), "xlh-logo-dark.png")

BRAND_VOICE = (
    "Confident, refined, and concrete. Operators talking to operators. "
    "Dark luxury brand voice. No hype, no fluff, no emojis. "
    "Short assured sentences."
)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def load_business_profile() -> str:
    for name in ("business.md", "business.txt"):
        if os.path.exists(name):
            with open(name, encoding="utf-8") as f:
                return f.read().strip()
    return ""


def _client():
    from anthropic import Anthropic
    return Anthropic(api_key=ANTHROPIC_API_KEY)


def _check(resp):
    if not resp.ok:
        raise RuntimeError(f"{resp.status_code} from {resp.url}: {resp.text}")
    return resp


def _notify(text: str) -> None:
    requests.post(f"{TG_BASE}/sendMessage",
                  json={"chat_id": TG_CHAT, "text": text}, timeout=30)


# ---------------------------------------------------------------------------
# Font loading
# ---------------------------------------------------------------------------

def _load_font(name, size, variant=None):
    from PIL import ImageFont
    path = os.path.join(FONT_DIR, name)
    f = ImageFont.truetype(path, size)
    if variant:
        try: f.set_variation_by_name(variant)
        except: pass
    return f


def _fonts():
    return {
        "pf_bold":  _load_font("PlayfairDisplay.ttf",  72, "Bold"),
        "dm_body":  _load_font("DMSans.ttf",           30),
        "dm_small": _load_font("DMSans.ttf",           22),
        "dm_eye":   _load_font("DMSans.ttf",           17),
        "dm_over":  _load_font("DMSans.ttf",           38, "Bold"),
    }


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def _wrap(draw, text, font, max_w):
    words = text.split()
    lines, cur = [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur); cur = w
    if cur: lines.append(cur)
    return lines


def _th(draw, text, font):
    bb = draw.textbbox((0, 0), text, font=font)
    return bb[3] - bb[1]


# ---------------------------------------------------------------------------
# Caption generation
# ---------------------------------------------------------------------------

def generate_caption(image_urls, direction: str) -> str:
    client  = _client()
    profile = load_business_profile()
    content = [{"type": "image", "source": {"type": "url", "url": u}} for u in image_urls]
    instruction = (
        (f"About the business:\n{profile}\n\n" if profile else "")
        + "The image(s) above are what will be posted. Look closely and write about "
        + "what is actually shown, not generically.\n\n"
        + f"Direction / CTA for this post: {direction}\n\n"
        + "Write one Instagram caption that fits the brand, reflects the image(s), "
        + "and works in the call to action. Return only the caption text."
    )
    content.append({"type": "text", "text": instruction})
    msg = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=500,
        system=f"You write Instagram captions. Voice: {BRAND_VOICE}",
        messages=[{"role": "user", "content": content}],
    )
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def generate_caption_no_image(direction: str) -> str:
    """For card mode: no image to pass, just direction + profile."""
    client  = _client()
    profile = load_business_profile()
    msg = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=500,
        system=f"You write Instagram captions. Voice: {BRAND_VOICE}",
        messages=[{"role": "user", "content":
            (f"About the business:\n{profile}\n\n" if profile else "")
            + f"Direction / CTA for this post: {direction}\n\n"
            + "Write one Instagram caption. Return only the caption text."
        }],
    )
    return "".join(b.text for b in msg.content if b.type == "text").strip()


# ---------------------------------------------------------------------------
# Overlay (smarter branded version)
# ---------------------------------------------------------------------------

def generate_overlay_lines(image_urls, direction: str):
    client  = _client()
    profile = load_business_profile()
    n       = len(image_urls)
    content = [{"type": "image", "source": {"type": "url", "url": u}} for u in image_urls]
    content.append({"type": "text", "text":
        (f"About the business:\n{profile}\n\n" if profile else "")
        + f"There are {n} image(s). Direction: {direction}\n\n"
        + f"Write exactly {n} short overlay line(s), one per image, max 7 words each. "
        + "Punchy benefit or CTA. No numbering, no quotes, one per line."
    })
    msg = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=300,
        system="You write short punchy marketing overlay lines.",
        messages=[{"role": "user", "content": content}],
    )
    text  = "".join(b.text for b in msg.content if b.type == "text").strip()
    lines = [ln.strip().strip('"') for ln in text.splitlines() if ln.strip()]
    while len(lines) < n: lines.append(lines[-1])
    return lines[:n]


def add_overlay(local_path: str, text: str) -> str:
    """Branded overlay: gold text on a dark scrim, DM Sans Bold."""
    from PIL import Image, ImageDraw
    fnt = _load_font("DMSans.ttf", 38, "Bold")
    img = Image.open(local_path).convert("RGB")
    W, H = img.size
    draw = ImageDraw.Draw(img, "RGBA")
    lines = _wrap(draw, text, fnt, int(W * 0.88))
    lh    = int(fnt.size * 1.35)
    pad   = int(H * 0.04)
    block = lh * len(lines)
    scrim_top = H - block - pad * 2
    draw.rectangle([0, scrim_top, W, H], fill=(10, 10, 10, 200))
    # gold rule above scrim
    draw.rectangle([int(W*0.08), scrim_top+1, int(W*0.92), scrim_top+3],
                   fill=(*BRAND_GOLD, 200))
    y = scrim_top + pad
    for ln in lines:
        lw  = draw.textlength(ln, font=fnt)
        bb  = draw.textbbox((0,0), ln, font=fnt)
        # subtle dark shadow
        draw.text(((W-lw)/2 + 2, y - bb[1] + 2), ln, font=fnt,
                  fill=(0, 0, 0, 180))
        draw.text(((W-lw)/2, y - bb[1]), ln, font=fnt, fill=BRAND_GOLD)
        y += lh
    out = local_path.rsplit(".", 1)[0] + "_overlay.png"
    img.save(out, "PNG")
    return out


# ---------------------------------------------------------------------------
# Branded card generator
# ---------------------------------------------------------------------------

def generate_card_content(direction: str) -> dict:
    """Ask Claude for the four text fields of a branded card."""
    client  = _client()
    profile = load_business_profile()
    msg = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=400,
        system=(
            "You write content for branded Instagram cards for a dark-luxury "
            "bar and restaurant inventory software brand. "
            "Respond ONLY with a JSON object, no markdown, no backticks. "
            'Keys: "eyebrow" (2-3 words, category label), '
            '"headline" (punchy, max 10 words, can use \\n for a line break), '
            '"body" (1-2 sentences, concrete benefit, max 30 words), '
            '"cta" (short action phrase, max 8 words).'
        ),
        messages=[{"role": "user", "content":
            (f"Business context:\n{profile}\n\n" if profile else "")
            + f"Direction for this post: {direction}\n\n"
            + "Generate the four card text fields as JSON."
        }],
    )
    import json
    raw = "".join(b.text for b in msg.content if b.type == "text").strip()
    raw = raw.replace("```json","").replace("```","").strip()
    return json.loads(raw)


def _measure_block(draw, fields, fonts, max_w):
    h = 26 + 18 + 22
    for raw in fields["headline"].split("\n"):
        for ln in _wrap(draw, raw, fonts["pf_bold"], max_w):
            h += _th(draw, ln, fonts["pf_bold"]) + 14
    h += 8 + 1 + 32
    for ln in _wrap(draw, fields["body"], fonts["dm_body"], max_w):
        h += _th(draw, ln, fonts["dm_body"]) + 12
    h += 52 + 72
    return h


def _render_card_content(draw, img, fields, fonts, max_w, start_y, logo):
    from PIL import Image
    cx = 1080 // 2
    y  = start_y

    # Eyebrow
    ey = "  ".join(fields["eyebrow"].upper())
    ew = draw.textlength(ey, font=fonts["dm_eye"])
    draw.text(((1080-ew)/2, y), ey, font=fonts["dm_eye"], fill=BRAND_GOLD)
    y += 26
    draw.rectangle([(cx-24,y),(cx+24,y+1)], fill=(*BRAND_GOLD,160))
    y += 22

    # Headline
    for raw in fields["headline"].split("\n"):
        for ln in _wrap(draw, raw, fonts["pf_bold"], max_w):
            lw = draw.textlength(ln, font=fonts["pf_bold"])
            bb = draw.textbbox((0,0), ln, font=fonts["pf_bold"])
            draw.text(((1080-lw)/2, y - bb[1]), ln,
                      font=fonts["pf_bold"], fill=BRAND_CREAM)
            y += bb[3]-bb[1] + 14
    y += 8
    draw.rectangle([(cx-60,y),(cx+60,y+1)], fill=(*BRAND_GOLD,120))
    y += 32

    # Body
    for ln in _wrap(draw, fields["body"], fonts["dm_body"], max_w):
        lw = draw.textlength(ln, font=fonts["dm_body"])
        bb = draw.textbbox((0,0), ln, font=fonts["dm_body"])
        draw.text(((1080-lw)/2, y - bb[1]), ln,
                  font=fonts["dm_body"], fill=BRAND_MUTED)
        y += bb[3]-bb[1] + 12
    y += 52

    # CTA button: solid gold, dark text
    cta_up = fields["cta"].upper()
    cw     = draw.textlength(cta_up, font=fonts["dm_small"])
    bpx, bh = 44, 72
    bw     = int(cw + bpx*2)
    bx     = (1080-bw)//2
    draw.rounded_rectangle([bx,y,bx+bw,y+bh], radius=3, fill=BRAND_GOLD)
    bb = draw.textbbox((0,0), cta_up, font=fonts["dm_small"])
    th = bb[3]-bb[1]
    draw.text((bx+bpx, y+(bh-th)//2 - bb[1]), cta_up,
              font=fonts["dm_small"], fill=BRAND_BG)

    # Logo pinned to bottom
    lh_px  = 52
    lw_px  = int(logo.width * lh_px / logo.height)
    logo_r = logo.resize((lw_px, lh_px), Image.LANCZOS)
    lx     = (1080-lw_px)//2
    ly     = 1350 - 52 - lh_px
    draw.rectangle([(60+40,ly-20),(1080-60-40,ly-19)], fill=(*BRAND_GOLD,45))
    img.paste(logo_r, (lx,ly), logo_r)


def render_stat_card(fields: dict, out_path: str) -> str:
    from PIL import Image, ImageDraw
    W, H = 1080, 1350
    fonts = _fonts()
    logo  = Image.open(LOGO_LIGHT).convert("RGBA")
    img   = Image.new("RGB", (W,H), BRAND_BG)
    draw  = ImageDraw.Draw(img, "RGBA")
    PAD   = 60
    MAX_W = W - PAD*2 - 40
    LOGO_ZONE = 52 + 20 + 32
    USABLE    = H - PAD - 4 - PAD - LOGO_ZONE
    dummy = ImageDraw.Draw(Image.new("RGB",(10,10)))
    bh    = _measure_block(dummy, fields, fonts, MAX_W)
    top   = PAD + 4 + max(0, (USABLE - bh)//2)
    draw.rectangle([28,28,W-28,H-28], outline=(*BRAND_GOLD,55), width=1)
    draw.rectangle([PAD,PAD,W-PAD,PAD+4], fill=BRAND_GOLD)
    _render_card_content(draw, img, fields, fonts, MAX_W, top, logo)
    img.save(out_path, "PNG")
    return out_path


def render_atm_card(fields: dict, out_path: str) -> str:
    from PIL import Image, ImageDraw, ImageFilter
    W, H = 1080, 1350
    fonts = _fonts()
    logo  = Image.open(LOGO_LIGHT).convert("RGBA")
    # Pick a random background photo
    bg_files = [f for f in glob.glob(os.path.join(BG_DIR, "*"))
                if f.lower().endswith((".jpg",".jpeg",".png"))
                and ".gitkeep" not in f]
    if not bg_files:
        return render_stat_card(fields, out_path)
    bg = Image.open(random.choice(bg_files)).convert("RGB")
    bg = bg.resize((W,H), Image.LANCZOS)
    bg = bg.filter(ImageFilter.GaussianBlur(radius=4))
    img  = bg.copy()
    draw = ImageDraw.Draw(img, "RGBA")
    draw.rectangle([0,0,W,H], fill=(0,0,0,168))
    PAD   = 60
    MAX_W = W - PAD*2 - 40
    LOGO_ZONE = 52 + 20 + 32
    USABLE    = H - PAD - 4 - PAD - LOGO_ZONE
    dummy = ImageDraw.Draw(Image.new("RGB",(10,10)))
    bh    = _measure_block(dummy, fields, fonts, MAX_W)
    top   = PAD + 4 + max(0, (USABLE - bh)//2)
    draw.rectangle([28,28,W-28,H-28], outline=(*BRAND_GOLD,70), width=1)
    draw.rectangle([PAD,PAD,W-PAD,PAD+4], fill=BRAND_GOLD)
    _render_card_content(draw, img, fields, fonts, MAX_W, top, logo)
    img.save(out_path, "PNG")
    return out_path


# ---------------------------------------------------------------------------
# Image normalisation
# ---------------------------------------------------------------------------

def normalize_for_ig(local_path: str) -> str:
    from PIL import Image
    img = Image.open(local_path).convert("RGB")
    bg  = img.getpixel((0,0))
    img.thumbnail((1080,1350), Image.LANCZOS)
    canvas = Image.new("RGB",(1080,1350), bg)
    canvas.paste(img,((1080-img.width)//2,(1350-img.height)//2))
    out = local_path.rsplit(".",1)[0] + "_ig.png"
    canvas.save(out,"PNG")
    return out


# ---------------------------------------------------------------------------
# S3 / R2 upload
# ---------------------------------------------------------------------------

def upload_image(local_path: str) -> str:
    import boto3
    bucket      = os.environ["S3_BUCKET"]
    public_base = os.environ["S3_PUBLIC_BASE"].rstrip("/")
    ext         = os.path.splitext(local_path)[1] or ".jpg"
    key         = f"ig/{uuid.uuid4().hex}{ext}"
    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["S3_ENDPOINT"],
        aws_access_key_id=os.environ["S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ["S3_SECRET_KEY"],
        region_name="auto",
    )
    ct = "image/png" if ext.lower()==".png" else "image/jpeg"
    with open(local_path,"rb") as f:
        s3.put_object(Bucket=bucket, Key=key, Body=f, ContentType=ct)
    return f"{public_base}/{key}"


def resolve_paths(image_arg: str):
    names = [n.strip() for n in image_arg.split(",") if n.strip()]
    paths = []
    for n in names:
        p = n if os.path.exists(n) else os.path.join("inbox", os.path.basename(n))
        paths.append(p)
    return paths


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def request_approval(image_urls, caption: str, tag: str) -> None:
    if len(image_urls) == 1:
        requests.post(f"{TG_BASE}/sendPhoto",
                      data={"chat_id":TG_CHAT,"photo":image_urls[0]}, timeout=30)
    else:
        media = [{"type":"photo","media":u} for u in image_urls]
        requests.post(f"{TG_BASE}/sendMediaGroup",
                      json={"chat_id":TG_CHAT,"media":media}, timeout=60)
    count = len(image_urls)
    label = "1 image" if count==1 else f"{count} images (carousel)"
    kb    = {"inline_keyboard":[[
        {"text":"Approve","callback_data":f"approve:{tag}"},
        {"text":"Reject", "callback_data":f"reject:{tag}"},
    ]]}
    requests.post(f"{TG_BASE}/sendMessage",
                  json={"chat_id":TG_CHAT,
                        "text":f"Post this? ({label})\n\n{caption}",
                        "reply_markup":kb}, timeout=30)


def wait_for_approval(tag: str, timeout_s: int = APPROVAL_TIMEOUT):
    deadline = time.time() + timeout_s
    offset   = None
    while time.time() < deadline:
        params = {"timeout":30,"allowed_updates":'["callback_query"]'}
        if offset: params["offset"] = offset
        resp = requests.get(f"{TG_BASE}/getUpdates", params=params, timeout=40)
        for upd in resp.json().get("result",[]):
            offset = upd["update_id"] + 1
            cq     = upd.get("callback_query")
            if not cq: continue
            requests.post(f"{TG_BASE}/answerCallbackQuery",
                          json={"callback_query_id":cq["id"]}, timeout=30)
            data = cq.get("data","")
            if data == f"approve:{tag}":
                _notify("Approved. Publishing now."); return True
            if data == f"reject:{tag}":
                _notify("Rejected. Nothing posted."); return False
    _notify("Approval timed out. Nothing posted.")
    return None


# ---------------------------------------------------------------------------
# Instagram publish
# ---------------------------------------------------------------------------

def create_container(image_url, caption):
    resp = requests.post(f"{GRAPH_BASE}/{IG_USER_ID}/media",
                         data={"image_url":image_url,"caption":caption,
                               "access_token":IG_ACCESS_TOKEN}, timeout=30)
    _check(resp); return resp.json()["id"]


def create_carousel_item(image_url):
    resp = requests.post(f"{GRAPH_BASE}/{IG_USER_ID}/media",
                         data={"image_url":image_url,"is_carousel_item":"true",
                               "access_token":IG_ACCESS_TOKEN}, timeout=30)
    _check(resp); return resp.json()["id"]


def create_carousel_container(child_ids, caption):
    resp = requests.post(f"{GRAPH_BASE}/{IG_USER_ID}/media",
                         data={"media_type":"CAROUSEL",
                               "children":",".join(child_ids),
                               "caption":caption,
                               "access_token":IG_ACCESS_TOKEN}, timeout=30)
    _check(resp); return resp.json()["id"]


def wait_for_container(creation_id, attempts=15, delay=4):
    for _ in range(attempts):
        resp = requests.get(f"{GRAPH_BASE}/{creation_id}",
                            params={"fields":"status_code",
                                    "access_token":IG_ACCESS_TOKEN}, timeout=30)
        _check(resp)
        s = resp.json().get("status_code")
        if s == "FINISHED": return
        if s == "ERROR": raise RuntimeError("Container processing failed")
        time.sleep(delay)
    raise TimeoutError("Container did not finish in time")


def publish(creation_id):
    resp = requests.post(f"{GRAPH_BASE}/{IG_USER_ID}/media_publish",
                         data={"creation_id":creation_id,
                               "access_token":IG_ACCESS_TOKEN}, timeout=30)
    _check(resp); return resp.json()["id"]


def refresh_token():
    resp = requests.get(REFRESH_URL,
                        params={"grant_type":"ig_refresh_token",
                                "access_token":IG_ACCESS_TOKEN}, timeout=30)
    _check(resp); return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) == 2 and sys.argv[1] == "--refresh":
        print(refresh_token()); return

    if len(sys.argv) < 4:
        print('Usage: python ig_agent.py "<image(s) or none>" "<direction>" "<mode>" [overlay yes/no]')
        print('Modes: photo | card_stat | card_atm')
        sys.exit(1)

    image_arg = sys.argv[1]
    direction = sys.argv[2]
    mode      = sys.argv[3].strip().lower()
    overlay   = len(sys.argv) > 4 and sys.argv[4].strip().lower() in ("yes","true","1","on")

    tag = uuid.uuid4().hex
    os.makedirs("/tmp/ig_work", exist_ok=True)

    # --- CARD MODES ---
    if mode in ("card_stat", "card_atm"):
        print("Asking Claude to write card content...")
        fields = generate_card_content(direction)
        print(f"  eyebrow:  {fields.get('eyebrow')}")
        print(f"  headline: {fields.get('headline')}")
        print(f"  body:     {fields.get('body')}")
        print(f"  cta:      {fields.get('cta')}")

        out_path = "/tmp/ig_work/branded_card.png"
        if mode == "card_atm":
            print("Rendering atmospheric card...")
            render_atm_card(fields, out_path)
        else:
            print("Rendering stat card...")
            render_stat_card(fields, out_path)

        print("Uploading card...")
        image_urls = [upload_image(out_path)]

        print("Generating caption...")
        caption = generate_caption_no_image(direction)

        print("Sending to Telegram for approval...")
        request_approval(image_urls, caption, tag)

        if wait_for_approval(tag) is not True:
            print("Not approved. Exiting."); return

        print("Creating container...")
        creation_id = create_container(image_urls[0], caption)
        wait_for_container(creation_id)
        media_id = publish(creation_id)
        _notify(f"Posted branded card. Media id {media_id}")
        print(f"Done. Media id: {media_id}")
        return

    # --- PHOTO MODE ---
    paths = resolve_paths(image_arg)
    if not paths:
        print("No image filenames given."); sys.exit(1)
    if len(paths) > 10:
        print("Instagram carousels allow at most 10 images."); sys.exit(1)

    print("Preparing images (fitting to 4:5 canvas)...")
    paths = [normalize_for_ig(p) for p in paths]

    print(f"Uploading {len(paths)} image(s) for analysis...")
    source_urls = [upload_image(p) for p in paths]

    if overlay:
        print("Asking Claude for overlay lines...")
        lines = generate_overlay_lines(source_urls, direction)
        print("Drawing branded overlays...")
        paths = [add_overlay(p, t) for p, t in zip(paths, lines)]
        print("Uploading final images...")
        image_urls = [upload_image(p) for p in paths]
    else:
        image_urls = source_urls

    print("Asking Claude to write the caption...")
    caption = generate_caption(image_urls, direction)

    print("Sending to Telegram for approval...")
    request_approval(image_urls, caption, tag)

    if wait_for_approval(tag) is not True:
        print("Not approved. Exiting."); return

    if len(image_urls) == 1:
        print("Creating container...")
        creation_id = create_container(image_urls[0], caption)
    else:
        print("Creating carousel items...")
        child_ids   = [create_carousel_item(u) for u in image_urls]
        print("Creating carousel container...")
        creation_id = create_carousel_container(child_ids, caption)

    wait_for_container(creation_id)
    media_id = publish(creation_id)
    _notify(f"Posted. Media id {media_id}")
    print(f"Done. Media id: {media_id}")


if __name__ == "__main__":
    main()
