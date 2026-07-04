#!/usr/bin/env python3
"""
Marukyu-Koyamaen matcha restock monitor
========================================

Checks each matcha product page on the Marukyu-Koyamaen international shop and
emails you the moment anything flips from SOLD OUT -> IN STOCK.

It reads products.json (same folder), remembers the last status in state.json,
and only emails on a genuine restock (a size becoming available again).

Checks run around the clock: most restocks happen Mon-Fri 09:00-17:30 Japan
time, but they've been observed outside those hours too.

To be considerate to the shop (it explicitly asks people not to hammer it), each
5-minute run only re-checks products that are currently sold out -- the only ones
that *could* restock -- and does a full sweep once an hour to pick up new sell-outs.

--------------------------------------------------------------------------------
ONE-TIME EMAIL SETUP
--------------------------------------------------------------------------------
1. Turn on 2-Step Verification for your Google account.
2. Create an App Password:  https://myaccount.google.com/apppasswords
3. Put your credentials in a file next to this script called  .env  :
       MATCHA_SMTP_USER=you@gmail.com
       MATCHA_SMTP_PASS=your16charapppassword
       MATCHA_MAIL_TO=you@gmail.com
   (or export them as environment variables instead)

--------------------------------------------------------------------------------
RUN IT
--------------------------------------------------------------------------------
   python3 matcha_monitor.py            # one check now
   python3 matcha_monitor.py --force    # one check now, full sweep of all products
   python3 matcha_monitor.py --loop 5   # check every 5 minutes forever

For fully automatic background running, use the included launchd file
(com.matcha.monitor.plist) -- see README.md.
"""

import json
import os
import re
import sys
import time
import smtplib
import urllib.request
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from pathlib import Path

# ------------------------------ paths & config --------------------------------
HERE = Path(__file__).resolve().parent
PRODUCTS_FILE = HERE / "products.json"
STATE_FILE = HERE / "state.json"
LOG_FILE = HERE / "monitor.log"
ENV_FILE = HERE / ".env"

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

OUT_OF_STOCK_MARKER = "currently out of stock and unavailable"  # WooCommerce phrase
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
REQUEST_TIMEOUT = 30
POLITE_DELAY = 2.0        # seconds between product requests
# ------------------------------------------------------------------------------


def load_env():
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'").strip()
                if k == "MATCHA_SMTP_PASS":
                    v = v.replace(" ", "")  # Gmail shows app passwords spaced; they work unspaced
                os.environ[k] = v


def jst_now():
    return datetime.now(timezone(timedelta(hours=9)))


def stamp():
    """Timestamp format used in state entries."""
    return jst_now().strftime("%Y-%m-%d %H:%M JST")


def log(msg):
    ts = jst_now().strftime("%Y-%m-%d %H:%M:%S JST")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_catalog():
    """products.json, parsed once per run and shared by every consumer."""
    return json.loads(PRODUCTS_FILE.read_text(encoding="utf-8"))


def shop_base(catalog):
    return catalog["base_url"], catalog.get("currency", "USD")


def product_url(base, pid, cur):
    return f"{base}{pid}?currency={cur}"


def load_products(catalog, state=None):
    base, cur = shop_base(catalog)
    items = []
    for p in catalog["products"]:
        if p.get("watch", True):
            p = dict(p)
            p["url"] = product_url(base, p["id"], cur)
            items.append(p)
    # Include auto-discovered products (stored in state.json) that are watched.
    if state:
        listed = {p["id"] for p in items}
        for pid, s in state.items():
            if s.get("discovered") and s.get("watch", True) and pid not in listed:
                items.append({"id": pid, "name": s.get("name", pid),
                              "category": s.get("category", "New arrival"),
                              "url": product_url(base, pid, cur)})
    return items


def all_known_ids(catalog, state):
    ids = {p["id"] for p in catalog["products"]}
    ids |= set(state.keys())
    return ids


def load_state():
    return json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
              "image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

# A real product page always contains these; a block/error page won't. Used to
# avoid mistaking a 403/challenge page for an "in stock" product.
PAGE_SENTINELS = ("preparation of matcha", "proceed to cart", "best before")


