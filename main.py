import base64
import os
import random
import re
import string
import threading
import time
import unicodedata
import functools
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse, quote

import requests
from flask import (
    Flask, render_template, request, jsonify, redirect, url_for,
    session, Response,
)

from utils.db import get_db
from utils.text import display_title

# ─── Configuration ──────────────────────────────────────────────────────────
# Public domain used in every generated link. ONLY line to edit if this
# service's Render domain ever changes.
PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "pw-universal-live-player.koyeb.com"
)
# "https://" scheme zaroori hai warna generated link (jaise
# "domain.com/CODE/https://...") ek valid absolute URL nahi banta —
# browser address bar isko URL na maan ke seedha Google search bhej deta
# hai (khaas kar jab link me lambi/complex query string ho). Agar env var
# me pehle se scheme diya ho to usko chhedte nahi.
if not PUBLIC_BASE_URL.startswith(("http://", "https://")):
    PUBLIC_BASE_URL = "https://" + PUBLIC_BASE_URL

# ─── Server-side Admin Auth (keys never reach the browser) ────────────────
OWNER_NAME = os.environ.get("OWNER_NAME", "ViPvxMS10BRO")
ADMIN_KEYS = ["MS#Admin_R4!xQ8Lp7", "Core$MS_N6v!T2Zk9", "mS@Root_P8#Lm5Qx3"]
VIP_KEYS = ["ToXic#ViPR8m!4QxL7", "tOxic@VipN5v!9ZpK2", "ToXic$ViPX7#rT3Lm8"]

# ─── Flask app ──────────────────────────────────────────────────────────────
flask_app = Flask(__name__)
flask_app.secret_key = os.environ.get(
    "SECRET_KEY",
    "c7c8d55d9d8b4a3c2f71b1f5f79c8ea84e8d2c7c3a4b51d70b91ef0fdad5f2f6f13e9a7b8c6d1e24f4a8e9c0b5d3a7f6d8e2c1b9a4f7d5e8c3a6b1d0f9e2c7",
)
flask_app.config["SESSION_COOKIE_HTTPONLY"] = True
flask_app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
flask_app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)

db = get_db()
lectures_col = db["lectures"]


# ═══════════════════════════════════════════════════════════════════════════
#  LIVE UNIQUE-CODE STORE (backend memory — NOT MongoDB)
#  Purpose: pehle har playlist/segment request pe MongoDB se original_url
#  lookup hota tha (name ke through) — live m3u8 har 2-6 sec me refresh hota
#  hai, isliye ye baar-baar ka DB read hi buffering/atakne ka asli reason
#  tha. Ab generate ke time hi ek unique code बनता hai jiske against
#  original_url is process ki memory me rakha jaata hai — streaming ke
#  waqt sirf yahi dict check hota hai (O(1), zero DB load). MongoDB me bhi
#  save hota hai (sirf ek baar, generate ke waqt) taaki restart/redeploy ke
#  baad bhi record/backup maujood rahe — lekin streaming path isse kabhi
#  nahi padhta.
# ═══════════════════════════════════════════════════════════════════════════
LIVE_CODE_TTL = timedelta(hours=5)
LIVE_CODES = {}  # code -> {"name": str, "title": str, "expires_at": datetime}
LIVE_CODES_LOCK = threading.Lock()

# Fixed routes jinse code kabhi match na ho (case-sensitive hai isliye
# asal me clash nahi hota — ye sirf extra safety hai).
_RESERVED_CODES = {"API", "LOGIN", "LOGOUT", "HEALTH", "GENERATED", "RECORDINGS", "STATIC", "FAVICON.ICO"}


def _prune_expired_codes():
    now = datetime.utcnow()
    dead = [c for c, e in LIVE_CODES.items() if e["expires_at"] <= now]
    for c in dead:
        LIVE_CODES.pop(c, None)


def generate_unique_code(title_seed: str) -> str:
    """Title ke sirf English letters+numbers se ek prefix, + random
    letters/numbers suffix — final code hamesha unique hota hai."""
    seed = re.sub(r"[^A-Za-z0-9]", "", title_seed or "").upper()
    prefix = seed[:4]
    alphabet = string.ascii_uppercase + string.digits
    with LIVE_CODES_LOCK:
        _prune_expired_codes()
        while True:
            suffix_len = 6 if prefix else 8
            suffix = "".join(random.choices(alphabet, k=suffix_len))
            code = (prefix + suffix)[:12]
            if code not in LIVE_CODES and code not in _RESERVED_CODES:
                return code


