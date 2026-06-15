"""
Instagram posting agent with Telegram approval (Instagram Login API).

Captions are written by Claude, which LOOKS AT your images, reads your saved
business profile (business.md), and works in the per-post direction you give it.

Supports a single image OR a carousel of 2 to 10 images in one post.

Flow (one GitHub Actions run does all of it):
  1. You supply one or more images and a per-post direction / call to action.
  2. Images upload to Cloudflare R2 to get public HTTPS URLs.
  3. Claude views the images + business.md + your direction and writes a caption.
  4. Photos and caption go to Telegram with Approve / Reject buttons.
  5. On Approve it publishes. On Reject or timeout it stops.

Token refresh:
  python ig_agent.py --refresh

Setup:
  pip install requests anthropic boto3 python-dotenv

Run locally:
  Single:    python ig_agent.py image.jpg "weekend brunch, book a table"
  Carousel:  python ig_agent.py "a.jpg,b.jpg,c.jpg" "new tasting menu, link in bio"
  The order you list files is the order they appear in the post.
"""

import os
import sys
import time
import uuid
import requests
from dotenv import load_dotenv

load_dotenv()

GRAPH_VERSION = "v23.0"
GRAPH_BASE = f"https://graph.instagram.com/{GRAPH_VERSION}"
REFRESH_URL = "https://graph.instagram.com/refresh_access_token"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
IG_ACCESS_TOKEN = os.environ.get("IG_ACCESS_TOKEN")
IG_USER_ID = os.environ.get("IG_USER_ID")

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID")
TG_BASE = f"https://api.telegram.org/bot{TG_TOKEN}" if TG_TOKEN else None

APPROVAL_TIMEOUT = 30 * 60

# Fallback voice if business.md is missing. business.md is the better place to
# describe your business; this just sets the baseline writing style.
BRAND_VOICE = (
    "Friendly, concrete, and useful. Short sentences. "
    "No hype words. End with one clear call to action. "
    "Include 3 to 5 relevant hashtags on the last line."
)


def load_business_profile() -> str:
    for name in ("business.md", "business.txt"):
        if os.path.exists(name):
            with open(name, encoding="utf-8") as f:
                return f.read().strip()
    return ""


def generate_caption(image_urls, direction: str) -> str:
    """Claude views the images, reads the business profile, applies the direction."""
    from anthropic import Anthropic

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    profile = load_business_profile()

    content = [{"type": "image", "source": {"type": "url", "url": u}} for u in image_urls]
    instruction = (
        (f"About the business:\n{profile}\n\n" if profile else "")
        + "The image(s) above are what will be posted together. Look closely at what "
        + "they actually show and write about that specifically, not generically.\n\n"
        + f"Direction for this post (angle, offer, or call to action): {direction}\n\n"
        + "Write one Instagram caption that fits the business, reflects what is genuinely "
        + "in the images, and naturally works in the call to action. Return only the "
        + "caption text, nothing else."
    )
    content.append({"type": "text", "text": instruction})

    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system=f"You write Instagram captions for a business. Baseline voice: {BRAND_VOICE}",
        messages=[{"role": "user", "content": content}],
    )
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def upload_image(local_path: str) -> str:
    import boto3

    bucket = os.environ["S3_BUCKET"]
    public_base = os.environ["S3_PUBLIC_BASE"].rstrip("/")
    ext = os.path.splitext(local_path)[1] or ".jpg"
    key = f"ig/{uuid.uuid4().hex}{ext}"

    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["S3_ENDPOINT"],
        aws_access_key_id=os.environ["S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ["S3_SECRET_KEY"],
        region_name="auto",
    )
    content_type = "image/png" if ext.lower() == ".png" else "image/jpeg"
    with open(local_path, "rb") as f:
        s3.put_object(Bucket=bucket, Key=key, Body=f, ContentType=content_type)

    return f"{public_base}/{key}"


def resolve_paths(image_arg: str):
    names = [n.strip() for n in image_arg.split(",") if n.strip()]
    paths = []
    for n in names:
        p = n if os.path.exists(n) else os.path.join("inbox", os.path.basename(n))
        paths.append(p)
    return paths


def request_approval(image_urls, caption: str, tag: str) -> None:
    if len(image_urls) == 1:
        requests.post(
            f"{TG_BASE}/sendPhoto",
            data={"chat_id": TG_CHAT, "photo": image_urls[0]},
            timeout=30,
        )
    else:
        media = [{"type": "photo", "media": u} for u in image_urls]
        requests.post(
            f"{TG_BASE}/sendMediaGroup",
            json={"chat_id": TG_CHAT, "media": media},
            timeout=60,
        )
    count = len(image_urls)
    label = "1 image" if count == 1 else f"{count} images (carousel)"
    keyboard = {"inline_keyboard": [[
        {"text": "Approve", "callback_data": f"approve:{tag}"},
        {"text": "Reject", "callback_data": f"reject:{tag}"},
    ]]}
    requests.post(
        f"{TG_BASE}/sendMessage",
        json={
            "chat_id": TG_CHAT,
            "text": f"Post this? ({label})\n\n{caption}",
            "reply_markup": keyboard,
        },
        timeout=30,
    )


