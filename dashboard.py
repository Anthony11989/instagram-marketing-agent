"""
BarLedger Instagram Dashboard
Local web UI for managing posts, inbox, and workflows.

Setup:
  pip install flask requests pillow numpy python-dotenv anthropic

  Create dashboard.env next to this file:
    GITHUB_TOKEN=your_gh_pat_here
    GITHUB_REPO=Anthony11989/instagram-marketing-agent
    GITHUB_BRANCH=master

Run:
  python dashboard.py
  Then open http://localhost:5000 in your browser.
"""

import os, base64, json, time, threading, uuid, math, glob, random
from datetime import datetime
from flask import Flask, render_template_string, request, jsonify, send_file
import requests as req
from dotenv import load_dotenv

load_dotenv("dashboard.env")

GH_TOKEN  = os.environ.get("GITHUB_TOKEN", "")
GH_REPO   = os.environ.get("GITHUB_REPO",  "Anthony11989/instagram-marketing-agent")
GH_BRANCH = os.environ.get("GITHUB_BRANCH","master")
GH_API    = "https://api.github.com"
GH_HEADS  = {
    "Authorization": f"token {GH_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
    "User-Agent": "BarLedger-Dashboard",
}

app = Flask(__name__)
post_log = []   # in-memory log; survives until you close the server

# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------

def gh_get(path):
    r = req.get(f"{GH_API}/repos/{GH_REPO}{path}", headers=GH_HEADS, timeout=15)
    r.raise_for_status()
    return r.json()

def gh_list_folder(folder):
    try:
        items = gh_get(f"/contents/{folder}?ref={GH_BRANCH}")
        return [i for i in items if i["name"] != ".gitkeep"]
    except Exception:
        return []