def _save_live_code(code: str, name: str, title: str, expires_at: datetime):
    with LIVE_CODES_LOCK:
        LIVE_CODES[code] = {"name": name, "title": title, "expires_at": expires_at}


def _get_live_code(code: str):
    """Pehle memory check karo (hot path, zero DB load). Miss ho (jaise
    server restart ke turant baad) to hi ek baar MongoDB se refill karo."""
    with LIVE_CODES_LOCK:
        entry = LIVE_CODES.get(code)
        if entry:
            if entry["expires_at"] <= datetime.utcnow():
                LIVE_CODES.pop(code, None)
                return None
            return entry

    doc = lectures_col.find_one({"live_code": code}, {"_id": 1, "title": 1, "live_code_expires_at": 1})
    if not doc:
        return None
    expires_at = doc.get("live_code_expires_at")
    if not expires_at or expires_at <= datetime.utcnow():
        return None
    entry = {"name": doc["_id"], "title": doc.get("title") or display_title(doc["_id"]), "expires_at": expires_at}
    with LIVE_CODES_LOCK:
        LIVE_CODES[code] = entry
    return entry


# ═══════════════════════════════════════════════════════════════════════════
#  HLS PROXY (stream.js logic, ported to Python)
#  - Full CORS on EVERY response (success + error + preflight)
#  - Case-insensitive m3u8 content-type detection
#  - CloudFront signed-URL auth params inherited onto segments
#  - Original URL NEVER reaches the browser (base64 opaque tokens)
# ═══════════════════════════════════════════════════════════════════════════

UPSTREAM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.pw.live/",
    "Origin": "https://www.pw.live",
    # sec-ch-ua / client-hints — kuch CDN edge nodes bina in headers ke bhi
    # requests ko "non-browser" maan ke drop/slow kar dete hain.
    "sec-ch-ua": '"Chromium";v="126", "Not_A Brand";v="8"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

AUTH_PARAMS = {"signature", "policy", "key-pair-id", "expires", "start", "session-id"}
UPSTREAM_TIMEOUT = 15
UPSTREAM_MAX_RETRIES = 2  # transient CDN edge hiccups ke liye


@flask_app.after_request
def add_cors_headers(resp):
    """CORS on every response — success ho ya error. Iframe/embed
    bhi kahi se bhi allowed hai (X-Frame-Options set nahi karte, aur
    CSP explicitly frame-ancestors * — koi bhi site ise apne inline
    player me embed kar sake, block nahi karna)."""
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Access-Control-Expose-Headers"] = "*"
    resp.headers["Access-Control-Max-Age"] = "86400"
    resp.headers["Content-Security-Policy"] = "frame-ancestors *;"
    return resp


def _b64e(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _b64d(s: str) -> str:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad).decode()


def _inherit_auth_params(seg_url: str, playlist_url: str) -> str:
    """Signed CloudFront playlist ke auth params same-host segments pe copy karo."""
    try:
        seg = urlparse(seg_url)
        pl = urlparse(playlist_url)
        if seg.netloc != pl.netloc:
            return seg_url
        seg_q = dict(parse_qsl(seg.query, keep_blank_values=True))
        seg_lower = {k.lower() for k in seg_q}
        for k, v in parse_qsl(pl.query, keep_blank_values=True):
            if k.lower() in AUTH_PARAMS and k.lower() not in seg_lower:
                seg_q[k] = v
        return urlunparse(seg._replace(query=urlencode(seg_q)))
    except Exception:
        return seg_url


def _rewrite_m3u8(body: str, playlist_url: str, seg_base: str) -> str:
    """Playlist ke saare URLs ko proxy tokens se replace karo.
    seg_base: poora base URL jahan tokenized segments serve honge
    (e.g. https://host/api/live/<name>/seg ya https://host/api/livecode/<code>/seg)."""

    def tok(raw: str) -> str:
        absolute = urljoin(playlist_url, raw.strip())
        absolute = _inherit_auth_params(absolute, playlist_url)
        return f"{seg_base}?u={_b64e(absolute)}"

    out_lines = []
    for line in body.splitlines():
        t = line.strip()
        if not t:
            out_lines.append(line)
            continue
        if t.startswith("#"):
            if "URI=" in t:
                line = re.sub(
                    r'URI="([^"]+)"',
                    lambda m: f'URI="{tok(m.group(1))}"',
                    line,
                    flags=re.IGNORECASE,
                )
            out_lines.append(line)
            continue
        out_lines.append(tok(t))
    return "\n".join(out_lines) + "\n"