def _decode(data, encoding):
    encoding = (encoding or "").lower()
    if "gzip" in encoding:
        import gzip as _gz
        data = _gz.decompress(data)
    elif "deflate" in encoding:
        import zlib
        try:
            data = zlib.decompress(data)
        except zlib.error:
            data = zlib.decompress(data, -zlib.MAX_WBITS)
    return data.decode("utf-8", "ignore")


def _curl_fetch(url):
    """Fallback via the system curl (different TLS stack; slips past some WAFs)."""
    import subprocess
    cmd = ["curl", "-sS", "--compressed", "--http2", "--max-time", str(REQUEST_TIMEOUT),
           "-A", USER_AGENT,
           "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
           "-H", "Accept-Language: en-US,en;q=0.9",
           url]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=REQUEST_TIMEOUT + 5)
    if out.returncode != 0 or not out.stdout:
        raise RuntimeError(f"curl rc={out.returncode} {out.stderr.strip()[:120]}")
    return out.stdout


def fetch(url):
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
            return _decode(r.read(), r.headers.get("Content-Encoding"))
    except Exception:
        return _curl_fetch(url)   # try curl before giving up


def is_product_page(html):
    h = html.lower()
    return any(s in h for s in PAGE_SENTINELS)


def in_stock(html):
    return OUT_OF_STOCK_MARKER not in html.lower()


def _send_mail(subject, body, kind="EMAIL"):
    """Send one email to the configured recipients. All alert emails go
    through here so credential handling and delivery logic live in one place."""
    user, pw = os.environ.get("MATCHA_SMTP_USER"), os.environ.get("MATCHA_SMTP_PASS")
    to_raw = os.environ.get("MATCHA_MAIL_TO", user) or ""
    recipients = [a.strip() for a in to_raw.split(",") if a.strip()]
    if not (user and pw and recipients):
        log(f"  {kind} SKIPPED: MATCHA_SMTP_USER / MATCHA_SMTP_PASS / MATCHA_MAIL_TO not set (see .env).")
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"], msg["To"] = user, ", ".join(recipients)
    msg.set_content(body)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=REQUEST_TIMEOUT) as s:
        s.starttls()
        s.login(user, pw)
        s.send_message(msg, to_addrs=recipients)
    log(f"  {kind} SENT to {', '.join(recipients)}: {subject}")


def send_email(restocked):
    body_lines = [f"IN STOCK: {p['name']}  ({p['category']})\n  {p['url']}" for p in restocked]
    body = ("Back in stock at Marukyu-Koyamaen:\n\n"
            + "\n\n".join(body_lines)
            + "\n\nMatcha is limited to 5 items per order. Move fast.")
    names = ", ".join(p["name"] for p in restocked)
    _send_mail(f"Matcha restock: {names}", body, kind="RESTOCK EMAIL")


def extract_name(html):
    m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', html, re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r'<title>([^<|]+)', html, re.I)
    if m:
        return m.group(1).strip()
    return None


def discover_new(catalog, state):
    """Scan the shop's Matcha catalog for product IDs we've never seen. New ones
    get auto-added to state (watched) and returned so we can email about them."""
    base, cur = shop_base(catalog)
    catalog_url = f"{base}catalog/matcha?viewall=1"
    try:
        html = fetch(catalog_url)
    except Exception as e:
        log(f"  (discovery skipped: catalog fetch failed: {e})")
        return []
    # Product IDs start with a digit; category slugs (catalog, matcha, ...) don't.
    ids = set(re.findall(r'/english/shop/products/([0-9][0-9a-z]{4,})', html))
    if len(ids) < 30:
        log(f"  (discovery skipped: catalog looked blocked/incomplete - {len(ids)} links)")
        return []
    known = all_known_ids(catalog, state)
    new_ids = sorted(i for i in ids if i not in known)
    if not new_ids:
        return []
    checked_at = stamp()
    discovered = []
    for pid in new_ids:
        url = product_url(base, pid, cur)
        try:
            phtml = fetch(url)
        except Exception:
            phtml = ""
        # Only auto-watch if it verifiably looks like a Matcha product page.
        if not (phtml and is_product_page(phtml) and "matcha" in phtml.lower()):
            state[pid] = {"name": extract_name(phtml) or pid, "in_stock": None,
                          "checked": checked_at, "discovered": True, "watch": False,
                          "note": "auto-found but unverified / not matcha"}
            time.sleep(POLITE_DELAY)
            continue
        name = extract_name(phtml) or pid
        state[pid] = {"name": name, "in_stock": in_stock(phtml), "checked": checked_at,
                      "discovered": True, "watch": True, "category": "New arrival"}
        discovered.append({"id": pid, "name": name, "url": url})
        time.sleep(POLITE_DELAY)
    return discovered