def gh_upload_file(folder, filename, data_bytes):
    path    = f"{folder}/{filename}"
    encoded = base64.b64encode(data_bytes).decode()
    # check if file exists to get its sha (needed for update)
    sha = None
    try:
        existing = gh_get(f"/contents/{path}?ref={GH_BRANCH}")
        sha = existing.get("sha")
    except Exception:
        pass
    payload = {"message": f"Upload {filename} via dashboard",
               "content": encoded, "branch": GH_BRANCH}
    if sha:
        payload["sha"] = sha
    r = req.put(f"{GH_API}/repos/{GH_REPO}/contents/{path}",
                headers=GH_HEADS, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

def gh_delete_file(folder, filename):
    path = f"{folder}/{filename}"
    info = gh_get(f"/contents/{path}?ref={GH_BRANCH}")
    sha  = info["sha"]
    r = req.delete(f"{GH_API}/repos/{GH_REPO}/contents/{path}",
                   headers=GH_HEADS,
                   json={"message": f"Delete {filename} via dashboard",
                         "sha": sha, "branch": GH_BRANCH},
                   timeout=15)
    r.raise_for_status()

def gh_trigger_workflow(inputs: dict):
    r = req.post(
        f"{GH_API}/repos/{GH_REPO}/actions/workflows/post.yml/dispatches",
        headers=GH_HEADS,
        json={"ref": GH_BRANCH, "inputs": inputs},
        timeout=15,
    )
    r.raise_for_status()

def gh_get_runs(limit=10):
    try:
        data = gh_get(f"/actions/runs?per_page={limit}")
        return data.get("workflow_runs", [])
    except Exception:
        return []

def gh_get_secret_updated(secret_name):
    """Returns the updated_at date of a repo secret (not the value)."""
    try:
        r = req.get(f"{GH_API}/repos/{GH_REPO}/actions/secrets/{secret_name}",
                    headers=GH_HEADS, timeout=10)
        if r.ok:
            return r.json().get("updated_at","unknown")
    except Exception:
        pass
    return "unknown"

def gh_read_json(path):
    try:
        data    = gh_get(f"/contents/{path}?ref={GH_BRANCH}")
        content = base64.b64decode(data["content"].replace("\n","")).decode()
        return json.loads(content), data.get("sha")
    except Exception:
        return None, None

def gh_write_json(path, obj, sha=None, message="Update via dashboard"):
    content = json.dumps(obj, indent=2)
    encoded = base64.b64encode(content.encode()).decode()
    payload = {"message": message, "content": encoded, "branch": GH_BRANCH}
    if sha:
        payload["sha"] = sha
    r = req.put(f"{GH_API}/repos/{GH_REPO}/contents/{path}",
                headers=GH_HEADS, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

# ---------------------------------------------------------------------------
# Card preview (runs locally, same renderer as ig_agent)
# ---------------------------------------------------------------------------

FONT_CACHE = {}

def _get_font(name, size, variant=None):
    from PIL import ImageFont
    key = (name, size, variant)
    if key not in FONT_CACHE:
        # try local fonts/ folder first, then system
        for base in ["fonts", os.path.join(os.path.dirname(__file__), "fonts")]:
            p = os.path.join(base, name)
            if os.path.exists(p):
                f = ImageFont.truetype(p, size)
                if variant:
                    try: f.set_variation_by_name(variant)
                    except: pass
                FONT_CACHE[key] = f
                break
        else:
            FONT_CACHE[key] = ImageFont.load_default()
    return FONT_CACHE[key]

BRAND_BG   = (10,  10,  10)
BRAND_GOLD = (201, 168,  76)
BRAND_CREAM= (232, 228, 220)
BRAND_MUTED= (155, 151, 143)

def _wrap(draw, text, font, max_w):
    words = text.split()
    lines, cur = [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= max_w or not cur: cur = t
        else: lines.append(cur); cur = w
    if cur: lines.append(cur)
    return lines

def _th(draw, text, font):
    bb = draw.textbbox((0,0), text, font=font)
    return bb[3]-bb[1]

def render_preview(eyebrow, headline, body, cta, style="stat"):
    from PIL import Image, ImageDraw, ImageFilter
    import io
    headline = headline.replace("\\n", "\n")
    W, H = 1080, 1350
    pf   = _get_font("PlayfairDisplay.ttf", 96, "Bold")
    dm_b = _get_font("DMSans.ttf", 42)
    dm_s = _get_font("DMSans.ttf", 36)
    dm_e = _get_font("DMSans.ttf", 40)

    # spacing constants
    EY_ADV  = 52   # y advance after eyebrow text (covers text + gap to divider)
    EY_GAP  = 26   # y advance after eyebrow divider line
    HL_GAP  = 18   # spacing between headline lines
    PHL_GAP = 14   # gap after last headline line before divider
    DIV_GAP = 40   # gap after headline divider line
    BD_GAP  = 16   # spacing between body lines
    POST_BD = 62   # gap after body before button
    BTN_H   = 92   # button height
    BTN_PAD = 58   # horizontal padding inside button
    LH_PX   = 72   # logo height

    if style == "atm":
        bg_paths = glob.glob("backgrounds/*")
        bg_paths = [p for p in bg_paths
                    if p.lower().endswith((".jpg",".jpeg",".png"))
                    and ".gitkeep" not in p]
        if bg_paths:
            bg = Image.open(random.choice(bg_paths)).convert("RGB").resize((W,H),Image.LANCZOS)
            bg = bg.filter(ImageFilter.GaussianBlur(4))
            img = bg.copy()
        else:
            img = Image.new("RGB",(W,H),(14,12,10))
    else:
        img = Image.new("RGB",(W,H),BRAND_BG)

    draw = ImageDraw.Draw(img,"RGBA")
    if style == "atm":
        draw.rectangle([0,0,W,H], fill=(0,0,0,165))

    PAD   = 60
    MAX_W = W - PAD*2 - 40
    draw.rectangle([28,28,W-28,H-28], outline=(*BRAND_GOLD,60), width=1)
    draw.rectangle([PAD,PAD,W-PAD,PAD+4], fill=BRAND_GOLD)

    # measure block height for vertical centering
    bh = EY_ADV + 1 + EY_GAP
    for raw in headline.split("\n"):
        for ln in _wrap(draw, raw, pf, MAX_W):
            bh += _th(draw,ln,pf) + HL_GAP
    bh += PHL_GAP + 1 + DIV_GAP
    for ln in _wrap(draw, body, dm_b, MAX_W):
        bh += _th(draw,ln,dm_b) + BD_GAP
    bh += POST_BD + BTN_H

    LOGO_ZONE = LH_PX + 24 + 32
    USABLE    = H - PAD - 4 - PAD - LOGO_ZONE
    y         = PAD + 4 + max(0,(USABLE-bh)//2)
    cx        = W//2

    # eyebrow
    ey = "  ".join(eyebrow.upper())
    ew = draw.textlength(ey, font=dm_e)
    draw.text(((W-ew)/2, y), ey, font=dm_e, fill=BRAND_GOLD)
    y += EY_ADV
    draw.rectangle([(cx-32,y),(cx+32,y+1)], fill=(*BRAND_GOLD,160))
    y += EY_GAP

    # headline
    for raw in headline.split("\n"):
        for ln in _wrap(draw, raw, pf, MAX_W):
            lw = draw.textlength(ln, font=pf)
            bb = draw.textbbox((0,0), ln, font=pf)
            draw.text(((W-lw)/2, y-bb[1]), ln, font=pf, fill=BRAND_CREAM)
            y += bb[3]-bb[1] + HL_GAP
    y += PHL_GAP
    draw.rectangle([(cx-52,y),(cx+52,y+1)], fill=(*BRAND_GOLD,120))
    y += DIV_GAP

    # body
    for ln in _wrap(draw, body, dm_b, MAX_W):
        lw = draw.textlength(ln, font=dm_b)
        bb = draw.textbbox((0,0), ln, font=dm_b)
        draw.text(((W-lw)/2, y-bb[1]), ln, font=dm_b, fill=BRAND_MUTED)
        y += bb[3]-bb[1] + BD_GAP
    y += POST_BD

    # button
    cta_up = cta.upper()
    cw     = draw.textlength(cta_up, font=dm_s)
    bw     = int(cw + BTN_PAD*2)
    bx     = (W-bw)//2
    draw.rounded_rectangle([bx,y,bx+bw,y+BTN_H], radius=4, fill=BRAND_GOLD)
    bb = draw.textbbox((0,0), cta_up, font=dm_s)
    th = bb[3]-bb[1]
    draw.text((bx+BTN_PAD, y+(BTN_H-th)//2-bb[1]), cta_up, font=dm_s, fill=BRAND_BG)

    # logo
    for lp in ["xlh-logo-transparent.png",
               os.path.join(os.path.dirname(__file__),"xlh-logo-transparent.png")]:
        if os.path.exists(lp):
            logo  = Image.open(lp).convert("RGBA")
            lw_px = int(logo.width * LH_PX / logo.height)
            logo  = logo.resize((lw_px, LH_PX), Image.LANCZOS)
            lx    = (W-lw_px)//2
            ly    = H - 52 - LH_PX
            draw.rectangle([(PAD+20,ly-24),(W-PAD-20,ly-23)], fill=(*BRAND_GOLD,45))
            img.paste(logo,(lx,ly),logo)
            break

    buf = io.BytesIO()
    img.save(buf,"PNG")
    buf.seek(0)
    return buf

# ---------------------------------------------------------------------------
# HTML UI
# ---------------------------------------------------------------------------

HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BarLedger Instagram Dashboard</title>
<style>
  :root {
    --bg:      #0a0a0a;
    --card:    #111111;
    --border:  rgba(201,168,76,0.18);
    --gold:    #C9A84C;
    --gold-lt: #E8C97A;
    --cream:   #e8e4dc;
    --muted:   rgba(232,228,220,0.55);
    --radius:  4px;
  }
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--cream); font-family: "DM Sans", system-ui, sans-serif;
         min-height: 100vh; }
  /* Nav */
  nav { display: flex; align-items: center; gap: 0; border-bottom: 1px solid var(--border);
        padding: 0 32px; background: #0d0d0d; }
  .nav-brand { font-family: Georgia, serif; font-size: 18px; color: var(--gold);
               letter-spacing: .04em; padding: 18px 32px 18px 0;
               border-right: 1px solid var(--border); margin-right: 24px; }
  .nav-tab { padding: 20px 18px; font-size: 13px; letter-spacing: .08em; text-transform: uppercase;
             color: var(--muted); cursor: pointer; border-bottom: 2px solid transparent;
             transition: color .15s, border-color .15s; }
  .nav-tab:hover { color: var(--cream); }
  .nav-tab.active { color: var(--gold); border-bottom-color: var(--gold); }
  /* Layout */
  main { max-width: 1100px; margin: 0 auto; padding: 40px 24px; }
  .tab-panel { display: none; }
  .tab-panel.active { display: block; }
  /* Cards */
  .card { background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
          padding: 28px; margin-bottom: 20px; }
  .card h2 { font-family: Georgia, serif; font-size: 20px; color: var(--cream);
              font-weight: normal; margin-bottom: 18px; }
  .card h3 { font-size: 11px; letter-spacing: .16em; text-transform: uppercase;
              color: var(--gold); margin-bottom: 14px; }
  /* Form */
  label { display: block; font-size: 12px; letter-spacing: .1em; text-transform: uppercase;
          color: var(--gold); margin-bottom: 6px; margin-top: 16px; }
  label:first-child { margin-top: 0; }
  input[type=text], input[type=date], input[type=time], textarea, select {
    width: 100%; background: #0a0a0a; border: 1px solid var(--border);
    color: var(--cream); padding: 10px 14px; border-radius: var(--radius);
    font-size: 14px; font-family: inherit; resize: vertical;
  }
  input[type=text]:focus, input[type=date]:focus, input[type=time]:focus,
  textarea:focus, select:focus {
    outline: none; border-color: var(--gold);
  }
  input[type=date]::-webkit-calendar-picker-indicator,
  input[type=time]::-webkit-calendar-picker-indicator { filter: invert(0.6); }
  textarea { min-height: 80px; }
  .row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  /* Buttons */
  .btn { display: inline-block; padding: 11px 28px; border-radius: var(--radius);
         font-size: 13px; font-weight: 600; letter-spacing: .08em; text-transform: uppercase;
         cursor: pointer; border: none; transition: background .15s; }
  .btn-gold { background: var(--gold); color: #0a0a0a; }
  .btn-gold:hover { background: var(--gold-lt); }
  .btn-outline { background: transparent; color: var(--gold);
                 border: 1px solid rgba(201,168,76,.45); }
  .btn-outline:hover { border-color: var(--gold); }
  .btn-danger { background: transparent; color: #c0392b;
                border: 1px solid rgba(192,57,43,.4); font-size: 11px;
                padding: 5px 12px; }
  .btn-danger:hover { background: rgba(192,57,43,.1); }
  .btn:disabled { opacity: .4; cursor: not-allowed; }
  .btn-row { display: flex; gap: 12px; margin-top: 22px; flex-wrap: wrap; }
  /* Status */
  .badge { display: inline-block; padding: 3px 10px; border-radius: 12px;
           font-size: 11px; font-weight: 600; letter-spacing: .06em; }
  .badge-ok    { background: rgba(39,174,96,.15); color: #2ecc71; }
  .badge-warn  { background: rgba(243,156,18,.12); color: #f39c12; }
  .badge-err   { background: rgba(192,57,43,.12); color: #e74c3c; }
  .badge-run   { background: rgba(52,152,219,.12); color: #3498db; }
  /* Log */
  #run-log { background: #050505; border: 1px solid var(--border); border-radius: var(--radius);
              padding: 16px; font-family: monospace; font-size: 12px; color: var(--muted);
              max-height: 220px; overflow-y: auto; margin-top: 16px; white-space: pre-wrap; }
  /* File grid */
  .file-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px,1fr)); gap: 14px; }
  .file-item { background: #0d0d0d; border: 1px solid var(--border); border-radius: var(--radius);
               padding: 12px; text-align: center; }
  .file-item img { width: 100%; height: 120px; object-fit: cover;
                   border-radius: 2px; margin-bottom: 8px; }
  .file-item .fname { font-size: 11px; color: var(--muted); word-break: break-all; margin-bottom: 8px; }
  /* Preview */
  #card-preview img { max-width: 100%; border-radius: var(--radius); }
  /* Divider */
  hr { border: none; border-top: 1px solid var(--border); margin: 24px 0; }
  /* Upload zone */
  .upload-zone { border: 1px dashed rgba(201,168,76,.3); border-radius: var(--radius);
                 padding: 28px; text-align: center; color: var(--muted); font-size: 14px;
                 cursor: pointer; transition: border-color .15s; }
  .upload-zone:hover { border-color: var(--gold); }
  .upload-zone input { display: none; }
  /* Token bar */
  .token-bar { height: 6px; background: #1a1a1a; border-radius: 3px; margin-top: 8px; }
  .token-fill { height: 100%; border-radius: 3px; background: var(--gold); transition: width .4s; }
  /* Runs table */
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; font-size: 11px; letter-spacing: .12em; text-transform: uppercase;
       color: var(--gold); padding: 8px 12px; border-bottom: 1px solid var(--border); }
  td { padding: 10px 12px; border-bottom: 1px solid rgba(201,168,76,.07); color: var(--muted); }
  td:first-child { color: var(--cream); }
  .mode-pill { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 10px;
               font-weight: 600; letter-spacing: .06em; text-transform: uppercase; }
  .pill-photo    { background: rgba(52,152,219,.12); color: #3498db; }
  .pill-stat     { background: rgba(201,168,76,.12); color: var(--gold); }
  .pill-atm      { background: rgba(155,89,182,.12); color: #9b59b6; }
</style>
</head>
<body>

<nav>
  <div class="nav-brand">BarLedger</div>
  <div class="nav-tab active" onclick="showTab('compose')">Compose</div>
  <div class="nav-tab" onclick="showTab('preview')">Card Preview</div>
  <div class="nav-tab" onclick="showTab('inbox')">Inbox</div>
  <div class="nav-tab" onclick="showTab('backgrounds')">Backgrounds</div>
  <div class="nav-tab" onclick="showTab('history')">History</div>
  <div class="nav-tab" onclick="showTab('token')">Token</div>
  <div class="nav-tab" onclick="showTab('schedule')">Schedule</div>
</nav>

<main>

<!-- COMPOSE -->
<div id="tab-compose" class="tab-panel active">
  <div class="card">
    <h2>New Post</h2>

    <label>Post Mode</label>
    <select id="mode" onchange="onModeChange()">
      <option value="photo">Photo / Carousel (supply your own images)</option>
      <option value="card_stat">Branded Stat Card (flat dark, Claude generates content)</option>
      <option value="card_atm">Branded Atmospheric Card (background photo, Claude generates content)</option>
    </select>

    <div id="image-section">
      <label>Images (comma-separated filenames from your inbox)</label>
      <input type="text" id="images" placeholder="IMAGE1.JPG,IMAGE2.JPG">
      <label style="margin-top:12px;">
        <input type="checkbox" id="overlay" style="width:auto;margin-right:6px;">
        Draw branded text overlay on images
      </label>
    </div>

    <label>Direction / CTA</label>
    <textarea id="direction" placeholder="What this post is about and what you want people to do. E.g. show how barcode scanning cuts a full liquor count to minutes, push the free trial"></textarea>

    <div class="btn-row">
      <button class="btn btn-gold" onclick="triggerPost()">Post to Instagram</button>
      <button class="btn btn-outline" onclick="showTab('preview')">Preview Card First</button>
    </div>

    <div id="run-log" style="display:none;"></div>
  </div>
</div>

<!-- CARD PREVIEW -->
<div id="tab-preview" class="tab-panel">
  <div class="card">
    <h2>Card Preview</h2>
    <p style="color:var(--muted);font-size:13px;margin-bottom:20px;">
      Fill in the fields below to see exactly how your branded card will look before posting.
      These fields are what Claude generates automatically when you post, but you can type them
      manually here to preview any variation.
    </p>
    <div class="row">
      <div>
        <label>Style</label>
        <select id="prev-style">
          <option value="stat">Flat Dark (Stat)</option>
          <option value="atm">Atmospheric</option>
        </select>
        <label>Eyebrow (2-3 words)</label>
        <input type="text" id="prev-eyebrow" value="INVENTORY INSIGHT">
        <label>Headline (use \n for line break)</label>
        <input type="text" id="prev-headline" value="Stop Counting Bottles.\nStart Controlling Cost.">
        <label>Body (1-2 sentences)</label>
        <textarea id="prev-body">Most bar owners lose 18-22% of their pour cost to variance they never see coming. BarLedger surfaces it automatically.</textarea>
        <label>CTA</label>
        <input type="text" id="prev-cta" value="Your first 14 days are free. Link in bio.">
        <div class="btn-row">
          <button class="btn btn-gold" onclick="generatePreview()">Render Preview</button>
          <button class="btn btn-outline" id="gen-content-btn" onclick="generateContent()">Generate Content</button>
        </div>
        <div id="gen-content-status" style="font-size:13px;color:var(--muted);margin-top:10px;min-height:18px;"></div>
      </div>
      <div id="card-preview" style="display:flex;align-items:flex-start;justify-content:center;">
        <p style="color:var(--muted);font-size:13px;margin-top:60px;">Click Render Preview to see your card.</p>
      </div>
    </div>
  </div>
</div>

<!-- INBOX -->
<div id="tab-inbox" class="tab-panel">
  <div class="card">
    <h2>Inbox</h2>
    <p style="color:var(--muted);font-size:13px;margin-bottom:20px;">
      Images here are what you reference by filename when composing a photo post.
    </p>
    <div class="upload-zone" onclick="document.getElementById('inbox-upload').click()">
      Click to upload images to inbox
    </div>
    <input type="file" id="inbox-upload" accept="image/*" multiple style="display:none">
    <button class="btn btn-outline" style="margin-top:12px;" onclick="document.getElementById('inbox-upload').click()">Choose Files</button>
    <div id="inbox-upload-status" style="font-size:13px;color:var(--muted);margin-top:10px;min-height:18px;"></div>
    <div id="inbox-files" class="file-grid" style="margin-top:20px;"></div>
  </div>
</div>

<!-- BACKGROUNDS -->
<div id="tab-backgrounds" class="tab-panel">
  <div class="card">
    <h2>Background Photos</h2>
    <p style="color:var(--muted);font-size:13px;margin-bottom:20px;">
      These are used automatically as the atmospheric layer in card_atm posts.
      Dark, moody bar and restaurant photos work best.
    </p>
    <div class="upload-zone" onclick="document.getElementById('bg-upload').click()">
      Click to upload background photos
    </div>
    <input type="file" id="bg-upload" accept="image/*" multiple style="display:none">
    <button class="btn btn-outline" style="margin-top:12px;" onclick="document.getElementById('bg-upload').click()">Choose Files</button>
    <div id="bg-upload-status" style="font-size:13px;color:var(--muted);margin-top:10px;min-height:18px;"></div>
    <div id="bg-files" class="file-grid" style="margin-top:20px;"></div>
  </div>
</div>

<!-- HISTORY -->
<div id="tab-history" class="tab-panel">
  <div class="card">
    <h2>Recent Workflow Runs</h2>
    <div id="history-content">
      <p style="color:var(--muted);font-size:13px;">Loading...</p>
    </div>
  </div>
</div>

<!-- TOKEN -->
<div id="tab-token" class="tab-panel">
  <div class="card">
    <h2>Instagram Token Status</h2>
    <div id="token-content">
      <p style="color:var(--muted);font-size:13px;">Loading...</p>
    </div>
    <div class="btn-row" style="margin-top:20px;">
      <button class="btn btn-outline" onclick="refreshToken()">Refresh Token Now</button>
    </div>
    <div id="token-log" style="display:none;" class="run-log"></div>
  </div>
</div>

<!-- SCHEDULE -->
<div id="tab-schedule" class="tab-panel">
  <div class="card">
    <h2>Queue a Post</h2>

    <label>Post Mode</label>
    <select id="sch-mode" onchange="onSchModeChange()">
      <option value="card_atm">Atmospheric Card</option>
      <option value="card_stat">Stat Card</option>
      <option value="photo">Photo</option>
    </select>

    <div id="sch-image-section" style="display:none;">
      <label>Images</label>
      <input type="text" id="sch-images" placeholder="Comma-separated filenames from inbox, e.g. bar1.jpg,bar2.jpg">
    </div>

    <label>Direction / Context</label>
    <textarea id="sch-direction" placeholder="What this post is about and what you want people to do."></textarea>

    <hr>
    <div style="font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--gold);margin-bottom:4px;">Override Card Text</div>
    <p style="color:var(--muted);font-size:12px;margin-bottom:14px;">Leave blank to let Claude generate from the direction above.</p>

    <label>Headline</label>
    <input type="text" id="sch-headline" placeholder="Optional">
    <label>Body</label>
    <textarea id="sch-body" placeholder="Optional" style="min-height:60px;"></textarea>
    <label>CTA</label>
    <input type="text" id="sch-cta" placeholder="Optional">

    <hr>
    <div class="row">
      <div>
        <label>Date</label>
        <input type="date" id="sch-date">
      </div>
      <div>
        <label>Time</label>
        <input type="time" id="sch-time">
      </div>
    </div>

    <label>Timezone</label>
    <select id="sch-tz">
      <option value="America/Phoenix">America/Phoenix (MST, no DST)</option>
      <option value="America/Los_Angeles">America/Los_Angeles (PT)</option>
      <option value="America/Denver">America/Denver (MT)</option>
      <option value="America/Chicago">America/Chicago (CT)</option>
      <option value="America/New_York">America/New_York (ET)</option>
      <option value="UTC">UTC</option>
    </select>

    <div style="margin-top:20px;display:flex;align-items:center;gap:10px;">
      <input type="checkbox" id="sch-recurring" style="width:auto;accent-color:var(--gold);"
             onchange="onSchRecurringChange()">
      <span style="font-size:13px;color:var(--cream);">Recurring post</span>
    </div>

    <div id="sch-recurring-opts" style="display:none;margin-top:16px;padding:16px;
         border:1px solid var(--border);border-radius:var(--radius);">
      <label>Frequency</label>
      <select id="sch-freq">
        <option value="weekly">Weekly</option>
        <option value="daily">Daily</option>
      </select>
      <label style="margin-top:14px;">Days of Week</label>
      <div style="display:flex;gap:14px;flex-wrap:wrap;margin-top:10px;">
        <div style="display:flex;align-items:center;gap:6px;">
          <input type="checkbox" value="monday" class="sch-day" style="width:auto;accent-color:var(--gold);">
          <span style="font-size:13px;color:var(--cream);">Monday</span>
        </div>
        <div style="display:flex;align-items:center;gap:6px;">
          <input type="checkbox" value="tuesday" class="sch-day" style="width:auto;accent-color:var(--gold);">
          <span style="font-size:13px;color:var(--cream);">Tuesday</span>
        </div>
        <div style="display:flex;align-items:center;gap:6px;">
          <input type="checkbox" value="wednesday" class="sch-day" style="width:auto;accent-color:var(--gold);">
          <span style="font-size:13px;color:var(--cream);">Wednesday</span>
        </div>
        <div style="display:flex;align-items:center;gap:6px;">
          <input type="checkbox" value="thursday" class="sch-day" style="width:auto;accent-color:var(--gold);">
          <span style="font-size:13px;color:var(--cream);">Thursday</span>
        </div>
        <div style="display:flex;align-items:center;gap:6px;">
          <input type="checkbox" value="friday" class="sch-day" style="width:auto;accent-color:var(--gold);">
          <span style="font-size:13px;color:var(--cream);">Friday</span>
        </div>
        <div style="display:flex;align-items:center;gap:6px;">
          <input type="checkbox" value="saturday" class="sch-day" style="width:auto;accent-color:var(--gold);">
          <span style="font-size:13px;color:var(--cream);">Saturday</span>
        </div>
        <div style="display:flex;align-items:center;gap:6px;">
          <input type="checkbox" value="sunday" class="sch-day" style="width:auto;accent-color:var(--gold);">
          <span style="font-size:13px;color:var(--cream);">Sunday</span>
        </div>
      </div>
    </div>

    <div class="btn-row">
      <button class="btn btn-gold" onclick="queuePost()">Queue Post</button>
    </div>
    <div id="sch-status" style="font-size:13px;margin-top:12px;min-height:18px;"></div>
  </div>

  <div class="card">
    <h2>Upcoming Schedule</h2>
    <div id="sch-upcoming">
      <p style="color:var(--muted);font-size:13px;">Loading...</p>
    </div>
  </div>
</div>

</main>

<script>
// ---- Tab switching ----
function showTab(name) {
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  event.target.classList.add('active');
  if (name === 'inbox')       loadFiles('inbox');
  if (name === 'backgrounds') loadFiles('backgrounds');
  if (name === 'history')     loadHistory();
  if (name === 'token')       loadToken();
  if (name === 'schedule')  { loadSchedule(); onSchModeChange(); }
}

// ---- Mode toggle ----
function onModeChange() {
  const mode = document.getElementById('mode').value;
  document.getElementById('image-section').style.display =
    mode === 'photo' ? 'block' : 'none';
}

// ---- Compose / post ----
async function triggerPost() {
  const mode    = document.getElementById('mode').value;
  const images  = document.getElementById('images').value.trim();
  const overlay = document.getElementById('overlay').checked ? 'yes' : 'no';
  let direction = document.getElementById('direction').value.trim();

  if (!direction && mode !== 'photo') {
    const ph = document.getElementById('prev-headline').value.trim();
    const pb = document.getElementById('prev-body').value.trim();
    const pc = document.getElementById('prev-cta').value.trim();
    if (!ph && !pb) {
      alert('Enter a direction, or generate content in the Card Preview tab first.');
      return;
    }
    direction = [ph, pb, pc ? 'CTA: ' + pc : ''].filter(Boolean).join(' | ');
  }
  if (!direction) { alert('Please enter a direction / CTA.'); return; }
  if (mode === 'photo' && !images) { alert('Please enter at least one image filename.'); return; }

  const log = document.getElementById('run-log');
  log.style.display = 'block';
  log.textContent   = 'Triggering GitHub Action...\n';

  const resp = await fetch('/trigger', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ mode, images, direction, overlay })
  });
  const data = await resp.json();
  if (data.ok) {
    log.textContent += 'Action triggered. Watch your Telegram for the approval message.\n';
    log.textContent += 'Check the History tab or GitHub Actions for live run status.\n';
  } else {
    log.textContent += 'Error: ' + data.error + '\n';
  }
}

// ---- Card preview ----
async function generateContent() {
  const btn    = document.getElementById('gen-content-btn');
  const status = document.getElementById('gen-content-status');
  btn.disabled    = true;
  btn.textContent = 'Generating...';
  status.style.color = 'var(--muted)';
  status.textContent = 'Calling Claude...';

  const style = document.getElementById('prev-style').value;
  try {
    const resp = await fetch('/generate-content', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ style }),
    });
    const data = await resp.json();
    if (!data.ok) {
      status.style.color = '#e74c3c';
      status.textContent = 'Error: ' + data.error;
      return;
    }
    if (data.eyebrow)  document.getElementById('prev-eyebrow').value  = data.eyebrow;
    if (data.headline) document.getElementById('prev-headline').value = data.headline;
    if (data.body)     document.getElementById('prev-body').value     = data.body;
    if (data.cta)      document.getElementById('prev-cta').value      = data.cta;
    status.style.color = '#2ecc71';
    status.textContent = 'Content generated.';
    generatePreview();
  } catch (err) {
    status.style.color = '#e74c3c';
    status.textContent = 'Unexpected error: ' + err.message;
  } finally {
    btn.disabled    = false;
    btn.textContent = 'Generate Content';
  }
}

const CTA_URL = 'https://xlforhospitality.com/barledger/?utm_source=ig&utm_medium=social&utm_content=link_in_bio';

async function generatePreview() {
  const box = document.getElementById('card-preview');
  box.innerHTML = '<p style="color:var(--muted);font-size:13px;">Rendering...</p>';
  const cta    = document.getElementById('prev-cta').value;
  const params = new URLSearchParams({
    eyebrow:  document.getElementById('prev-eyebrow').value,
    headline: document.getElementById('prev-headline').value,
    body:     document.getElementById('prev-body').value,
    cta:      cta,
    style:    document.getElementById('prev-style').value,
    t:        Date.now(),
  });
  box.innerHTML = `
    <div style="width:100%;">
      <img src="/preview?${params}" style="max-width:100%;border-radius:4px;display:block;">
      <div style="margin-top:14px;text-align:center;">
        <a href="${CTA_URL}" target="_blank" rel="noopener"
           style="display:inline-block;background:var(--gold);color:#0a0a0a;
                  padding:12px 32px;border-radius:4px;font-size:12px;font-weight:700;
                  letter-spacing:.1em;text-transform:uppercase;text-decoration:none;
                  transition:background .15s;"
           onmouseover="this.style.background='var(--gold-lt)'"
           onmouseout="this.style.background='var(--gold)'">
          ${cta || 'Link in Bio'}
        </a>
      </div>
    </div>`;
}

// ---- File management ----
async function loadFiles(folder) {
  const containerId = folder === 'inbox' ? 'inbox-files' : 'bg-files';
  const container   = document.getElementById(containerId);
  container.innerHTML = '<p style="color:var(--muted);font-size:13px;">Loading...</p>';
  const resp  = await fetch(`/files/${folder}`);
  const files = await resp.json();
  if (!files.length) {
    container.innerHTML = '<p style="color:var(--muted);font-size:13px;">No files yet.</p>';
    return;
  }
  container.innerHTML = files.map(f => `
    <div class="file-item">
      <img src="${f.download_url}" onerror="this.style.display='none'">
      <div class="fname">${f.name}</div>
      <button class="btn btn-danger" onclick="deleteFile('${folder}','${f.name}',this)">Delete</button>
    </div>
  `).join('');
}

async function uploadFiles(folder, input) {
  const statusEl = document.getElementById(
    folder === 'inbox' ? 'inbox-upload-status' : 'bg-upload-status'
  );
  try {
    const files = Array.from(input.files);
    if (!files.length) return;
    statusEl.style.color = 'var(--muted)';
    statusEl.textContent = 'Uploading ' + files.length + ' file(s)...';
    for (const file of files) {
      const b64 = await toBase64(file);
      const resp = await fetch('/upload', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ folder, filename: file.name, data: b64 })
      });
      const data = await resp.json();
      if (!data.ok) {
        statusEl.style.color = '#e74c3c';
        statusEl.textContent = 'Error uploading ' + file.name + ': ' + data.error;
        return;
      }
    }
    statusEl.style.color = '#2ecc71';
    statusEl.textContent = 'Upload complete.';
    input.value = '';
    loadFiles(folder);
  } catch (err) {
    statusEl.style.color = '#e74c3c';
    statusEl.textContent = 'Unexpected error: ' + err.message;
  }
}

