import sqlite3
import logging
import datetime
import asyncio
import os
import requests
import io # Added for BytesIO
import base64 # Added for base64 decoding

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ParseMode

# --- Configuration Section ---
TELEGRAM_BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN" # আপনার টেলিগ্রাম বট টোকেন দিন
SUPER_ADMIN_ID = 123456789 # আপনার সুপার অ্যাডমিন ID দিন
SUB_ADMIN_IDS = [] # অন্যান্য সাব অ্যাডমিন ID গুলো লিস্টে যোগ করুন
ALL_ADMIN_IDS = [SUPER_ADMIN_ID] + SUB_ADMIN_IDS
ITEMS_PER_PAGE = 5
WHATSAPP_API_URL = "http://localhost:3000"  # WhatsApp API সার্ভারের ঠিকানা

# পয়েন্ট সিস্টেম
POINTS_PER_LOGIN = 10
POINTS_PER_REFERRAL = 20
POINTS_PER_DAILY_LOGIN = 5
POINTS_STREAK_BONUS = 50 # Not used in current code, but can be implemented
POINTS_TO_BDT_RATE = 10
MIN_WITHDRAWAL_BDT = 100

# Conversation states
PHONE_NUMBER, WAIT_FOR_QR_CONFIRMATION, WITHDRAW_AMOUNT, WITHDRAW_NUMBER, BROADCAST_MESSAGE, ADMIN_SESSION_ACTION = range(6)

