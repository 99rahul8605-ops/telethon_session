"""
Telegram Session String Generator Bot (Telethon + Pyrogram edition)
----------------------------------------------------------------------
Flow:
1. /start -> sends a stylish welcome message with an inline
   "Generate Session" button.
2. Tapping the button lets the user choose which library to generate the
   session string for: Telethon or Pyrogram.
3. Asks for API_ID, with an inline "Skip (use default)" button for users who
   don't want to provide their own API_ID/API_HASH (requires the bot owner to
   configure defaults).
4. If not skipped -> asks for API_HASH.
5. Asks for the phone number.
6. Sends an OTP to that number via Telegram.
7. Asks the user to enter the OTP with a SPACE between every digit
   (e.g. "1 2 3 4 5") — this prevents Telegram's client from auto
   invalidating the code when it detects a raw login-code pattern being
   forwarded/pasted into a chat.
8. If the account has Two-Step Verification (2FA) enabled, asks for the
   password.
9. Generates the session string (Telethon StringSession or Pyrogram session
   string, depending on what the user picked) and sends it, then cleans up
   the temporary client.

Run:
    pip install -r requirements.txt
    export BOT_TOKEN="123456:ABC-your-bot-token"
    # Optional, only used when the user taps "Skip":
    export DEFAULT_API_ID="12345"
    export DEFAULT_API_HASH="0123456789abcdef0123456789abcdef"
    python bot.py

WARNING: A session string grants full access to the Telegram account it was
generated for. Never share it publicly, and only run this bot on
infrastructure you trust — the bot itself will see every session string it
generates.
"""
from dotenv import load_dotenv
import os

load_dotenv()
import asyncio
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# Python 3.14 removed asyncio's implicit "create a loop if none exists"
# behavior (asyncio.get_event_loop() now raises instead). Pyrogram's
# sync.py calls asyncio.get_event_loop() at import time, which crashes the
# whole process on 3.14+ unless a loop already exists in this thread. This
# creates one up front so the import succeeds regardless of Python version.
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ---- Telethon ----
from telethon import TelegramClient
from telethon.errors import (
    ApiIdInvalidError,
    FloodWaitError as TelethonFloodWaitError,
    PasswordHashInvalidError as TelethonPasswordHashInvalidError,
    PhoneCodeInvalidError as TelethonPhoneCodeInvalidError,
    PhoneNumberInvalidError as TelethonPhoneNumberInvalidError,
    SessionPasswordNeededError as TelethonSessionPasswordNeededError,
)
from telethon.sessions import StringSession

# ---- Pyrogram ----
from pyrogram import Client as PyrogramClient
from pyrogram.errors import (
    ApiIdInvalid as PyroApiIdInvalid,
    FloodWait as PyroFloodWait,
    PasswordHashInvalid as PyroPasswordHashInvalid,
    PhoneCodeInvalid as PyroPhoneCodeInvalid,
    PhoneNumberInvalid as PyroPhoneNumberInvalid,
    SessionPasswordNeeded as PyroSessionPasswordNeeded,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DEFAULT_API_ID = os.environ.get("DEFAULT_API_ID")
DEFAULT_API_HASH = os.environ.get("DEFAULT_API_HASH")

# Render (and most PaaS providers) expect a Web Service to bind to $PORT and
# respond to HTTP requests, otherwise the deploy is marked unhealthy/failed —
# even though this bot doesn't actually need to serve web traffic. This tiny
# server exists purely to satisfy that port-detection / health-check.
PORT = int(os.environ.get("PORT", 8080))


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK - Session Generator Bot is running")

    def log_message(self, format, *args):  # noqa: A002 - silence default access logs
        pass


def start_health_server() -> None:
    server = HTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
    logger.info("Health check server listening on 0.0.0.0:%s", PORT)
    server.serve_forever()

# Conversation states
MENU, CHOOSE_LIB, API_ID, API_HASH, PHONE, OTP, PASSWORD = range(7)

WELCOME_TEXT = (
    "✨ *Welcome to Session Generator Bot* ✨\n\n"
    "🔐 I can generate a secure *session string* for your Telegram account "
    "in just a few simple steps — your choice of *Telethon* or *Pyrogram*.\n\n"
    "⚡ *What you get:*\n"
    "  •  Fast & guided setup\n"
    "  •  Safe OTP handling\n"
    "  •  Full 2FA support\n\n"
    "Tap the button below to begin 👇"
)


def generate_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🚀 Generate Session", callback_data="generate")]]
    )