async function deleteFile(folder, filename, btn) {
  if (!confirm(`Delete ${filename}?`)) return;
  btn.disabled = true;
  await fetch('/delete', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ folder, filename })
  });
  loadFiles(folder);
}

function toBase64(file) {
  return new Promise((res, rej) => {
    const r = new FileReader();
    r.onload  = () => res(r.result.split(',')[1]);
    r.onerror = rej;
    r.readAsDataURL(file);
  });
}

// ---- History ----
async function loadHistory() {
  const box  = document.getElementById('history-content');
  const resp = await fetch('/runs');
  const runs = await resp.json();
  if (!runs.length) {
    box.innerHTML = '<p style="color:var(--muted);font-size:13px;">No runs yet.</p>';
    return;
  }
  const rows = runs.map(r => {
    const status = r.conclusion === 'success'  ? '<span class="badge badge-ok">success</span>'
                 : r.conclusion === 'failure'   ? '<span class="badge badge-err">failed</span>'
                 : r.status     === 'in_progress'? '<span class="badge badge-run">running</span>'
                 :                                 '<span class="badge badge-warn">'+r.status+'</span>';
    const dt = new Date(r.created_at).toLocaleString();
    return `<tr>
      <td>${r.name || r.workflow_id}</td>
      <td>${status}</td>
      <td>${dt}</td>
      <td><a href="${r.html_url}" target="_blank" style="color:var(--gold);font-size:12px;">View</a></td>
    </tr>`;
  }).join('');
  box.innerHTML = `<table>
    <tr><th>Workflow</th><th>Status</th><th>Started</th><th>Link</th></tr>
    ${rows}
  </table>`;
}