def _fetch_upstream(url: str):
    """
    Upstream fetch with retry + backoff — ported from the reference
    stream.js proxy logic:
      - 2xx aur 4xx dono FINAL maane jaate hain (4xx retry karne se theek
        nahi hoga — e.g. expired signed URL — retry sirf time waste karta
        hai aur player ko zyada der "loading" pe atka deta hai).
      - Sirf 5xx / connection-level errors (timeout, DNS, reset — transient
        CDN edge hiccups) retry hote hain, chhoti backoff ke saath.
    Pehle sirf EK attempt tha (koi retry nahi) — isliye ek chhota transient
    upstream glitch turant hi player ko fatal error de deta tha, jo live
    stream ke case me bahut common hai. Ye hi "live nahi chal raha" ke
    symptoms ka ek bada part tha.
    """
    headers = dict(UPSTREAM_HEADERS)
    if request.headers.get("Range"):
        headers["Range"] = request.headers["Range"]

    last_exc = None
    for attempt in range(UPSTREAM_MAX_RETRIES + 1):
        try:
            r = requests.get(
                url, headers=headers, timeout=UPSTREAM_TIMEOUT, allow_redirects=True
            )
            if r.ok or (400 <= r.status_code < 500):
                return r  # final — 2xx ya 4xx, retry se koi fayda nahi
            last_exc = requests.RequestException(f"Upstream {r.status_code}")
        except requests.RequestException as e:
            last_exc = e
        if attempt < UPSTREAM_MAX_RETRIES:
            time.sleep(0.3 * (attempt + 1))
    raise last_exc


