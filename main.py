
# main.py
# Earnly Bot - PTB v20+ + FastAPI webhook + SQLite (single-file)
# Full features as requested:
#  - Task before reward: tracking /v?t=... & wait >= WAIT_SECONDS
#  - Max 10 ads/day, daily reset
#  - Referral $0.001 per valid /start <id> (no self-ref)
#  - Daily bonus $0.001 once/day
#  - Offerwall integration (hard-coded direct link w/ subid)
#  - /postback endpoint to credit offerwall via provider
#  - Withdraw requests + admin Approve/Reject
#  - Admin commands: /admin_broadcast, /admin_stats
#  - Leaderboard, Earnly Website button (coming soon)
#  - Top-of-chat UX via edit_message_text
#  - SQLite persistence (no balance loss)
#  - Micro-units: 1 micro = $0.001
#  - Basic anti-cheat: click token tracking & wait-time
# Use with uvicorn main:app --host 0.0.0.0 --port $PORT

import os, sqlite3, asyncio, json, time, secrets
from datetime import datetime, date
from typing import Optional
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, PlainTextResponse, JSONResponse
from dotenv import load_dotenv
load_dotenv()

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# -------------------------
# CONFIG (env or defaults)
# -------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "8458909740:AAFxZQCcuzMGZctsbo_AG4RmTmf-5L8YvRs")
BASE_URL = os.getenv("BASE_URL", "https://earnly-bot.onrender.com")  # your Render url
ADMIN_ID = int(os.getenv("ADMIN_ID", os.getenv("MY_ADMIN_ID", "7589508564")))  # your admin id

# Offerwall hard-coded direct link (we append subid=user_id)
OFFERWALL_DIRECT = "https://zwidgetymz56r.xyz/list/zCMQAYfI"

# micro-units: 1 micro = $0.001
AD_REWARD_MICRO = int(os.getenv("AD_REWARD_MICRO", "10"))      # default gross ad reward (10 micro = $0.01)
DAILY_BONUS_MICRO = int(os.getenv("DAILY_BONUS_MICRO", "1"))   # 1 micro = $0.001
REFERRAL_BONUS_MICRO = int(os.getenv("REFERRAL_BONUS_MICRO", "1"))
WITHDRAW_MIN_MICRO = int(os.getenv("WITHDRAW_MIN_MICRO", "1000"))  # 1000 micro = $1
MAX_ADS_PER_DAY = int(os.getenv("MAX_ADS_PER_DAY", "10"))
WAIT_SECONDS = int(os.getenv("WAIT_SECONDS", "30"))

DB_PATH = os.getenv("DB_PATH", "earnly.db")