def library_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🐍 Telethon", callback_data="lib_telethon"),
                InlineKeyboardButton("🔥 Pyrogram", callback_data="lib_pyrogram"),
            ]
        ]
    )


def skip_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⏭️ Skip (use default)", callback_data="skip_api")]]
    )


LIB_LABELS = {"telethon": "🐍 Telethon", "pyrogram": "🔥 Pyrogram"}


# --------------------------------------------------------------------------
# Library-agnostic helpers: each returns/accepts a plain (client) object and
# hides whether we're driving Telethon or Pyrogram underneath.
# --------------------------------------------------------------------------

async def create_and_send_code(lib: str, api_id: int, api_hash: str, phone: str):
    """Connect a fresh client and request a login code. Returns (client, phone_code_hash)."""
    if lib == "telethon":
        client = TelegramClient(StringSession(), api_id, api_hash)
        await client.connect()
        sent = await client.send_code_request(phone)
        return client, sent.phone_code_hash
    else:  # pyrogram
        client = PyrogramClient(
            name="temp_session", api_id=api_id, api_hash=api_hash, in_memory=True
        )
        await client.connect()
        sent = await client.send_code(phone)
        return client, sent.phone_code_hash


async def sign_in_with_code(lib: str, client, phone: str, code: str, phone_code_hash: str):
    if lib == "telethon":
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
    else:  # pyrogram
        await client.sign_in(
            phone_number=phone, phone_code_hash=phone_code_hash, phone_code=code
        )


async def sign_in_with_password(lib: str, client, password: str):
    if lib == "telethon":
        await client.sign_in(password=password)
    else:  # pyrogram
        await client.check_password(password)


async def export_session_string(lib: str, client) -> str:
    if lib == "telethon":
        return client.session.save()
    else:  # pyrogram
        return await client.export_session_string()


async def disconnect_client(lib: str, client):
    try:
        if lib == "telethon":
            await client.disconnect()
        else:
            await client.disconnect()
    except Exception:
        pass


def flood_wait_seconds(lib: str, error) -> int:
    if lib == "telethon":
        return error.seconds
    return getattr(error, "value", 0)


# --------------------------------------------------------------------------
# OTP timeout: if the user doesn't enter the code within 5 minutes, the
# in-progress login is reset for security (a half-finished Telegram login
# left open indefinitely is a bad idea).
# --------------------------------------------------------------------------

OTP_TIMEOUT_SECONDS = 5 * 60


def _otp_job_name(user_id: int) -> str:
    return f"otp_timeout_{user_id}"


