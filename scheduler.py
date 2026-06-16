"""
BarLedger scheduled post runner.
Reads schedule.json, fires due posts via ig_agent functions,
marks them posted, generates next recurrence if enabled,
and commits the updated schedule.json back to GitHub.

Runs every 15 minutes via .github/workflows/scheduler.yml.
"""

import os, sys, json, uuid, base64
from datetime import datetime, timezone as dt_timezone, timedelta
from zoneinfo import ZoneInfo

import requests as req

SCHEDULE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schedule.json")

GH_PAT    = os.environ.get("GH_PAT", "")
GH_REPO   = os.environ.get("GITHUB_REPO", "Anthony11989/instagram-marketing-agent")
GH_BRANCH = os.environ.get("GITHUB_BRANCH", "master")
GH_API    = "https://api.github.com"


def _gh_headers():
    return {
        "Authorization": f"token {GH_PAT}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "BarLedger-Scheduler",
    }


def load_schedule():
    if os.path.exists(SCHEDULE_FILE):
        with open(SCHEDULE_FILE) as f:
            return json.load(f)
    return {"posts": []}


def save_schedule_local(data):
    with open(SCHEDULE_FILE, "w") as f:
        json.dump(data, f, indent=2)


def push_schedule(data):
    """Write schedule.json locally and commit to GitHub."""
    save_schedule_local(data)

    if not GH_PAT:
        print("GH_PAT not set - schedule.json updated locally only.")
        return

    content = json.dumps(data, indent=2)
    encoded = base64.b64encode(content.encode()).decode()
    headers = _gh_headers()

    r   = req.get(f"{GH_API}/repos/{GH_REPO}/contents/schedule.json?ref={GH_BRANCH}",
                  headers=headers, timeout=15)
    sha = r.json().get("sha") if r.ok else None

    payload = {
        "message": "scheduler: update schedule.json",
        "content": encoded,
        "branch":  GH_BRANCH,
    }
    if sha:
        payload["sha"] = sha

    r = req.put(f"{GH_API}/repos/{GH_REPO}/contents/schedule.json",
                headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    print("schedule.json committed to GitHub.")


def _tz(tz_name):
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("UTC")


def parse_scheduled_time(scheduled_str, tz_name):
    """Return a UTC-aware datetime for a naive ISO string + timezone name."""
    dt = datetime.fromisoformat(scheduled_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz(tz_name))
    return dt.astimezone(dt_timezone.utc)


def next_occurrence(scheduled_str, tz_name, recurring):
    """Return the next ISO datetime string (naive, in the post's timezone)."""
    tz = _tz(tz_name)
    dt = datetime.fromisoformat(scheduled_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)

    freq = recurring.get("frequency", "weekly")
    days = [d.lower() for d in recurring.get("days", [])]

    day_map = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
               "friday": 4, "saturday": 5, "sunday": 6}

    if freq == "daily":
        next_dt = dt + timedelta(days=1)
    elif freq == "weekly" and days:
        targets = sorted([day_map[d] for d in days if d in day_map])
        if not targets:
            next_dt = dt + timedelta(weeks=1)
        else:
            cur_wd  = dt.weekday()
            later   = [d for d in targets if d > cur_wd]
            if later:
                next_dt = dt + timedelta(days=later[0] - cur_wd)
            else:
                next_dt = dt + timedelta(days=7 - cur_wd + targets[0])
    else:
        next_dt = dt + timedelta(weeks=1)

    return next_dt.replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S")