def wait_for_approval(tag: str, timeout_s: int = APPROVAL_TIMEOUT):
    deadline = time.time() + timeout_s
    offset = None
    while time.time() < deadline:
        params = {"timeout": 30, "allowed_updates": '["callback_query"]'}
        if offset is not None:
            params["offset"] = offset
        resp = requests.get(f"{TG_BASE}/getUpdates", params=params, timeout=40)
        for upd in resp.json().get("result", []):
            offset = upd["update_id"] + 1
            cq = upd.get("callback_query")
            if not cq:
                continue
            requests.post(
                f"{TG_BASE}/answerCallbackQuery",
                json={"callback_query_id": cq["id"]},
                timeout=30,
            )
            data = cq.get("data", "")
            if data == f"approve:{tag}":
                _notify("Approved. Publishing now.")
                return True
            if data == f"reject:{tag}":
                _notify("Rejected. Nothing posted.")
                return False
    _notify("Approval timed out. Nothing posted.")
    return None


def _notify(text: str) -> None:
    requests.post(
        f"{TG_BASE}/sendMessage",
        json={"chat_id": TG_CHAT, "text": text},
        timeout=30,
    )


def create_container(image_url: str, caption: str) -> str:
    resp = requests.post(
        f"{GRAPH_BASE}/{IG_USER_ID}/media",
        data={"image_url": image_url, "caption": caption, "access_token": IG_ACCESS_TOKEN},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def create_carousel_item(image_url: str) -> str:
    resp = requests.post(
        f"{GRAPH_BASE}/{IG_USER_ID}/media",
        data={
            "image_url": image_url,
            "is_carousel_item": "true",
            "access_token": IG_ACCESS_TOKEN,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def create_carousel_container(child_ids, caption: str) -> str:
    resp = requests.post(
        f"{GRAPH_BASE}/{IG_USER_ID}/media",
        data={
            "media_type": "CAROUSEL",
            "children": ",".join(child_ids),
            "caption": caption,
            "access_token": IG_ACCESS_TOKEN,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def wait_for_container(creation_id: str, attempts: int = 15, delay: int = 4) -> None:
    for _ in range(attempts):
        resp = requests.get(
            f"{GRAPH_BASE}/{creation_id}",
            params={"fields": "status_code", "access_token": IG_ACCESS_TOKEN},
            timeout=30,
        )
        resp.raise_for_status()
        status = resp.json().get("status_code")
        if status == "FINISHED":
            return
        if status == "ERROR":
            raise RuntimeError("Container processing failed")
        time.sleep(delay)
    raise TimeoutError("Container did not finish in time")


def publish(creation_id: str) -> str:
    resp = requests.post(
        f"{GRAPH_BASE}/{IG_USER_ID}/media_publish",
        data={"creation_id": creation_id, "access_token": IG_ACCESS_TOKEN},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def refresh_token() -> str:
    resp = requests.get(
        REFRESH_URL,
        params={"grant_type": "ig_refresh_token", "access_token": IG_ACCESS_TOKEN},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def main() -> None:
    if len(sys.argv) == 2 and sys.argv[1] == "--refresh":
        print(refresh_token())
        return

    if len(sys.argv) < 3:
        print('Usage: python ig_agent.py "<image or a.jpg,b.jpg,c.jpg>" "<direction / CTA>"')
        sys.exit(1)

    image_arg, direction = sys.argv[1], sys.argv[2]
    paths = resolve_paths(image_arg)
    if not paths:
        print("No image filenames given.")
        sys.exit(1)
    if len(paths) > 10:
        print("Instagram carousels allow at most 10 images.")
        sys.exit(1)

    tag = uuid.uuid4().hex

    print(f"Uploading {len(paths)} image(s) to R2...")
    image_urls = [upload_image(p) for p in paths]

    print("Asking Claude to view the images and write the caption...")
    caption = generate_caption(image_urls, direction)

    print("Sending to Telegram for approval...")
    request_approval(image_urls, caption, tag)

    decision = wait_for_approval(tag)
    if decision is not True:
        print("Not approved. Exiting.")
        return

    if len(image_urls) == 1:
        print("Creating container...")
        creation_id = create_container(image_urls[0], caption)
    else:
        print("Creating carousel items...")
        child_ids = [create_carousel_item(u) for u in image_urls]
        print("Creating carousel container...")
        creation_id = create_carousel_container(child_ids, caption)

    wait_for_container(creation_id)

    print("Publishing...")
    media_id = publish(creation_id)
    _notify(f"Posted. Media id {media_id}")
    print(f"Done. Published media id: {media_id}")


if __name__ == "__main__":
    main()