// ---- Token ----
async function loadToken() {
  const box  = document.getElementById('token-content');
  const resp = await fetch('/token-status');
  const data = await resp.json();
  const days = data.days_remaining;
  const pct  = Math.min(100, Math.round((days / 60) * 100));
  const cls  = days > 20 ? 'badge-ok' : days > 7 ? 'badge-warn' : 'badge-err';
  box.innerHTML = `
    <p style="color:var(--muted);font-size:13px;margin-bottom:12px;">
      Token last refreshed: <strong style="color:var(--cream)">${data.last_refreshed}</strong>
    </p>
    <span class="badge ${cls}">${days} days remaining</span>
    <div class="token-bar" style="margin-top:12px;">
      <div class="token-fill" style="width:${pct}%"></div>
    </div>
    <p style="color:var(--muted);font-size:12px;margin-top:8px;">
      The weekly refresh workflow keeps this renewed automatically.
    </p>`;
}

async function refreshToken() {
  const log = document.getElementById('token-log');
  log.style.display = 'block';
  log.textContent   = 'Triggering token refresh workflow...';
  const resp = await fetch('/refresh-token', { method: 'POST' });
  const data = await resp.json();
  log.textContent += data.ok ? '\nDone. Reload this tab in a moment.' : '\nError: ' + data.error;
}