def fire_post(post):
    """
    Execute one scheduled post.
    Returns True if approved and published, False if rejected/timed-out.
    Raises on hard errors.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import ig_agent  # noqa: PLC0415  (lazy import after path setup)

    mode   = post.get("mode", "card_atm")
    inputs = post.get("inputs", {})
    direction        = inputs.get("direction", "promote the brand")
    images           = inputs.get("images", [])
    override_headline = inputs.get("override_headline", "").strip()
    override_body     = inputs.get("override_body", "").strip()
    override_cta      = inputs.get("override_cta", "").strip()

    tag = uuid.uuid4().hex
    os.makedirs("/tmp/ig_work", exist_ok=True)

    # ---- Card modes (card_atm / card_stat / aliases) ----
    if mode in ("card_atm", "card_stat", "atmospheric", "stat"):
        if override_headline and override_body and override_cta:
            fields = {
                "eyebrow":  "BARLEDGER",
                "headline": override_headline,
                "body":     override_body,
                "cta":      override_cta,
            }
        else:
            fields = ig_agent.generate_card_content(direction)
            if override_headline:
                fields["headline"] = override_headline
            if override_body:
                fields["body"] = override_body
            if override_cta:
                fields["cta"] = override_cta

        print(f"  eyebrow:  {fields.get('eyebrow')}")
        print(f"  headline: {fields.get('headline')}")
        print(f"  body:     {fields.get('body')}")
        print(f"  cta:      {fields.get('cta')}")

        out_path = "/tmp/ig_work/card.png"
        if mode in ("card_atm", "atmospheric"):
            print("Rendering atmospheric card...")
            ig_agent.render_atm_card(fields, out_path)
        else:
            print("Rendering stat card...")
            ig_agent.render_stat_card(fields, out_path)

        print("Uploading card to R2...")
        image_urls = [ig_agent.upload_image(out_path)]

        print("Generating caption...")
        caption = ig_agent.generate_caption_no_image(direction)

        print("Requesting Telegram approval...")
        ig_agent.request_approval(image_urls, caption, tag)
        approved = ig_agent.wait_for_approval(tag)
        if approved is not True:
            print("Not approved or timed out - skipping.")
            return False

        print("Publishing to Instagram...")
        cid = ig_agent.create_container(image_urls[0], caption)
        ig_agent.wait_for_container(cid)
        media_id = ig_agent.publish(cid)
        ig_agent._notify(f"Scheduled post published. Media ID: {media_id}")
        print(f"Published. Media ID: {media_id}")
        return True

    # ---- Photo mode ----
    elif mode == "photo":
        if not images:
            raise ValueError("Photo mode requires at least one filename in inputs.images")

        paths = ig_agent.resolve_paths(",".join(images))
        if not paths:
            raise ValueError(f"Could not resolve image paths: {images}")

        paths      = [ig_agent.normalize_for_ig(p) for p in paths]
        image_urls = [ig_agent.upload_image(p) for p in paths]

        print("Generating caption...")
        caption = ig_agent.generate_caption(image_urls, direction)

        print("Requesting Telegram approval...")
        ig_agent.request_approval(image_urls, caption, tag)
        approved = ig_agent.wait_for_approval(tag)
        if approved is not True:
            print("Not approved or timed out - skipping.")
            return False

        print("Publishing to Instagram...")
        if len(image_urls) == 1:
            cid = ig_agent.create_container(image_urls[0], caption)
        else:
            child_ids = [ig_agent.create_carousel_item(u) for u in image_urls]
            cid       = ig_agent.create_carousel_container(child_ids, caption)

        ig_agent.wait_for_container(cid)
        media_id = ig_agent.publish(cid)
        ig_agent._notify(f"Scheduled photo post published. Media ID: {media_id}")
        print(f"Published. Media ID: {media_id}")
        return True

    else:
        raise ValueError(f"Unknown post mode: {mode!r}")


def main():
    now_utc = datetime.now(dt_timezone.utc)
    print(f"Scheduler running at {now_utc.isoformat()}")

    data    = load_schedule()
    changed = False

    for post in data["posts"]:
        if post.get("status") != "pending":
            continue

        tz_name       = post.get("timezone", "America/Phoenix")
        scheduled_str = post.get("scheduled_time", "")
        if not scheduled_str:
            print(f"Post {post.get('id','?')[:8]} has no scheduled_time - skipping.")
            continue

        try:
            due_utc = parse_scheduled_time(scheduled_str, tz_name)
        except Exception as e:
            print(f"Bad scheduled_time on {post.get('id','?')[:8]}: {e}")
            continue

        if due_utc > now_utc:
            print(f"Post {post['id'][:8]} not due until {scheduled_str} ({tz_name})")
            continue

        print(f"\nFiring post {post['id'][:8]}  mode={post['mode']}  due={scheduled_str}")
        try:
            success = fire_post(post)
            post["status"]    = "posted" if success else "skipped"
            post["posted_at"] = now_utc.isoformat()
            changed = True
        except Exception as e:
            print(f"Error on post {post['id'][:8]}: {e}")
            post["status"] = "error"
            post["error"]  = str(e)
            changed = True

        # Create next recurrence only on successful publish
        if post.get("status") == "posted":
            recurring = post.get("recurring", {})
            if recurring.get("enabled"):
                next_time = next_occurrence(scheduled_str, tz_name, recurring)
                new_post  = {
                    "id":             str(uuid.uuid4()),
                    "mode":           post["mode"],
                    "scheduled_time": next_time,
                    "timezone":       tz_name,
                    "status":         "pending",
                    "inputs":         post.get("inputs", {}),
                    "recurring":      recurring,
                }
                data["posts"].append(new_post)
                print(f"Next occurrence queued: {new_post['id'][:8]} at {next_time} ({tz_name})")
                changed = True

    if changed:
        push_schedule(data)
    else:
        print("No posts due. Nothing to do.")


if __name__ == "__main__":
    main()
