"""
Instagram posting agent with Telegram approval (Instagram Login API).

Normal flow (one GitHub Actions run does all of it):
  1. You supply a local image and a topic.
  2. Claude writes a caption.
  3. The image is uploaded to Cloudflare R2 to get a public HTTPS URL.
  4. The photo and caption are sent to you in Telegram with Approve / Reject buttons.
  5. The run waits for your tap. On Approve it publishes. On Reject or timeout it stops.

Token refresh:
  python ig_agent.py --refresh
  Prints a fresh 60 day Instagram token to stdout. The refresh workflow writes it back.

Setup:
  pip install requests anthropic boto3 python-dotenv

  .env (local) or GitHub Actions Secrets (cloud):
    ANTHROPIC_API_KEY=...
    IG_ACCESS_TOKEN=...        long-lived Instagram user token (60 day)
    IG_USER_ID=...             your Instagram user id
    S3_BUCKET=...              your R2 bucket name
    S3_ENDPOINT=...            https://<accountid>.r2.cloudflarestorage.com
    S3_ACCESS_KEY=...          R2 access key id
    S3_SECRET_KEY=...          R2 secret access key
    S3_PUBLIC_BASE=...         your r2.dev public URL, e.g. https://pub-xxxx.r2.dev
    TELEGRAM_BOT_TOKEN=...     from BotFather
    TELEGRAM_CHAT_ID=...       your chat id with the bot

Run locally:
  python ig_agent.py path/to/image.jpg "topic or angle for the caption"
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

# How long the run waits for your tap before giving up, in seconds.
APPROVAL_TIMEOUT = 30 * 60

BRAND_VOICE = (
    "Friendly, concrete, and useful. Short sentences. "
    "No hype words. End with one clear call to action. "
    "Include 3 to 5 relevant hashtags on the last line."
)


def generate_caption(topic: str) -> str:
    from anthropic import Anthropic

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        system=f"You write Instagram captions for a business. Voice: {BRAND_VOICE}",
        messages=[{
            "role": "user",
            "content": f"Write one Instagram caption about: {topic}. "
                       f"Return only the caption text, nothing else.",
        }],
    )
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def upload_image(local_path: str) -> str:
    """Upload to Cloudflare R2 and return a public HTTPS URL."""
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


def request_approval(image_url: str, caption: str, tag: str) -> None:
    """Send the photo and caption to Telegram with Approve / Reject buttons."""
    requests.post(
        f"{TG_BASE}/sendPhoto",
        data={"chat_id": TG_CHAT, "photo": image_url},
        timeout=30,
    )
    keyboard = {"inline_keyboard": [[
        {"text": "Approve", "callback_data": f"approve:{tag}"},
        {"text": "Reject", "callback_data": f"reject:{tag}"},
    ]]}
    requests.post(
        f"{TG_BASE}/sendMessage",
        json={
            "chat_id": TG_CHAT,
            "text": f"Post this caption?\n\n{caption}",
            "reply_markup": keyboard,
        },
        timeout=30,
    )


def wait_for_approval(tag: str, timeout_s: int = APPROVAL_TIMEOUT):
    """
    Long-poll Telegram for the button tap that matches this run's tag.
    Returns True (approve), False (reject), or None (timed out).
    """
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
            # A tap from an older post. Ignore it and keep waiting.
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


def wait_for_container(creation_id: str, attempts: int = 10, delay: int = 3) -> None:
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
    """Extend the long-lived Instagram token for another 60 days."""
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
        print('Usage: python ig_agent.py <image_path> "<topic>"')
        sys.exit(1)

    image_path, topic = sys.argv[1], sys.argv[2]
    tag = uuid.uuid4().hex

    print("Generating caption...")
    caption = generate_caption(topic)

    print("Uploading image to R2...")
    image_url = upload_image(image_path)

    print("Sending to Telegram for approval...")
    request_approval(image_url, caption, tag)

    decision = wait_for_approval(tag)
    if decision is not True:
        print("Not approved. Exiting.")
        return

    print("Creating container...")
    creation_id = create_container(image_url, caption)
    wait_for_container(creation_id)

    print("Publishing...")
    media_id = publish(creation_id)
    _notify(f"Posted. Media id {media_id}")
    print(f"Done. Published media id: {media_id}")


if __name__ == "__main__":
    main()