// ---- Schedule ----
function onSchModeChange() {
  const mode = document.getElementById('sch-mode').value;
  document.getElementById('sch-image-section').style.display =
    mode === 'photo' ? 'block' : 'none';

  if (mode === 'photo') {
    // Pull images + direction from Compose tab
    const img = document.getElementById('images').value.trim();
    const dir = document.getElementById('direction').value.trim();
    if (img) document.getElementById('sch-images').value    = img;
    if (dir) document.getElementById('sch-direction').value = dir;
  } else {
    // Pull override text from Card Preview only if styles match
    const previewStyle = document.getElementById('prev-style').value;
    const matches = (mode === 'card_atm' && previewStyle === 'atm') ||
                    (mode === 'card_stat' && previewStyle === 'stat');
    if (matches) {
      const ph = document.getElementById('prev-headline').value.trim();
      const pb = document.getElementById('prev-body').value.trim();
      const pc = document.getElementById('prev-cta').value.trim();
      document.getElementById('sch-headline').value  = ph;
      document.getElementById('sch-body').value      = pb;
      document.getElementById('sch-cta').value       = pc;
      document.getElementById('sch-direction').value = '';
    }
  }
}

function onSchRecurringChange() {
  const on = document.getElementById('sch-recurring').checked;
  document.getElementById('sch-recurring-opts').style.display = on ? 'block' : 'none';
}