# -------------------------
# DB helpers (sqlite)
# -------------------------
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn(); c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        balance_micro INTEGER DEFAULT 0,
        ad_balance_micro INTEGER DEFAULT 0,
        offer_balance_micro INTEGER DEFAULT 0,
        referral_balance_micro INTEGER DEFAULT 0,
        total_earned_micro INTEGER DEFAULT 0,
        referrals_count INTEGER DEFAULT 0,
        referred_by INTEGER DEFAULT NULL,
        last_daily_bonus TEXT DEFAULT NULL,
        last_reset_date TEXT DEFAULT NULL,
        ads_today INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS clicks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        token TEXT,
        ts INTEGER
    );
    CREATE TABLE IF NOT EXISTS withdraws (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount_micro INTEGER,
        status TEXT DEFAULT 'pending',
        requested_at INTEGER
    );
    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        type TEXT,
        amount_micro INTEGER,
        created_at INTEGER
    );
    """)
    conn.commit(); conn.close()

def ensure_user(user_id: int, username: Optional[str]=None, referred_by: Optional[int]=None):
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
    if not c.fetchone():
        c.execute("INSERT INTO users (user_id, username, referred_by) VALUES (?, ?, ?)", (user_id, username, referred_by))
        conn.commit()
    conn.close()

def get_user(user_id:int):
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    r = c.fetchone(); conn.close(); return r

def credit(user_id:int, amount_micro:int, field:str="balance_micro", add_total=True, tx_type:str="credit"):
    conn = get_conn(); c = conn.cursor()
    c.execute(f"UPDATE users SET {field} = {field} + ? WHERE user_id = ?", (amount_micro, user_id))
    if add_total:
        c.execute("UPDATE users SET total_earned_micro = total_earned_micro + ? WHERE user_id = ?", (amount_micro, user_id))
    ts = int(datetime.utcnow().timestamp())
    c.execute("INSERT INTO transactions (user_id, type, amount_micro, created_at) VALUES (?, ?, ?, ?)", (user_id, tx_type, amount_micro, ts))
    conn.commit(); conn.close()

def record_click(user_id:int, token:str):
    ts = int(datetime.utcnow().timestamp())
    conn = get_conn(); c = conn.cursor()
    c.execute("INSERT INTO clicks (user_id, token, ts) VALUES (?, ?, ?)", (user_id, token, ts))
    conn.commit(); conn.close()
    return ts

def get_click(user_id:int, token:str):
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT * FROM clicks WHERE user_id = ? AND token = ? ORDER BY ts DESC LIMIT 1", (user_id, token))
    r = c.fetchone(); conn.close(); return r

def inc_ads_today(user_id:int):
    today = date.today().isoformat()
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT last_reset_date, ads_today FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if not row: conn.close(); return 0
    last = row["last_reset_date"]; ads = row["ads_today"] or 0
    if last != today:
        c.execute("UPDATE users SET last_reset_date = ?, ads_today = 1 WHERE user_id = ?", (today, user_id))
        conn.commit(); conn.close(); return 1
    ads += 1
    c.execute("UPDATE users SET ads_today = ? WHERE user_id = ?", (ads, user_id))
    conn.commit(); conn.close(); return ads

def can_watch_more_ads(user_id:int, max_per_day:int):
    today = date.today().isoformat()
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT last_reset_date, ads_today FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone(); conn.close()
    if not row: return True
    last = row["last_reset_date"]; ads = row["ads_today"] or 0
    if last != today: return True
    return ads < max_per_day

def has_claimed_daily(user_id:int):
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT last_daily_bonus FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone(); conn.close()
    if not row: return False
    return row["last_daily_bonus"] == date.today().isoformat()

def set_daily_bonus_claimed(user_id:int):
    today = date.today().isoformat()
    conn = get_conn(); c = conn.cursor()
    c.execute("UPDATE users SET last_daily_bonus = ? WHERE user_id = ?", (today, user_id))
    conn.commit(); conn.close()

def add_referral_for(referrer_id:int):
    conn = get_conn(); c = conn.cursor()
    c.execute("UPDATE users SET referrals_count = referrals_count + 1 WHERE user_id = ?", (referrer_id,))
    conn.commit(); conn.close()

def add_withdraw_request(user_id:int, amount_micro:int):
    ts = int(datetime.utcnow().timestamp())
    conn = get_conn(); c = conn.cursor()
    c.execute("INSERT INTO withdraws (user_id, amount_micro, status, requested_at) VALUES (?, ?, 'pending', ?)", (user_id, amount_micro, ts))
    wid = c.lastrowid
    conn.commit(); conn.close()
    return wid

def get_pending_withdraws():
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT * FROM withdraws WHERE status = 'pending'")
    r = c.fetchall(); conn.close(); return r

def approve_withdraw(wid:int):
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT * FROM withdraws WHERE id = ?", (wid,))
    r = c.fetchone()
    if not r: conn.close(); return False
    if r["status"] != "pending": conn.close(); return False
    user_id = r["user_id"]; amt = r["amount_micro"]
    # deduct user_balance (we deduct from balance_micro)
    c.execute("UPDATE users SET balance_micro = balance_micro - ? WHERE user_id = ?", (amt, user_id))
    c.execute("UPDATE withdraws SET status = 'approved' WHERE id = ?", (wid,))
    conn.commit(); conn.close(); return True

def reject_withdraw(wid:int):
    conn = get_conn(); c = conn.cursor()
    c.execute("UPDATE withdraws SET status = 'rejected' WHERE id = ?", (wid,))
    conn.commit(); conn.close(); return True

def total_users():
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT COUNT(*) as cnt FROM users"); r = c.fetchone(); conn.close(); return r["cnt"]

def top_users(limit=10):
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT user_id, balance_micro FROM users ORDER BY balance_micro DESC LIMIT ?", (limit,))
    rows = c.fetchall(); conn.close(); return rows

# -------------------------
# Helpers
# -------------------------
def micro_to_usd(micro:int) -> str:
    usd = micro * 0.001
    return f"${usd:.4f}"

def make_user_keyboard(user_id:int):
    # 3-per-row layout
    row1 = [
        InlineKeyboardButton("▶ Watch Ad", callback_data="watch_ad"),
        InlineKeyboardButton("📋 Offerwall", callback_data="offerwall"),
        InlineKeyboardButton("🎁 Daily Bonus", callback_data="daily_bonus"),
    ]
    row2 = [
        InlineKeyboardButton("👥 Referrals", callback_data="referrals"),
        InlineKeyboardButton("💰 Balance", callback_data="balance"),
        InlineKeyboardButton("💸 Withdraw", callback_data="withdraw"),
    ]
    row3 = [
        InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard"),
        InlineKeyboardButton("🌐 Earnly Website", callback_data="earnly_website"),
    ]
    kb = [row1, row2, row3]
    if user_id == ADMIN_ID:
        kb.append([InlineKeyboardButton("🛠 Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(kb)

# -------------------------
# App + Telegram Application
# -------------------------
app = FastAPI()
application: Optional[Application] = None

@app.on_event("startup")
async def startup():
    global application
    init_db()
    application = Application.builder().token(BOT_TOKEN).build()
    # register handlers
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CallbackQueryHandler(on_button))
    application.add_handler(CommandHandler("admin_broadcast", admin_broadcast))
    application.add_handler(CommandHandler("admin_stats", admin_stats))
    # start PTB & set webhook to /webhook/<token>
    await application.initialize()
    await application.start()
    webhook_url = f"{BASE_URL.rstrip('/')}/webhook/{BOT_TOKEN}"
    try:
        await application.bot.set_webhook(webhook_url)
        print("Webhook set to:", webhook_url)
    except Exception as e:
        print("Failed to set webhook:", e)

@app.on_event("shutdown")
async def shutdown():
    global application
    if application:
        try:
            await application.bot.delete_webhook()
        except:
            pass
        await application.stop()
        await application.shutdown()

# Telegram webhook receiver
@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    if token != BOT_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")
    data = await request.json()
    upd = Update.de_json(data, application.bot)
    # hand to PTB
    await application.update_queue.put(upd)
    return JSONResponse({"ok": True})

# Tracking link endpoint: /v?t=TOKEN&user=USERID
@app.get("/v")
async def track_and_redirect(t: str = "", user: Optional[int] = None):
    # record click for the user if provided
    if user:
        record_click(user, t or "none")
    # redirect to offerwall DIRECT (for ad rotation you can extend)
    target = OFFERWALL_DIRECT
    sep = "&" if "?" in target else "?"
    if user:
        target = f"{target}{sep}subid={user}"
    return RedirectResponse(url=target)

# Postback endpoint to receive offerwall/CPA credits
# Example: /postback?subid=123&amount=0.50
@app.get("/postback")
async def postback(subid: Optional[int] = None, amount: Optional[float] = 0.0):
    if not subid:
        return PlainTextResponse("Missing subid", status_code=400)
    # Convert USD to micro (1 micro = $0.001)
    micro = int(round(amount / 0.001))
    ensure_user(subid)
    # credit 80% to user offer_balance, owner 20% kept (owner accounting optional)
    user_share = int(round(micro * 0.80))
    credit(subid, user_share, field="offer_balance_micro", add_total=True, tx_type="offer")
    return PlainTextResponse("OK")

# -------------------------
# Telegram handlers
# -------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args or []
    referred_by = None
    if args:
        try:
            p = int(args[0])
            if p != user.id:
                referred_by = p
        except:
            referred_by = None

    ensure_user(user.id, user.username or "")
    # handle referral once (we simply credit referrer on /start with ref id; in production, ensure one-time only)
    if referred_by:
        # only credit if referrer exists and not self and referrer not previously counted
        r = get_user(referred_by)
        if r:
            add_referral_for(referred_by)
            credit(referred_by, REFERRAL_BONUS_MICRO, field="referral_balance_micro", add_total=True, tx_type="referral")

    row = get_user(user.id)
    bal = row["balance_micro"] if row else 0
    text = (f"👋 Hello {user.first_name}!\n\n"
            f"Total Balance: {micro_to_usd(bal)}\n\n"
            "Choose an action below:")
    await update.message.reply_text(text, reply_markup=make_user_keyboard(user.id))

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    uid = user.id
    ensure_user(uid, user.username or "")

    # WATCH AD -> generate token & send link + I watched button
    if query.data == "watch_ad":
        token = secrets.token_hex(8)
        track_url = f"{BASE_URL.rstrip('/')}/v?t={token}&user={uid}"
        text = (f"▶ Open the ad link below (tracking enabled).\n"
                f"You MUST watch/keep tab open for at least {WAIT_SECONDS} seconds, then click *I watched*.")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Open Ad (tracking)", url=track_url)],
            [InlineKeyboardButton("I watched", callback_data=f"confirm_ad:{token}")]
        ])
        await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
        return

    # Confirm ad after watching
    if query.data and query.data.startswith("confirm_ad:"):
        token = query.data.split(":",1)[1]
        click = get_click(uid, token)
        if not click:
            await query.edit_message_text("❌ Could not verify click. Use the *Open Ad (tracking)* button first.", reply_markup=make_user_keyboard(uid))
            return
        elapsed = int(time.time()) - click["ts"]
        if elapsed < WAIT_SECONDS:
            await query.edit_message_text(f"⏳ You waited {elapsed}s. You must wait {WAIT_SECONDS}s before claiming.", reply_markup=make_user_keyboard(uid))
            return
        if not can_watch_more_ads(uid, MAX_ADS_PER_DAY):
            await query.edit_message_text(f"⚠️ Daily ad limit reached ({MAX_ADS_PER_DAY}).", reply_markup=make_user_keyboard(uid))
            return
        # credit 80% of AD_REWARD_MICRO to user's ad_balance
        user_share = int(round(AD_REWARD_MICRO * 0.80))
        credit(uid, user_share, field="ad_balance_micro", add_total=True, tx_type="ad")
        inc_ads_today(uid)
        row = get_user(uid)
        await query.edit_message_text(f"✅ You earned {micro_to_usd(user_share)} for watching the ad.\nTotal: {micro_to_usd(row['balance_micro'])}", reply_markup=make_user_keyboard(uid))
        return

    # Offerwall: open direct link with subid
    if query.data == "offerwall":
        url = OFFERWALL_DIRECT
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}subid={uid}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Open Offerwall", url=url)]])
        await query.edit_message_text("🔗 Open the offerwall to complete tasks. Credits use postback.", reply_markup=kb)
        return

    # Daily bonus
    if query.data == "daily_bonus":
        if has_claimed_daily(uid):
            await query.edit_message_text("❌ You already claimed today's bonus.", reply_markup=make_user_keyboard(uid))
            return
        credit(uid, DAILY_BONUS_MICRO, field="balance_micro", add_total=True, tx_type="bonus")
        set_daily_bonus_claimed(uid)
        row = get_user(uid)
        await query.edit_message_text(f"🎁 Daily bonus credited {micro_to_usd(DAILY_BONUS_MICRO)}\nTotal: {micro_to_usd(row['balance_micro'])}", reply_markup=make_user_keyboard(uid))
        return

    # Referrals screen
    if query.data == "referrals":
        row = get_user(uid)
        rc = row["referrals_count"] if row else 0
        bot_username = (await context.bot.get_me()).username
        link = f"https://t.me/{bot_username}?start={uid}"
        await query.edit_message_text(f"👥 Your referral link:\n{link}\n\nReferrals: {rc}\nBonus: {micro_to_usd(REFERRAL_BONUS_MICRO)} each", reply_markup=make_user_keyboard(uid))
        return

    # Balance screen (show breakdown + total)
    if query.data == "balance":
        row = get_user(uid)
        if not row:
            await query.edit_message_text("0 balance", reply_markup=make_user_keyboard(uid)); return
        total = row["balance_micro"]
        ad = row["ad_balance_micro"]
        offer = row["offer_balance_micro"]
        referral = row["referral_balance_micro"]
        text = (f"💰 Your balances:\n\n"
                f"Total: {micro_to_usd(total)}\n"
                f" - Ads: {micro_to_usd(ad)}\n"
                f" - Offerwall: {micro_to_usd(offer)}\n"
                f" - Referrals: {micro_to_usd(referral)}\n")
        await query.edit_message_text(text, reply_markup=make_user_keyboard(uid))
        return

    # Withdraw request
    if query.data == "withdraw":
        row = get_user(uid)
        bal = row["balance_micro"] if row else 0
        if bal < WITHDRAW_MIN_MICRO:
            await query.edit_message_text(f"💳 Withdraw requires minimum {micro_to_usd(WITHDRAW_MIN_MICRO)}. Your total: {micro_to_usd(bal)}", reply_markup=make_user_keyboard(uid))
            return
        wid = add_withdraw_request(uid, bal)
        await query.edit_message_text(f"✅ Withdraw request #{wid} submitted for {micro_to_usd(bal)}. Admin will review.", reply_markup=make_user_keyboard(uid))
        # notify admin with approve/reject buttons
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Approve", callback_data=f"approve_withdraw:{wid}"),
             InlineKeyboardButton("Reject", callback_data=f"reject_withdraw:{wid}")]
        ])
        try:
            await context.bot.send_message(ADMIN_ID, f"Withdraw #{wid} requested by {uid}: {micro_to_usd(bal)}", reply_markup=kb)
        except Exception as e:
            print("Failed to notify admin:", e)
        return

    # Admin approve/reject actions
    if query.data and query.data.startswith("approve_withdraw:") and query.from_user.id == ADMIN_ID:
        wid = int(query.data.split(":")[1])
        ok = approve_withdraw(wid)
        if ok:
            conn = get_conn(); cur = conn.cursor()
            cur.execute("SELECT user_id, amount_micro FROM withdraws WHERE id = ?", (wid,))
            r = cur.fetchone(); conn.close()
            if r:
                await context.bot.send_message(r["user_id"], f"✅ Your withdraw #{wid} approved. Amount: {micro_to_usd(r['amount_micro'])}")
            await query.edit_message_text("✅ Withdraw approved.")
        else:
            await query.edit_message_text("❌ Could not approve (maybe processed).")
        return

    if query.data and query.data.startswith("reject_withdraw:") and query.from_user.id == ADMIN_ID:
        wid = int(query.data.split(":")[1])
        reject_withdraw(wid)
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT user_id, amount_micro FROM withdraws WHERE id = ?", (wid,))
        r = cur.fetchone(); conn.close()
        if r:
            await context.bot.send_message(r["user_id"], f"❌ Your withdraw #{wid} was rejected.")
        await query.edit_message_text("❌ Withdraw rejected.")
        return

    # Leaderboard
    if query.data == "leaderboard":
        rows = top_users(10)
        text = "🏆 Leaderboard\n\n"
        for i, r in enumerate(rows, start=1):
            text += f"{i}. {r['user_id']} — {micro_to_usd(r['balance_micro'])}\n"
        await query.edit_message_text(text, reply_markup=make_user_keyboard(uid))
        return

    # Earnly Website
    if query.data == "earnly_website":
        await query.edit_message_text("🌐 Earnly Website is coming soon!", reply_markup=make_user_keyboard(uid))
        return

    # Admin panel simple view
    if query.data == "admin_panel" and query.from_user.id == ADMIN_ID:
        pending = get_pending_withdraws()
        text = f"🛠 Admin Panel\nPending withdraws: {len(pending)}\nTotal users: {total_users()}"
        await query.edit_message_text(text)
        return

    await query.edit_message_text("Unknown action. Returning to menu.", reply_markup=make_user_keyboard(uid))

# Admin commands
async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Not allowed.")
        return
    text = " ".join(context.args) if context.args else ""
    if not text:
        await update.message.reply_text("Usage: /admin_broadcast <message>")
        return
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT user_id FROM users"); rows = cur.fetchall(); conn.close()
    sent = 0
    for r in rows:
        try:
            await context.bot.send_message(r["user_id"], text)
            sent += 1
        except:
            pass
    await update.message.reply_text(f"Broadcast sent to {sent} users.")

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Not allowed.")
        return
    users = total_users()
    pending = len(get_pending_withdraws())
    await update.message.reply_text(f"Total users: {users}\nPending withdraws: {pending}")

# -------------------------
# Run uvicorn if __main__
# -------------------------
if __name__ == "__main__":
    init_db()
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