# Logging setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Database Setup ---
def setup_database():
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, 
        username TEXT, 
        points INTEGER DEFAULT 0, 
        referral_code TEXT,
        referred_by INTEGER, 
        last_login DATE, 
        login_streak INTEGER DEFAULT 0,
        successful_sessions INTEGER DEFAULT 0, 
        failed_sessions INTEGER DEFAULT 0
    )""")
    # Note: 'session_data' will now store a placeholder, as baileys manages files.
    # 'two_fa_pass' is removed as it's not applicable with baileys in this context.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS sessions (
        session_id INTEGER PRIMARY KEY AUTOINCREMENT, 
        user_id INTEGER, 
        phone_number TEXT NOT NULL UNIQUE, -- Phone number should be unique per session
        session_data TEXT DEFAULT 'Baileys Managed', -- Placeholder
        status TEXT DEFAULT 'active', 
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users (user_id)
    )""")
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS withdrawals (
        request_id INTEGER PRIMARY KEY AUTOINCREMENT, 
        user_id INTEGER, 
        amount_bdt REAL,
        points_used INTEGER, 
        payment_number TEXT, 
        status TEXT DEFAULT 'pending',
        requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, 
        FOREIGN KEY (user_id) REFERENCES users (user_id)
    )""")
    conn.commit()
    conn.close()

# --- WhatsApp API Functions ---
async def initiate_whatsapp_login(phone_number: str) -> (str, str):
    """WhatsApp লগইন শুরু করে এবং QR কোড ইমেজের URL বা Data URL রিটার্ন করে"""
    try:
        response = requests.post(
            f"{WHATSAPP_API_URL}/sessions", 
            json={"phone": phone_number}
        )
        if response.status_code == 200:
            data = response.json()
            qr_url = data.get("qr_url")
            status = data.get("status") # 'authenticated' if already logged in
            return qr_url, status
        elif response.status_code == 409: # Session already exists
            logger.info(f"Session for {phone_number} already exists.")
            return None, "exists"
        logger.error(f"API error: {response.status_code} - {response.text}")
        return None, "error"
    except Exception as e:
        logger.error(f"Error initiating WhatsApp login: {e}")
        return None, "error"

async def check_whatsapp_login_status(phone_number: str) -> str:
    """WhatsApp লগইন স্ট্যাটাস চেক করে ('authenticated', 'pending_qr', 'not_found')"""
    try:
        response = requests.get(
            f"{WHATSAPP_API_URL}/sessions/{phone_number}/status"
        )
        if response.status_code == 200:
            data = response.json()
            return data.get("status")
        elif response.status_code == 404:
            return "not_found"
        logger.error(f"API status check error: {response.status_code} - {response.text}")
        return "error"
    except Exception as e:
        logger.error(f"Error checking login status: {e}")
        return "error"

async def terminate_whatsapp_session(phone_number: str) -> bool:
    """WhatsApp সেশন terminate করে"""
    try:
        response = requests.delete(
            f"{WHATSAPP_API_URL}/sessions/{phone_number}"
        )
        if response.status_code == 200:
            return True
        logger.error(f"API session termination error: {response.status_code} - {response.text}")
        return False
    except Exception as e:
        logger.error(f"Error terminating WhatsApp session: {e}")
        return False

# --- UI Helper Functions ---
def get_main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    if user_id in ALL_ADMIN_IDS:
        keyboard = [
            ["👁️ ইউজার লিস্ট", "🧾 উইথড্র রিকুয়েস্ট"],
            ["🔁 সেশন ম্যানেজমেন্ট", "🔔 ব্রডকাস্ট"],
        ]
    else:
        keyboard = [
            ["▶️ WhatsApp লগইন", "📊 আমার একাউন্ট"],
            ["💰 উইথড্র", "🎁 রেফার কোড"],
            ["✅ Active Sessions"],
        ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

# --- Start Command & Main Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    db_user = cursor.fetchone()
    
    if not db_user:
        referral_code = f"ref_{user_id}"
        cursor.execute(
            "INSERT INTO users (user_id, username, referral_code, last_login, login_streak, points) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, user.username or user.first_name, referral_code, datetime.date.today(), 1, POINTS_PER_DAILY_LOGIN)
        )
        await update.message.reply_text(f"স্বাগতম! আপনি প্রথমবার লগইন করার জন্য {POINTS_PER_DAILY_LOGIN} পয়েন্ট পেয়েছেন।")
    else:
        today = datetime.date.today()
        # Ensure last_login is handled correctly, even if it's None or invalid
        last_login_str = db_user[5] if db_user and len(db_user) > 5 and db_user[5] else '1970-01-01'
        try:
            last_login = datetime.datetime.strptime(last_login_str, '%Y-%m-%d').date()
        except ValueError:
            last_login = datetime.date(1970, 1, 1) # Fallback to a very old date

        if last_login < today:
            cursor.execute("UPDATE users SET points = points + ?, last_login = ? WHERE user_id = ?", 
                          (POINTS_PER_DAILY_LOGIN, today, user_id))
            await context.bot.send_message(chat_id=user_id, text=f"পুনরায় স্বাগতম! আজকের ডেইলি লগইন বোনাস: {POINTS_PER_DAILY_LOGIN} পয়েন্ট।")

    conn.commit()
    conn.close()

    reply_markup = get_main_keyboard(user_id)
    await update.message.reply_text("👋 আপনাকে স্বাগতম! অনুগ্রহ করে একটি অপশন বেছে নিন:", reply_markup=reply_markup)
    return ConversationHandler.END

async def main_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    user_id = update.effective_user.id
    
    if text == "▶️ WhatsApp লগইন":
        await update.message.reply_text("📞 অনুগ্রহ করে আপনার WhatsApp নম্বরটি কান্ট্রি কোডসহ দিন (যেমন: +8801712345678):")
        return PHONE_NUMBER
    elif text == "📊 আমার একাউন্ট":
        await my_account(update, context)
    elif text == "💰 উইথড্র":
        await start_withdraw_request(update, context)
        return WITHDRAW_AMOUNT
    elif text == "🎁 রেফার কোড":
        await get_referral_code(update, context)
    elif text == "✅ Active Sessions":
        await list_active_sessions(update, context)
    
    elif user_id in ALL_ADMIN_IDS:
        if text == "👁️ ইউজার লিস্ট":
            await list_all_users(update, context, page=0)
        elif text == "🧾 উইথড্র রিকুয়েস্ট":
            await check_withdrawal_requests(update, context, page=0)
        elif text == "🔁 সেশন ম্যানেজমেন্ট":
            await admin_session_management(update, context, page=0)
        elif text == "🔔 ব্রডকাস্ট":
            if user_id == SUPER_ADMIN_ID:
                await update.message.reply_text("আপনি সকল ইউজারকে যে বার্তা পাঠাতে চান, সেটি লিখুন:")
                return BROADCAST_MESSAGE
            else:
                await update.message.reply_text("❌ শুধুমাত্র সুপার অ্যাডমিন এই ফিচারটি ব্যবহার করতে পারবেন।")
    
    return ConversationHandler.END

# --- WhatsApp লগইন ফ্লো ---
async def ask_phone_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone_number = update.message.text.strip().replace(" ", "") # Remove spaces
    if not phone_number.startswith('+'):
        await update.message.reply_text("❌ অনুগ্রহ করে সঠিক কান্ট্রি কোডসহ নম্বর দিন (যেমন: +8801712345678):")
        return PHONE_NUMBER

    context.user_data['phone_number'] = phone_number
    
    # WhatsApp লগইন শুরু করুন
    qr_url, status = await initiate_whatsapp_login(phone_number)
    
    if status == "authenticated":
        await update.message.reply_text(f"✅ এই নম্বর `{phone_number}` ইতিমধ্যেই লগইন করা আছে।")
        return ConversationHandler.END
    elif qr_url and qr_url.startswith('data:image/png;base64,'):
        # Decode base64 QR data and send as photo
        qr_data = base64.b64decode(qr_url.split(',')[1])
        photo_bytes = io.BytesIO(qr_data)
        
        await update.message.reply_photo(
            photo=photo_bytes,
            caption="নিচের QR কোডটি স্ক্যান করে WhatsApp এ লগইন করুন। স্ক্যান হয়ে গেলে /confirm কমান্ড দিন।"
        )
        return WAIT_FOR_QR_CONFIRMATION
    else:
        await update.message.reply_text("❌ WhatsApp লগইন শুরু করতে সমস্যা হয়েছে অথবা নম্বরটি ভুল। আবার চেষ্টা করুন।")
        return ConversationHandler.END

async def confirm_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone_number = context.user_data.get('phone_number')
    if not phone_number:
        await update.message.reply_text("⚠️ কোনো ফোন নম্বর পাওয়া যায়নি। দয়া করে আবার শুরু করুন।")
        return ConversationHandler.END
    
    # লগইন স্ট্যাটাস চেক করুন
    status = await check_whatsapp_login_status(phone_number)
    
    if status == "authenticated":
        # সেশন ডেটাবেজে সেভ করুন (যদি না থাকে)
        conn = sqlite3.connect("bot_database.db")
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sessions WHERE user_id = ? AND phone_number = ?", 
                       (update.effective_user.id, phone_number))
        session_exists = cursor.fetchone()
        
        if not session_exists:
            cursor.execute(
                "INSERT INTO sessions (user_id, phone_number, session_data) VALUES (?, ?, ?)",
                (update.effective_user.id, phone_number, 'Baileys Managed')
            )
            # পয়েন্ট যোগ করুন
            cursor.execute(
                "UPDATE users SET points = points + ?, successful_sessions = successful_sessions + 1 WHERE user_id = ?",
                (POINTS_PER_LOGIN, update.effective_user.id)
            )
            conn.commit()
            await update.message.reply_text("✅ WhatsApp সফলভাবে লগইন হয়েছে! আপনার সেশন সংরক্ষণ করা হয়েছে এবং আপনি পয়েন্ট পেয়েছেন।")
        else:
            await update.message.reply_text("✅ WhatsApp সফলভাবে লগইন হয়েছে এবং সেশনটি ইতিমধ্যেই রেকর্ড করা আছে।")
        
        conn.close()
        
    elif status == "pending_qr":
        await update.message.reply_text("⌛ WhatsApp লগইন এখনও পেন্ডিং আছে। QR কোড স্ক্যান নিশ্চিত করুন এবং কিছুক্ষণ পর আবার /confirm দিন।")
        return WAIT_FOR_QR_CONFIRMATION # Stay in this state
    else:
        # ব্যর্থ লগইন
        conn = sqlite3.connect("bot_database.db")
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET failed_sessions = failed_sessions + 1 WHERE user_id = ?",
            (update.effective_user.id,)
        )
        conn.commit()
        conn.close()
        await update.message.reply_text("❌ WhatsApp লগইন ব্যর্থ হয়েছে বা সেশন পাওয়া যায়নি। আবার চেষ্টা করুন।")
    
    context.user_data.pop('phone_number', None) # Clear user data
    return ConversationHandler.END

# --- Account Management ---
async def my_account(update, context):
    user_id = update.effective_user.id
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT points, successful_sessions, failed_sessions, referral_code FROM users WHERE user_id = ?", (user_id,))
    user_data = cursor.fetchone()
    cursor.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ? AND status = 'active'", (user_id,))
    session_count = cursor.fetchone()[0]
    conn.close()
    
    if user_data:
        text = (
            f"📊 **আপনার একাউন্টের বিস্তারিত** 📊\n\n"
            f"💰 **পয়েন্ট ব্যালেন্স:** `{user_data[0]}`\n"
            f"🔗 **সক্রিয় সেশন:** `{session_count}` টি\n"
            f"✅ **সফল সেশন:** `{user_data[1]}` বার\n" # Renamed from successful_otp
            f"❌ **ব্যর্থ সেশন:** `{user_data[2]}` বার\n\n" # Renamed from failed_otp
            f"🎁 **আপনার রেফার কোড:**\n`{user_data[3]}`"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def get_referral_code(update, context):
    user_id = update.effective_user.id
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT referral_code FROM users WHERE user_id = ?", (user_id,))
    referral_code = cursor.fetchone()[0]
    conn.close()
    
    await update.message.reply_text(
        f"🎁 আপনার রেফারেল কোড:\n\n"
        f"`{referral_code}`\n\n"
        f"এই কোডটি শেয়ার করুন এবং নতুন ইউজার রেজিস্ট্রেশনের সময় ব্যবহার করুন। প্রতিটি সফল রেফারেলের জন্য আপনি {POINTS_PER_REFERRAL} পয়েন্ট পাবেন।",
        parse_mode=ParseMode.MARKDOWN
    )

# --- Withdrawal System ---
async def start_withdraw_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT points FROM users WHERE user_id = ?", (user_id,))
    user_points = cursor.fetchone()[0]
    conn.close()
    
    available_bdt = user_points / POINTS_TO_BDT_RATE
    await update.message.reply_text(
        f"💰 আপনার ব্যালেন্স: {user_points} পয়েন্ট ({available_bdt:.2f} BDT)\n\n"
        f"উইথড্র করতে চাইলে {MIN_WITHDRAWAL_BDT} BDT এর সমান বা বেশি পয়েন্ট থাকতে হবে।\n"
        "উইথড্র করার পরিমাণ লিখুন (BDT তে):"
    )
    return WITHDRAW_AMOUNT

async def ask_withdraw_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        amount_bdt = float(update.message.text)
        if amount_bdt < MIN_WITHDRAWAL_BDT:
            await update.message.reply_text(
                f"❌ নূন্যতম উইথড্র পরিমাণ: {MIN_WITHDRAWAL_BDT} BDT\n"
                "আবার চেষ্টা করুন:"
            )
            return WITHDRAW_AMOUNT
        
        # পয়েন্ট চেক করুন
        user_id = update.effective_user.id
        required_points = amount_bdt * POINTS_TO_BDT_RATE
        
        conn = sqlite3.connect("bot_database.db")
        cursor = conn.cursor()
        cursor.execute("SELECT points FROM users WHERE user_id = ?", (user_id,))
        user_points = cursor.fetchone()[0]
        
        if user_points < required_points:
            await update.message.reply_text(
                f"❌ আপনার কাছে পর্যাপ্ত পয়েন্ট নেই!\n"
                f"প্রয়োজন: {required_points} পয়েন্ট, আপনার আছে: {user_points} পয়েন্ট\n"
                "আবার চেষ্টা করুন:"
            )
            return WITHDRAW_AMOUNT
        
        context.user_data['withdraw_amount'] = amount_bdt
        context.user_data['required_points'] = required_points
        
        await update.message.reply_text("📱 টাকা গ্রহণ করার জন্য আপনার বিকাশ/নগদ/রকেট নম্বরটি দিন:")
        return WITHDRAW_NUMBER
    except ValueError:
        await update.message.reply_text("❌ ভুল ইনপুট! শুধুমাত্র সংখ্যা লিখুন।\nআবার চেষ্টা করুন:")
        return WITHDRAW_AMOUNT

async def process_withdraw_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    payment_number = update.message.text
    amount_bdt = context.user_data['withdraw_amount']
    required_points = context.user_data['required_points']
    user_id = update.effective_user.id
    
    # ডাটাবেজে রিকোয়েস্ট সেভ করুন
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    
    # উইথড্র রিকোয়েস্ট যোগ করুন
    cursor.execute(
        "INSERT INTO withdrawals (user_id, amount_bdt, points_used, payment_number) VALUES (?, ?, ?, ?)",
        (user_id, amount_bdt, required_points, payment_number)
    )
    
    # পয়েন্ট কেটে নিন
    cursor.execute(
        "UPDATE users SET points = points - ? WHERE user_id = ?",
        (required_points, user_id)
    )
    
    conn.commit()
    conn.close()
    
    await update.message.reply_text(
        "✅ আপনার উইথড্র রিকোয়েস্ট গৃহীত হয়েছে!\n"
        "অ্যাডমিনের অনুমোদনের পর ২৪ ঘণ্টার মধ্যে টাকা পেয়ে যাবেন।"
    )
    
    # অ্যাডমিনদের নোটিফাই করুন
    for admin_id in ALL_ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"⚠️ নতুন উইথড্র রিকোয়েস্ট!\n"
                     f"ইউজার: {update.effective_user.username or update.effective_user.id}\n"
                     f"পরিমাণ: {amount_bdt} BDT\n"
                     f"নম্বর: {payment_number}"
            )
        except Exception:
            logger.warning(f"Admin {admin_id} কে নোটিফাই করতে ব্যর্থ")
    
    return ConversationHandler.END

# --- Active Sessions ---
async def list_active_sessions(update, context):
    user_id = update.effective_user.id
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT phone_number, created_at FROM sessions WHERE user_id = ? AND status = 'active'", (user_id,))
    sessions = cursor.fetchall()
    conn.close()
    
    if not sessions:
        await update.message.reply_text("আপনার কোনো সক্রিয় সেশন নেই।")
        return
    
    text = "📱 **আপনার সক্রিয় সেশনসমূহ:**\n\n"
    for i, session in enumerate(sessions, 1):
        status = await check_whatsapp_login_status(session[0])
        text += f"{i}. `{session[0]}` - {session[1]} (স্ট্যাটাস: {status})\n"
    
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

# --- Admin Features ---
async def list_all_users(update, context, page=0):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, username, points FROM users")
    users = cursor.fetchall()
    conn.close()

    if not users:
        await update.message.reply_text("কোনো ইউজার পাওয়া যায়নি।")
        return

    user_list = [(f"{user[1]} (ID: {user[0]}, Points: {user[2]})", user[0]) for user in users]
    
    reply_markup = build_paginated_menu(user_list, "admin_users_page", page)
    message = update.message if hasattr(update, 'message') else update.callback_query.message
    await message.reply_text(f"👥 **ইউজার লিস্ট (পেজ {page+1})**", reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

async def check_withdrawal_requests(update, context, page=0):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT request_id, user_id, amount_bdt, payment_number FROM withdrawals WHERE status = 'pending'")
    requests = cursor.fetchall()
    conn.close()

    if not requests:
        message = update.message if hasattr(update, 'message') else update.callback_query.message
        await message.reply_text("✅ কোনো পেন্ডিং উইথড্র রিকুয়েস্ট নেই।")
        return

    for req in requests:
        text = (f"🆔 রিকুয়েস্ট ID: `{req[0]}`\n"
                f"👤 ইউজার ID: `{req[1]}`\n"
                f"💰 পরিমাণ: `{req[2]}` BDT\n"
                f"📱 নম্বর: `{req[3]}`")
        keyboard = [[
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_{req[0]}"),
            InlineKeyboardButton("❌ Decline", callback_data=f"decline_{req[0]}")
        ]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        message = update.message if hasattr(update, 'message') else update.callback_query.message
        await message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

async def handle_withdrawal(query, context, request_id, status):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    
    # স্ট্যাটাস আপডেট করুন
    cursor.execute(
        "UPDATE withdrawals SET status = ? WHERE request_id = ?",
        (status, request_id)
    )
    
    if status == 'approved':
        # ইউজারকে নোটিফাই করুন
        cursor.execute("SELECT user_id, amount_bdt FROM withdrawals WHERE request_id = ?", (request_id,))
        result = cursor.fetchone()
        user_id, amount = result[0], result[1]
        
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"✅ আপনার `{amount}` BDT এর উইথড্র রিকুয়েস্ট অনুমোদিত হয়েছে!\n"
                     "২৪ ঘণ্টার মধ্যে টাকা পেয়ে যাবেন।"
            )
        except Exception as e:
            logger.error(f"User {user_id} কে নোটিফাই করতে ব্যর্থ: {e}")
    else: # declined
         # Optionally refund points if declined
        cursor.execute("SELECT user_id, points_used FROM withdrawals WHERE request_id = ?", (request_id,))
        result = cursor.fetchone()
        user_id, points_used = result[0], result[1]
        cursor.execute("UPDATE users SET points = points + ? WHERE user_id = ?", (points_used, user_id))
        
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"❌ আপনার `{amount}` BDT এর উইথড্র রিকুয়েস্ট বাতিল করা হয়েছে। ব্যবহৃত পয়েন্ট (`{points_used}`) আপনার অ্যাকাউন্টে ফেরত দেওয়া হয়েছে।"
            )
        except Exception as e:
            logger.error(f"User {user_id} কে নোটিফাই করতে ব্যর্থ: {e}")

    conn.commit()
    conn.close()
    
    await query.message.edit_text(f"✅ রিকুয়েস্ট `{request_id}` সফলভাবে `{status}` করা হয়েছে!", parse_mode=ParseMode.MARKDOWN)


async def admin_session_management(update, context, page=0):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    # Fetch all distinct phone numbers associated with user sessions
    cursor.execute("SELECT DISTINCT phone_number, user_id FROM sessions")
    sessions_db = cursor.fetchall()
    conn.close()

    if not sessions_db:
        message = update.message if hasattr(update, 'message') else update.callback_query.message
        await message.reply_text("কোনো সেভ করা সেশন নেই।")
        return

    # For display, get associated username
    session_list = []
    for phone, user_id in sessions_db:
        conn = sqlite3.connect("bot_database.db")
        cursor = conn.cursor()
        cursor.execute("SELECT username FROM users WHERE user_id = ?", (user_id,))
        username = cursor.fetchone()
        conn.close()
        username_display = username[0] if username else f"User {user_id}"
        session_list.append((f"{phone} ({username_display})", phone))
    
    reply_markup = build_paginated_menu(session_list, "admin_session_page", page)
    message = update.message if hasattr(update, 'message') else update.callback_query.message
    await message.reply_text(f"🔁 **সেশন ম্যানেজমেন্ট (পেজ {page+1})**\n\nসেশন অ্যাকশনের জন্য একটি নম্বর বেছে নিন:", reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    return ADMIN_SESSION_ACTION

async def admin_select_session_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    phone_number = context.user_data.get('admin_selected_phone') # This will be set by the button_handler
    if not phone_number:
        await query.message.reply_text("⚠️ কোনো ফোন নম্বর নির্বাচন করা হয়নি। আবার চেষ্টা করুন।")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton("📊 স্ট্যাটাস চেক", callback_data=f"admin_session_status_{phone_number}")],
        [InlineKeyboardButton("❌ লগআউট", callback_data=f"admin_session_logout_{phone_number}")],
        [InlineKeyboardButton("↩️ মেনুতে ফিরে যান", callback_data="admin_session_cancel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"আপনি `{phone_number}` নম্বরটি নির্বাচন করেছেন। কি করতে চান?",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )
    return ADMIN_SESSION_ACTION # Stay in this state to handle further actions

async def admin_perform_session_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data.split('_')
    action = data[2] # e.g., 'status' or 'logout'
    phone_number = data[3]

    if action == "status":
        status = await check_whatsapp_login_status(phone_number)
        await query.edit_message_text(f"`{phone_number}` নম্বরের স্ট্যাটাস: `{status}`", parse_mode=ParseMode.MARKDOWN)
    elif action == "logout":
        success = await terminate_whatsapp_session(phone_number)
        if success:
            conn = sqlite3.connect("bot_database.db")
            cursor = conn.cursor()
            cursor.execute("UPDATE sessions SET status = 'inactive' WHERE phone_number = ?", (phone_number,))
            conn.commit()
            conn.close()
            await query.edit_message_text(f"✅ `{phone_number}` সেশনটি সফলভাবে লগআউট করা হয়েছে।", parse_mode=ParseMode.MARKDOWN)
        else:
            await query.edit_message_text(f"❌ `{phone_number}` সেশনটি লগআউট করতে ব্যর্থ।", parse_mode=ParseMode.MARKDOWN)
    
    # After action, return to main menu or session management
    reply_markup = get_main_keyboard(update.effective_user.id)
    await context.bot.send_message(update.effective_chat.id, "অপারেশন সম্পন্ন।", reply_markup=reply_markup)
    context.user_data.pop('admin_selected_phone', None)
    return ConversationHandler.END

async def broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message.text
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users")
    user_ids = [row[0] for row in cursor.fetchall()]
    conn.close()
    
    success = 0
    failed = 0
    
    for user_id in user_ids:
        try:
            await context.bot.send_message(chat_id=user_id, text=message)
            success += 1
        except Exception:
            failed += 1
    
    await update.message.reply_text(
        f"✅ ব্রডকাস্ট সম্পন্ন!\n\n"
        f"সফল: {success} ইউজার\n"
        f"ব্যর্থ: {failed} ইউজার"
    )
    return ConversationHandler.END

# --- Utility Functions ---
def build_paginated_menu(items, prefix, page):
    start_idx = page * ITEMS_PER_PAGE
    end_idx = start_idx + ITEMS_PER_PAGE
    buttons = []
    
    for item_display, item_value in items[start_idx:end_idx]:
        buttons.append([InlineKeyboardButton(item_display, callback_data=f"{prefix}_select_{item_value}")])
    
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀️ আগের পেজ", callback_data=f"{prefix}_page_{page-1}"))
    if end_idx < len(items):
        nav_buttons.append(InlineKeyboardButton("পরের পেজ ▶️", callback_data=f"{prefix}_page_{page+1}"))
    
    if nav_buttons:
        buttons.append(nav_buttons)
        
    return InlineKeyboardMarkup(buttons)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("অপারেশন বাতিল করা হয়েছে।", reply_markup=get_main_keyboard(update.effective_user.id))
    return ConversationHandler.END

# --- Button Handlers ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split('_')
    
    prefix = data[0] + "_" + data[1] if len(data) > 1 and data[1] in ["users", "session"] else data[0]

    if prefix == "admin_users_page": # Pagination for user list
        page = int(data[3])
        await list_all_users(query, context, page)
    elif prefix == "admin_session_page": # Pagination for session list
        if data[2] == "page":
            page = int(data[3])
            await admin_session_management(query, context, page)
        elif data[2] == "select":
            phone_number = data[3]
            context.user_data['admin_selected_phone'] = phone_number
            await admin_select_session_action(update, context) # Pass update to new handler
            return ADMIN_SESSION_ACTION
    elif prefix == "approve":
        request_id = int(data[1])
        await handle_withdrawal(query, context, request_id, 'approved')
    elif prefix == "decline":
        request_id = int(data[1])
        await handle_withdrawal(query, context, request_id, 'declined')
    elif prefix == "admin_session_status" or prefix == "admin_session_logout":
        await admin_perform_session_action(update, context) # Handle status/logout actions
        return ConversationHandler.END # End the conversation after action
    elif query.data == "admin_session_cancel":
        await query.message.edit_text("সেশন ম্যানেজমেন্ট বাতিল করা হয়েছে।")
        await context.bot.send_message(query.message.chat_id, "প্রধান মেনু:", reply_markup=get_main_keyboard(update.effective_user.id))
        return ConversationHandler.END

def main() -> None:
    setup_database()
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Conversation Handlers
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            PHONE_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone_number)],
            WAIT_FOR_QR_CONFIRMATION: [CommandHandler("confirm", confirm_login)],
            WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_withdraw_number)],
            WITHDRAW_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_withdraw_request)],
            BROADCAST_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_message)],
            ADMIN_SESSION_ACTION: [CallbackQueryHandler(button_handler)], # Handle actions within this state
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    
    # Add handlers
    application.add_handler(conv_handler)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, main_menu_handler))
    application.add_handler(CallbackQueryHandler(button_handler)) # General button handler for non-conversation states

    logger.info("বট সফলভাবে চালু হয়েছে...")
    application.run_polling()

if __name__ == "__main__":
    main()