async function queuePost() {
  const status = document.getElementById('sch-status');
  const date   = document.getElementById('sch-date').value;
  const time   = document.getElementById('sch-time').value;
  if (!date || !time) {
    status.style.color = '#e74c3c';
    status.textContent = 'Please select a date and time.';
    return;
  }
  const mode = document.getElementById('sch-mode').value;
  let direction        = document.getElementById('sch-direction').value.trim();
  let overrideHeadline = document.getElementById('sch-headline').value.trim();
  let overrideBody     = document.getElementById('sch-body').value.trim();
  let overrideCta      = document.getElementById('sch-cta').value.trim();

  if (!direction && !overrideHeadline && !overrideBody) {
    status.style.color = '#e74c3c';
    status.textContent = mode === 'photo'
      ? 'Please enter a direction.'
      : 'Select a post mode to auto-fill content, or enter a direction.';
    return;
  }

  const recurringEnabled = document.getElementById('sch-recurring').checked;
  const recurringDays    = recurringEnabled
    ? Array.from(document.querySelectorAll('.sch-day:checked')).map(cb => cb.value)
    : [];
  const imagesRaw = document.getElementById('sch-images').value;
  const payload = {
    mode,
    direction,
    images:              imagesRaw ? imagesRaw.split(',').map(s => s.trim()).filter(Boolean) : [],
    override_headline:   overrideHeadline,
    override_body:       overrideBody,
    override_cta:        overrideCta,
    scheduled_time:      `${date}T${time}:00`,
    timezone:            document.getElementById('sch-tz').value,
    recurring_enabled:   recurringEnabled,
    recurring_frequency: document.getElementById('sch-freq').value,
    recurring_days:      recurringDays,
  };
  status.style.color = 'var(--muted)';
  status.textContent = 'Saving to GitHub...';
  const resp = await fetch('/schedule/add', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
  const data = await resp.json();
  if (data.ok) {
    status.style.color = '#2ecc71';
    status.textContent = 'Post queued successfully.';
    loadSchedule();
  } else {
    status.style.color = '#e74c3c';
    status.textContent = 'Error: ' + data.error;
  }
}

async function loadSchedule() {
  const box  = document.getElementById('sch-upcoming');
  box.innerHTML = '<p style="color:var(--muted);font-size:13px;">Loading...</p>';
  const resp = await fetch('/schedule');
  const data = await resp.json();
  const posts = (data.posts || []).filter(p => p.status !== 'cancelled');
  if (!posts.length) {
    box.innerHTML = '<p style="color:var(--muted);font-size:13px;">No scheduled posts.</p>';
    return;
  }
  const modeLabel = {
    card_atm: 'Atmospheric', card_stat: 'Stat Card',
    photo: 'Photo', atmospheric: 'Atmospheric', stat: 'Stat Card'
  };
  const rows = posts.map(p => {
    const dt  = p.scheduled_time ? p.scheduled_time.replace('T', ' ') : '-';
    const tz  = p.timezone || '-';
    const mode = modeLabel[p.mode] || p.mode;
    const rec  = (p.recurring && p.recurring.enabled)
      ? (p.recurring.frequency === 'daily'
          ? 'Daily'
          : 'Weekly' + (p.recurring.days && p.recurring.days.length
              ? ': ' + p.recurring.days.join(', ')
              : ''))
      : 'One-time';
    const badgeCls = p.status === 'pending' ? 'badge-ok'
                   : p.status === 'error'   ? 'badge-err'
                   : p.status === 'posted'  ? 'badge-run'
                   :                          'badge-warn';
    const canCancel = p.status === 'pending';
    return `<tr>
      <td>${dt}<br><span style="font-size:11px;color:var(--muted);">${tz}</span></td>
      <td>${mode}</td>
      <td>${rec}</td>
      <td><span class="badge ${badgeCls}">${p.status}</span></td>
      <td>${canCancel ? `<button class="btn btn-danger" onclick="cancelPost('${p.id}',this)">Cancel</button>` : ''}</td>
    </tr>`;
  }).join('');
  box.innerHTML = `<table>
    <tr><th>Scheduled</th><th>Mode</th><th>Recurring</th><th>Status</th><th></th></tr>
    ${rows}
  </table>`;
}

async function cancelPost(id, btn) {
  if (!confirm('Cancel this scheduled post?')) return;
  btn.disabled = true;
  const resp = await fetch('/schedule/cancel', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id}),
  });
  const data = await resp.json();
  if (data.ok) {
    loadSchedule();
  } else {
    alert('Error: ' + data.error);
    btn.disabled = false;
  }
}