def cancel_otp_timeout(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    if not context.job_queue:
        return
    for job in context.job_queue.get_jobs_by_name(_otp_job_name(user_id)):
        job.schedule_removal()


def schedule_otp_timeout(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> None:
    if not context.job_queue:
        logger.warning("job_queue is not available — OTP timeout will not be enforced.")
        return
    cancel_otp_timeout(context, user_id)
    context.job_queue.run_once(
        otp_timeout_job,
        when=OTP_TIMEOUT_SECONDS,
        chat_id=chat_id,
        user_id=user_id,
        name=_otp_job_name(user_id),
    )


async def otp_timeout_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    user_data = context.user_data
    client = user_data.get("client") if user_data else None
    lib = user_data.get("lib") if user_data else None

    if client and lib:
        await disconnect_client(lib, client)
    if user_data:
        user_data.clear()

    await context.bot.send_message(
        chat_id=context.job.chat_id,
        text=(
            "⏰ *Session Expired*\n\n"
            "You didn't enter the OTP within 5 minutes, so this session "
            "generation has been reset for your account's security.\n\n"
            "Send /start to begin again."
        ),
        parse_mode="Markdown",
    )


async def abort_and_restart(
    update: Update, context: ContextTypes.DEFAULT_TYPE, reason: str
) -> int:
    """Disconnects any in-progress client, wipes state, and tells the user to
    /start over. Used for every 'wrong input' case: bad OTP, bad 2FA
    password, invalid API_ID/API_HASH, etc."""
    user_id = update.effective_user.id
    cancel_otp_timeout(context, user_id)

    client = context.user_data.get("client")
    lib = context.user_data.get("lib")
    if client and lib:
        await disconnect_client(lib, client)
    context.user_data.clear()

    text = (
        f"❌ *{reason}*\n\n"
        "For your account's security, this session generation has been reset.\n\n"
        "Send /start to begin again."
    )
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.message.reply_text(text, parse_mode="Markdown")

    return ConversationHandler.END


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Clean up anything left over from a previous, abandoned attempt.
    old_client = context.user_data.get("client")
    old_lib = context.user_data.get("lib")
    if old_client and old_lib:
        await disconnect_client(old_lib, old_client)
    cancel_otp_timeout(context, update.effective_user.id)
    context.user_data.clear()

    await update.message.reply_text(
        WELCOME_TEXT,
        parse_mode="Markdown",
        reply_markup=generate_button(),
    )
    return MENU


async def choose_library(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🧰 *Choose a library*\n\n"
        "Which library do you want the session string for?",
        parse_mode="Markdown",
        reply_markup=library_buttons(),
    )
    return CHOOSE_LIB


async def lib_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lib = "telethon" if query.data == "lib_telethon" else "pyrogram"
    context.user_data["lib"] = lib

    await query.edit_message_text(
        f"✅ Library selected: *{LIB_LABELS[lib]}*\n\n"
        "🧩 *Step 1/4 — API Credentials*\n\n"
        "Please send your *API_ID*.\n"
        "Don't have one? Get it free at my.telegram.org, or tap *Skip* below "
        "to use this bot's default credentials (if configured).",
        parse_mode="Markdown",
        reply_markup=skip_button(),
    )
    return API_ID


async def skip_api(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if not DEFAULT_API_ID or not DEFAULT_API_HASH:
        await query.edit_message_text(
            "⚠️ No default API_ID/API_HASH is configured on this bot.\n\n"
            "Please send your own *API_ID* to continue "
            "(get one free at my.telegram.org).",
            parse_mode="Markdown",
        )
        return API_ID

    context.user_data["api_id"] = int(DEFAULT_API_ID)
    context.user_data["api_hash"] = DEFAULT_API_HASH
    await query.edit_message_text(
        "✅ Using the bot's default API_ID/API_HASH.\n\n"
        "📱 *Step 2/4 — Phone Number*\n"
        "Send your phone number with country code, e.g. `+919876543210`",
        parse_mode="Markdown",
    )
    return PHONE


async def get_api_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text(
            "❌ API_ID must be a number. Please try again, or tap Skip above.",
        )
        return API_ID

    context.user_data["api_id"] = int(text)
    await update.message.reply_text(
        "👍 Got it.\n\nNow send your *API_HASH*.", parse_mode="Markdown"
    )
    return API_HASH


async def get_api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["api_hash"] = update.message.text.strip()
    await update.message.reply_text(
        "✅ API credentials saved.\n\n"
        "📱 *Step 2/4 — Phone Number*\n"
        "Send your phone number with country code, e.g. `+919876543210`",
        parse_mode="Markdown",
    )
    return PHONE


async def get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone = update.message.text.strip()
    lib = context.user_data["lib"]
    api_id = context.user_data["api_id"]
    api_hash = context.user_data["api_hash"]

    status_msg = await update.message.reply_text("⏳ Sending OTP, please wait...")

    try:
        client, phone_code_hash = await create_and_send_code(lib, api_id, api_hash, phone)
    except (ApiIdInvalidError, PyroApiIdInvalid):
        await status_msg.edit_text(
            "❌ *Invalid API_ID / API_HASH*\n\n"
            "For your account's security, this session generation has been reset.\n\n"
            "Send /start to begin again.",
            parse_mode="Markdown",
        )
        context.user_data.clear()
        return ConversationHandler.END
    except (TelethonPhoneNumberInvalidError, PyroPhoneNumberInvalid):
        await status_msg.edit_text(
            "❌ That phone number looks invalid. Please send it again with country code."
        )
        return PHONE
    except (TelethonFloodWaitError, PyroFloodWait) as e:
        seconds = flood_wait_seconds(lib, e)
        await status_msg.edit_text(
            f"⏳ Too many attempts. Telegram asks you to wait {seconds}s, then /start again."
        )
        context.user_data.clear()
        return ConversationHandler.END

    context.user_data["client"] = client
    context.user_data["phone"] = phone
    context.user_data["phone_code_hash"] = phone_code_hash

    # Reset if the OTP isn't entered within 5 minutes.
    schedule_otp_timeout(context, update.effective_chat.id, update.effective_user.id)

    await status_msg.edit_text(
        "📩 *Step 3/4 — Enter OTP*\n\n"
        "A login code has been sent to your Telegram account.\n\n"
        "⚠️ *Important:* To stop Telegram from auto-invalidating the code, "
        "type it back with a *space between every digit*.\n\n"
        "Example — if the code is `12345`, send it as:\n"
        "`1 2 3 4 5`\n\n"
        "⏳ You have *5 minutes* to enter it, after which this session "
        "generation will automatically reset.",
        parse_mode="Markdown",
    )
    return OTP


async def get_otp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if "client" not in context.user_data:
        await update.message.reply_text(
            "⚠️ This session generation has expired or was reset. Send /start to begin again."
        )
        return ConversationHandler.END

    raw = update.message.text.strip()
    code = raw.replace(" ", "")

    if not code.isdigit():
        await update.message.reply_text(
            "❌ That doesn't look like a valid code.\n"
            "Please resend it with spaces between digits, e.g. `1 2 3 4 5`",
            parse_mode="Markdown",
        )
        return OTP

    lib = context.user_data["lib"]
    client = context.user_data["client"]
    phone = context.user_data["phone"]
    phone_code_hash = context.user_data["phone_code_hash"]

    try:
        await sign_in_with_code(lib, client, phone, code, phone_code_hash)
    except (TelethonPhoneCodeInvalidError, PyroPhoneCodeInvalid):
        return await abort_and_restart(update, context, "Wrong OTP entered")
    except (TelethonSessionPasswordNeededError, PyroSessionPasswordNeeded):
        cancel_otp_timeout(context, update.effective_user.id)
        await update.message.reply_text(
            "🔒 *Step 4/4 — Two-Step Verification*\n\n"
            "Your account has 2FA enabled. Please send your password.",
            parse_mode="Markdown",
        )
        return PASSWORD

    cancel_otp_timeout(context, update.effective_user.id)
    return await finish(update, context)


async def get_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if "client" not in context.user_data:
        await update.message.reply_text(
            "⚠️ This session generation has expired or was reset. Send /start to begin again."
        )
        return ConversationHandler.END

    password = update.message.text
    lib = context.user_data["lib"]
    client = context.user_data["client"]

    try:
        await sign_in_with_password(lib, client, password)
    except (TelethonPasswordHashInvalidError, PyroPasswordHashInvalid):
        return await abort_and_restart(update, context, "Wrong 2FA password entered")

    return await finish(update, context)


async def finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cancel_otp_timeout(context, update.effective_user.id)
    lib = context.user_data["lib"]
    client = context.user_data["client"]
    session_string = await export_session_string(lib, client)

    await update.message.reply_text(
        "🎉 *Login Successful!*\n\n"
        f"Here is your *{LIB_LABELS[lib]}* session string:\n\n"
        f"`{session_string}`\n\n"
        "⚠️ *Keep this secret* — anyone with this string has full access to "
        "your account. Never share it publicly.\n\n"
        "Send /start to generate another one.",
        parse_mode="Markdown",
    )

    await disconnect_client(lib, client)
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cancel_otp_timeout(context, update.effective_user.id)
    client = context.user_data.get("client")
    lib = context.user_data.get("lib")
    if client and lib:
        await disconnect_client(lib, client)
    context.user_data.clear()
    await update.message.reply_text("🚫 Cancelled. Send /start to begin again.")
    return ConversationHandler.END


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Please set the BOT_TOKEN environment variable.")

    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MENU: [CallbackQueryHandler(choose_library, pattern="^generate$")],
            CHOOSE_LIB: [
                CallbackQueryHandler(lib_selected, pattern="^lib_(telethon|pyrogram)$")
            ],
            API_ID: [
                CallbackQueryHandler(skip_api, pattern="^skip_api$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_api_id),
            ],
            API_HASH: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_api_hash)],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_phone)],
            OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_otp)],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_password)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
        ],
    )

    app.add_handler(conv)

    # Start the health-check HTTP server in a background thread so Render
    # can detect an open port, while the bot itself keeps polling Telegram.
    threading.Thread(target=start_health_server, daemon=True).start()

    logger.info("Bot started. Polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