@flask_app.route("/api/live/<name>/playlist")
def live_playlist(name):
    """Master/media playlist — original URL DB se aati hai, browser kabhi nahi dekhta."""
    doc = lectures_col.find_one({"_id": name})
    if not doc:
        return jsonify({"error": "Stream not found"}), 404
    try:
        r = _fetch_upstream(doc["original_url"])
    except requests.RequestException as e:
        return jsonify({"error": f"Upstream error: {e}"}), 502
    if not r.ok:
        return jsonify({"error": f"Upstream failed: {r.status_code}"}), r.status_code

    seg_base = f"{request.host_url.rstrip('/')}/api/live/{quote(name)}/seg"
    body = _rewrite_m3u8(r.text, doc["original_url"], seg_base)
    return Response(
        body,
        200,
        content_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@flask_app.route("/api/live/<name>/seg")
def live_segment(name):
    """Binary segments / nested playlists — opaque base64 token se fetch."""
    token = request.args.get("u")
    if not token:
        return jsonify({"error": "Missing segment token"}), 400
    try:
        url = _b64d(token)
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("bad scheme")
    except Exception:
        return jsonify({"error": "Invalid segment token"}), 400

    try:
        r = _fetch_upstream(url)
    except requests.RequestException as e:
        return jsonify({"error": f"Upstream error: {e}"}), 502
    if not r.ok:
        return jsonify({"error": f"Upstream failed: {r.status_code}"}), r.status_code

    ctype = (r.headers.get("Content-Type") or "").lower()
    if "mpegurl" in ctype or "m3u8" in ctype or parsed.path.lower().endswith(".m3u8"):
        # nested playlist — usko bhi rewrite karo
        # (pehle yahan ek MongoDB query thi jiska result kabhi use hi nahi
        # hota tha — pure wasted DB read har nested-playlist segment pe.
        # Hata diya, behavior bilkul same hai.)
        seg_base = f"{request.host_url.rstrip('/')}/api/live/{quote(name)}/seg"
        body = _rewrite_m3u8(r.text, url, seg_base)
        return Response(body, 200, content_type="application/vnd.apple.mpegurl")

    headers = {
        "Cache-Control": "public, max-age=30",
        "Accept-Ranges": "bytes",
    }
    if r.headers.get("Content-Range"):
        headers["Content-Range"] = r.headers["Content-Range"]
    return Response(
        r.content,
        206 if r.status_code == 206 else 200,
        content_type=r.headers.get("Content-Type") or "video/mp2t",
        headers=headers,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  HLS PROXY — unique-code variant (zero MongoDB reads during streaming)
#  original_url yahan seedha public link se aati hai (jaisa /<code>/<url>
#  route ne resolve kiya) — sirf "code" valid+not-expired hai ya nahi, wo
#  LIVE_CODES (in-memory) se check hota hai. Playlist/segment fetch logic
#  purane /api/live/ routes jaisi hi hai.
# ═══════════════════════════════════════════════════════════════════════════

@flask_app.route("/api/livecode/<code>/playlist")
def live_playlist_code(code):
    entry = _get_live_code(code)
    if not entry:
        return jsonify({"error": "Link expire ho gaya ya invalid hai"}), 404

    original_url = request.args.get("u", "")
    if not original_url.startswith(("http://", "https://")):
        return jsonify({"error": "Invalid stream URL"}), 400

    try:
        r = _fetch_upstream(original_url)
    except requests.RequestException as e:
        return jsonify({"error": f"Upstream error: {e}"}), 502
    if not r.ok:
        return jsonify({"error": f"Upstream failed: {r.status_code}"}), r.status_code

    seg_base = f"{request.host_url.rstrip('/')}/api/livecode/{quote(code)}/seg"
    body = _rewrite_m3u8(r.text, original_url, seg_base)
    return Response(
        body,
        200,
        content_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@flask_app.route("/api/livecode/<code>/seg")
def live_segment_code(code):
    entry = _get_live_code(code)
    if not entry:
        return jsonify({"error": "Link expire ho gaya ya invalid hai"}), 404

    token = request.args.get("u")
    if not token:
        return jsonify({"error": "Missing segment token"}), 400
    try:
        url = _b64d(token)
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("bad scheme")
    except Exception:
        return jsonify({"error": "Invalid segment token"}), 400

    try:
        r = _fetch_upstream(url)
    except requests.RequestException as e:
        return jsonify({"error": f"Upstream error: {e}"}), 502
    if not r.ok:
        return jsonify({"error": f"Upstream failed: {r.status_code}"}), r.status_code

    ctype = (r.headers.get("Content-Type") or "").lower()
    if "mpegurl" in ctype or "m3u8" in ctype or parsed.path.lower().endswith(".m3u8"):
        seg_base = f"{request.host_url.rstrip('/')}/api/livecode/{quote(code)}/seg"
        body = _rewrite_m3u8(r.text, url, seg_base)
        return Response(body, 200, content_type="application/vnd.apple.mpegurl")

    headers = {
        "Cache-Control": "public, max-age=30",
        "Accept-Ranges": "bytes",
    }
    if r.headers.get("Content-Range"):
        headers["Content-Range"] = r.headers["Content-Range"]
    return Response(
        r.content,
        206 if r.status_code == 206 else 200,
        content_type=r.headers.get("Content-Type") or "video/mp2t",
        headers=headers,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  AUTH + ADMIN (Luctyebro jaisa strict login portal — as it is)
# ═══════════════════════════════════════════════════════════════════════════

def admin_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "Login required"}), 401
            return redirect(url_for("index"))
        return view(*args, **kwargs)
    return wrapped


def _sanitize_name(name: str) -> str:
    """Spaces → hyphens; sirf letters (Hindi/English), numbers, hyphen."""
    name = (name or "").strip()
    name = re.sub(r"\s+", "-", name)
    kept = []
    for ch in name:
        if ch == "-":
            kept.append(ch)
            continue
        if unicodedata.category(ch)[0] in ("L", "N"):
            kept.append(ch)
    slug = "".join(kept)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug[:100]


@flask_app.route("/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    if (
        data.get("owner_name") == OWNER_NAME
        and data.get("admin_key") in ADMIN_KEYS
        and data.get("vip_key") in VIP_KEYS
    ):
        session.permanent = True
        session["is_admin"] = True
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Invalid Name / Admin Key / VIP Key."}), 401


@flask_app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@flask_app.route("/")
def index():
    return render_template("admin.html")


@flask_app.route("/api/generate", methods=["POST"])
@admin_required
def api_generate():
    data = request.get_json(silent=True) or {}
    original_url = (data.get("original_url") or "").strip()
    desired_name = (data.get("name") or "").strip()

    if not original_url:
        return jsonify({"ok": False, "error": "Original m3u8 link required"}), 400
    if not original_url.startswith(("http://", "https://")):
        return jsonify({"ok": False, "error": "Invalid link — valid http(s) URL do"}), 400

    name = _sanitize_name(desired_name)
    if not name:
        return jsonify({
            "ok": False,
            "error": "Invalid class name — sirf letters, numbers aur hyphen(-) allowed hai.",
        }), 400

    source_type = (data.get("source_type") or "live").strip().lower()
    title = display_title(name)

    now = datetime.utcnow()
    token = base64.urlsafe_b64encode(os.urandom(12)).decode().rstrip("=")

    # Live mode: ek unique code generate karo (title ke keywords + random),
    # jo streaming ke waqt MongoDB ki jagah backend memory (LIVE_CODES) se
    # validate hoga — 5 ghante ke liye valid.
    live_code = None
    live_code_expires_at = None
    if source_type == "live":
        live_code = generate_unique_code(name)
        live_code_expires_at = now + LIVE_CODE_TTL

    set_fields = {
        "original_url": original_url,
        "status": "LIVE",
        "title": title,
        "updated_at": now,
    }
    if live_code:
        set_fields["live_code"] = live_code
        set_fields["live_code_expires_at"] = live_code_expires_at

    lectures_col.update_one(
        {"_id": name},
        {
            "$set": set_fields,
            "$setOnInsert": {
                "created_at": now,
                "token": token,
                "duration": None,
                "file_size": None,
                "video_filename": None,
            },
            # Har naye/re-generate hone par watch_gen bump (field na ho to
            # $inc khud 0 se shuru karke 1 kar deta hai) — agar is naam ka
            # koi purana background watcher chal raha ho (purani link ke
            # liye) to wo khud-ba-khud supersede/stop ho jaayega, aur ek
            # naya watcher naye original_url ke liye start hoga neeche.
            "$inc": {"watch_gen": 1},
        },
        upsert=True,
    )
    doc = lectures_col.find_one({"_id": name})

    if live_code:
        _save_live_code(live_code, name, title, live_code_expires_at)

    if live_code:
        public_link = f"{PUBLIC_BASE_URL}/{live_code}/{original_url}"
    else:
        public_link = f"{PUBLIC_BASE_URL}/{name}"

    return jsonify({
        "ok": True,
        "name": name,
        "public_link": public_link,
        "status": doc.get("status", "LIVE"),
    })


@flask_app.route("/generated/<name>")
def generated(name):
    doc = lectures_col.find_one(
        {"_id": name}, {"_id": 1, "status": 1, "live_code": 1, "original_url": 1}
    )
    if not doc:
        return redirect(url_for("index"))
    if doc.get("live_code"):
        public_link = f"{PUBLIC_BASE_URL}/{doc['live_code']}/{doc['original_url']}"
    else:
        public_link = f"{PUBLIC_BASE_URL}/{name}"
    return render_template(
        "generated.html", name=name, public_link=public_link, status=doc.get("status")
    )


@flask_app.route("/health")
def health():
    return jsonify({"status": "ok"})


@flask_app.route("/<name>")
def play(name):
    doc = lectures_col.find_one({"_id": name})
    if not doc:
        return "Link galat hai ya Class expire ho gayi. 😔", 404
    return render_template(
        "player.html",
        name=name,
        title=display_title(name),
        status=doc.get("status", "LIVE"),
    )


@flask_app.route("/<code>/<path:original_url>")
def play_live_code(code, original_url):
    """Naya unique-code wala public link:
    PUBLIC_BASE_URL/<code>/<original m3u8 URL, http(s) samet, as-is>
    Validation sirf LIVE_CODES (backend memory) se — MongoDB ko is
    streaming path pe kabhi touch nahi kiya jaata (miss hone par hi ek
    baar fallback refill hota hai, _get_live_code ke andar)."""
    entry = _get_live_code(code)
    if not entry:
        return "Link expire ho gaya ya invalid hai. 😔", 404

    full_url = original_url
    if request.query_string:
        full_url += "?" + request.query_string.decode()
    # Vercel ka edge/routing layer path ke andar "//" ko kabhi-kabhi "/"
    # me collapse kar deta hai (well-known CDN/proxy normalization) —
    # isse "https://d2xi...cloudfront.net/..." "https:/d2xi..." (ek
    # slash) ban jaata hai aur URL genuinely valid hone ke baawajood
    # "invalid" dikhta hai. Yahan detect + repair karo.
    full_url = re.sub(r"^(https?):/(?!/)", r"\1://", full_url)
    if not full_url.startswith(("http://", "https://")):
        return "Link expire ho gaya ya invalid hai. 😔", 404

    return render_template(
        "player.html",
        name=entry["name"],
        title=entry.get("title") or display_title(entry["name"]),
        status="LIVE",
        live_code=code,
        live_original_url=full_url,
    )


def run_flask():
    port = int(os.environ.get("PORT", 8000))
    flask_app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    run_flask()