// Init
onModeChange();
document.getElementById('inbox-upload').addEventListener('change', function(){ uploadFiles('inbox', this); });
document.getElementById('bg-upload').addEventListener('change', function(){ uploadFiles('backgrounds', this); });
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/trigger", methods=["POST"])
def trigger():
    d = request.json
    mode      = d.get("mode","photo")
    images    = d.get("images","")
    direction = d.get("direction","")
    overlay   = d.get("overlay","no")
    try:
        gh_trigger_workflow({
            "mode":    mode,
            "image":   images,
            "topic":   direction,
            "overlay": overlay,
        })
        post_log.append({
            "ts": datetime.now().isoformat(),
            "mode": mode, "direction": direction,
        })
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.route("/preview")
def preview():
    import io
    eyebrow  = request.args.get("eyebrow","INVENTORY INSIGHT")
    headline = request.args.get("headline","Your Headline Here").replace("\\n", "\n")
    body     = request.args.get("body","Your body copy goes here.")
    cta      = request.args.get("cta","Get started. Link in bio.")
    style    = request.args.get("style","stat")
    buf = render_preview(eyebrow, headline, body, cta, style)
    return send_file(buf, mimetype="image/png")


@app.route("/generate-content", methods=["POST"])
def generate_content_route():
    import anthropic as _anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return jsonify(ok=False,
                       error="ANTHROPIC_API_KEY not set — add it to dashboard.env and restart.")
    style = request.json.get("style", "stat")
    style_note = (
        "This is a STAT card. Focus on a specific metric, operational insight, or hidden cost "
        "that bar and restaurant owners miss. Use a concrete number or data point if natural."
        if style == "stat" else
        "This is an ATMOSPHERIC card. Evoke the feeling of running a tight, profitable operation. "
        "Focus on transformation, control, and the premium result of using BarLedger."
    )
    prompt = f"""You are writing Instagram card copy for BarLedger, a premium SaaS platform \
for bar and restaurant inventory management and pour-cost control.

Brand voice: direct, confident, luxury hospitality. Speaks to operators who care about margins. \
No fluff, no cliches, no em dashes.

{style_note}

Return ONLY a valid JSON object with exactly these four keys:
  eyebrow  - 2-3 words, all caps, acts as a short label (e.g. "POUR COST" or "INVENTORY INSIGHT")
  headline - 6-12 words, punchy. Use \\n to break across 2 lines for dramatic effect.
  body     - 1-2 sentences, factual and specific. No em dashes.
  cta      - short call to action, 6-10 words (e.g. "Your first 14 days are free. Link in bio.")

Return only the JSON object. No explanation, no code fences."""
    try:
        client = _anthropic.Anthropic(api_key=api_key)
        msg    = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        fields = json.loads(text)
        return jsonify(ok=True,
                       eyebrow=fields.get("eyebrow",""),
                       headline=fields.get("headline",""),
                       body=fields.get("body",""),
                       cta=fields.get("cta",""))
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.route("/files/<folder>")
def list_files(folder):
    if folder not in ("inbox","backgrounds"):
        return jsonify([])
    return jsonify(gh_list_folder(folder))