def send_new_product_email(new_products):
    body_lines = [f"{p['name']}\n  {p['url']}" for p in new_products]
    body = ("New matcha just appeared on Marukyu-Koyamaen (now being watched for restocks):\n\n"
            + "\n\n".join(body_lines)
            + "\n\nYou'll get a restock alert whenever any of these comes into stock.")
    names = ", ".join(p["name"] for p in new_products)
    _send_mail(f"New matcha added: {names}", body, kind="NEW-PRODUCT EMAIL")


def run_once(force=False):
    state = load_state()
    catalog = load_catalog()

    # Full sweep on first run, at the top of each hour, or when forced.
    full_sweep = force or not state or jst_now().minute < 6

    # Auto-discover brand-new matcha added to the shop. The catalog page is the
    # heaviest page on the site, so only fetch it on hourly full sweeps —
    # spotting a new product within the hour is plenty fast.
    if full_sweep:
        newly = discover_new(catalog, state)
        if newly:
            for p in newly:
                log(f"  ** NEW PRODUCT ** {p['name']}")
            try:
                send_new_product_email(newly)
            except Exception as e:
                log(f"  ! NEW-PRODUCT EMAIL FAILED ({e}) - continuing with stock check")

    products = load_products(catalog, state)

    if full_sweep:
        to_check = products
    else:
        to_check = [p for p in products
                    if state.get(p["id"], {}).get("in_stock") is not True]

    log(f"Checking {len(to_check)} product(s) "
        f"({'full sweep' if full_sweep else 'sold-out candidates only'}).")

    restocked = []
    checked_at = stamp()
    blocked = 0
    for p in to_check:
        try:
            html = fetch(p["url"])
        except Exception as e:
            log(f"  ! {p['name']}: fetch error ({e}) - state unchanged")
            time.sleep(POLITE_DELAY)
            continue
        if not is_product_page(html):
            # Blocked / challenge / error page — do NOT treat as in stock.
            blocked += 1
            log(f"  ! {p['name']}: blocked or non-product response - state unchanged")
            time.sleep(POLITE_DELAY)
            continue
        stock = in_stock(html)
        prev = state.get(p["id"], {}).get("in_stock")
        if stock and prev is False:
            restocked.append(p)
            log(f"  ** RESTOCK ** {p['name']}")
        state[p["id"]] = {"name": p["name"], "in_stock": stock, "checked": checked_at}
        time.sleep(POLITE_DELAY)

    if blocked:
        log(f"  WARNING: {blocked}/{len(to_check)} requests were blocked by the shop "
            f"(bot protection). No alerts sent for those.")

    if restocked:
        try:
            send_email(restocked)
        except Exception as e:
            # Keep these marked sold-out so the alert is retried on the next run
            # instead of being lost forever.
            log(f"  ! RESTOCK EMAIL FAILED ({e}) - will retry next run")
            for p in restocked:
                state[p["id"]]["in_stock"] = False
    else:
        log("  No new restocks.")
    save_state(state)


def main():
    load_env()
    if "--test-email" in sys.argv:
        log("Sending a test email to confirm credentials/delivery...")
        send_email([{"name": "TEST — ignore me", "category": "setup check",
                     "url": "https://www.marukyu-koyamaen.co.jp/english/shop/products/catalog/matcha"}])
        return
    force = "--force" in sys.argv
    if "--loop" in sys.argv:
        i = sys.argv.index("--loop")
        minutes = float(sys.argv[i + 1]) if i + 1 < len(sys.argv) else 5
        log(f"Starting loop: every {minutes} min (Ctrl-C to stop).")
        while True:
            try:
                run_once(force=force)
            except Exception as e:
                log(f"run error: {e}")
            time.sleep(minutes * 60)
    else:
        run_once(force=force)


if __name__ == "__main__":
    main()