@app.route("/upload", methods=["POST"])
def upload():
    d        = request.json
    folder   = d["folder"]
    filename = d["filename"]
    data     = base64.b64decode(d["data"])
    try:
        gh_upload_file(folder, filename, data)
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.route("/delete", methods=["POST"])
def delete():
    d        = request.json
    folder   = d["folder"]
    filename = d["filename"]
    try:
        gh_delete_file(folder, filename)
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.route("/runs")
def runs():
    return jsonify(gh_get_runs(15))


@app.route("/token-status")
def token_status():
    updated = gh_get_secret_updated("IG_ACCESS_TOKEN")
    try:
        from datetime import timezone
        dt    = datetime.fromisoformat(updated.replace("Z","+00:00"))
        now   = datetime.now(timezone.utc)
        days  = 60 - (now - dt).days
        days  = max(0, days)
        human = dt.strftime("%b %d, %Y")
    except Exception:
        days, human = 60, updated
    return jsonify(days_remaining=days, last_refreshed=human)


@app.route("/refresh-token", methods=["POST"])
def trigger_refresh():
    try:
        req.post(
            f"{GH_API}/repos/{GH_REPO}/actions/workflows/refresh.yml/dispatches",
            headers=GH_HEADS,
            json={"ref": GH_BRANCH},
            timeout=15,
        ).raise_for_status()
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.route("/schedule")
def get_schedule():
    data, _ = gh_read_json("schedule.json")
    if data is None:
        data = {"posts": []}
    return jsonify(data)


@app.route("/schedule/add", methods=["POST"])
def add_schedule_entry():
    d    = request.json
    data, sha = gh_read_json("schedule.json")
    if data is None:
        data, sha = {"posts": []}, None
    new_post = {
        "id":             str(uuid.uuid4()),
        "mode":           d.get("mode", "card_atm"),
        "scheduled_time": d.get("scheduled_time", ""),
        "timezone":       d.get("timezone", "America/Phoenix"),
        "status":         "pending",
        "inputs": {
            "direction":        d.get("direction", ""),
            "images":           d.get("images", []),
            "override_headline": d.get("override_headline", ""),
            "override_body":     d.get("override_body", ""),
            "override_cta":      d.get("override_cta", ""),
        },
        "recurring": {
            "enabled":   d.get("recurring_enabled", False),
            "frequency": d.get("recurring_frequency", "weekly"),
            "days":      d.get("recurring_days", []),
        },
    }
    data["posts"].append(new_post)
    try:
        gh_write_json("schedule.json", data, sha,
                      f"schedule: add post {new_post['id'][:8]}")
        return jsonify(ok=True, id=new_post["id"])
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.route("/schedule/cancel", methods=["POST"])
def cancel_schedule_entry():
    d       = request.json
    post_id = d.get("id", "")
    data, sha = gh_read_json("schedule.json")
    if data is None:
        return jsonify(ok=False, error="schedule.json not found")
    for post in data["posts"]:
        if post["id"] == post_id:
            post["status"] = "cancelled"
            break
    try:
        gh_write_json("schedule.json", data, sha,
                      f"schedule: cancel post {post_id[:8]}")
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("\n  BarLedger Instagram Dashboard")
    print("  Open http://localhost:5000 in your browser\n")
    app.run(host="127.0.0.1", port=5000, debug=False)
