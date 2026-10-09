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
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
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

from account_manager import AccountManager

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

# Bulk reader uses fixed app credentials so it can open the Telethon SQLite
# sessions exported by Server 1. Prefer dedicated READER_* values, falling
# back to the bot's existing defaults.
READER_API_ID = (os.environ.get("READER_API_ID") or DEFAULT_API_ID or "").strip()
READER_API_HASH = (os.environ.get("READER_API_HASH") or DEFAULT_API_HASH or "").strip()
READER_MAX_ACCOUNTS = max(2, int(os.environ.get("READER_MAX_ACCOUNTS", "50")))
READER_MAX_ZIP_BYTES = int(os.environ.get("READER_MAX_ZIP_BYTES", str(50 * 1024 * 1024)))
READER_MAX_UNCOMPRESSED_BYTES = int(os.environ.get("READER_MAX_UNCOMPRESSED_BYTES", str(120 * 1024 * 1024)))
READER_AUTO_DISCONNECT_SECONDS = max(
    60, int(os.environ.get("READER_AUTO_DISCONNECT_SECONDS", "600"))
)


# Render (and most PaaS providers) expect a Web Service to bind to $PORT and
# respond to HTTP requests, otherwise the deploy is marked unhealthy/failed —
# even though this bot doesn't actually need to serve web traffic. This tiny
# server exists purely to satisfy that port-detection / health-check.
PORT = int(os.environ.get("PORT", 8080))


class HealthCheckHandler(BaseHTTPRequestHandler):
    """Tiny HTTP endpoint used by Render and uptime monitors."""

    def _send_health(self, include_body: bool = True) -> None:
        if self.path not in ("/", "/health", "/healthz"):
            body = b'{"status":"not_found"}'
            self.send_response(404)
        else:
            body = b'{"status":"ok","service":"telegram-session-bot"}'
            self.send_response(200)

        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def do_GET(self):
        self._send_health(include_body=True)

    def do_HEAD(self):
        self._send_health(include_body=False)

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
    "Tap the button below to begin 👇\n\n"
    "📦 Send /read to read ZIP file."
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



# ============================================================================
# Server 1 Bulk Session ZIP Reader
# ============================================================================

reader_application = None
reader_account_manager = None
reader_batches = {}      # user_id -> in-memory batch state
reader_locks = {}        # user_id -> asyncio.Lock
reader_last_otp = {}     # (user_id, phone) -> (otp, timestamp)
reader_auto_disconnect_tasks = {}  # user_id -> asyncio.Task


def _reader_normalize_phone(value: str) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _reader_lock(user_id: int):
    return reader_locks.setdefault(int(user_id), asyncio.Lock())


def _reader_credentials_ok() -> bool:
    return READER_API_ID.isdigit() and bool(READER_API_HASH)


def _reader_keyboard(phone: str) -> InlineKeyboardMarkup:
    """OTP controls; session management appears only after OTP arrival."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Request New OTP", callback_data=f"read_rereq:{phone}"),
            InlineKeyboardButton("📱 Manage Sessions", callback_data=f"read_sessions:{phone}"),
        ],
        [
            InlineKeyboardButton("🔌 Disconnect Reader", callback_data="read_disconnect_all")
        ],
    ])


def _reader_waiting_keyboard() -> InlineKeyboardMarkup:
    """Before OTP arrival: allow skip/disconnect, but no Manage Sessions."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏭️ Skip Number", callback_data="read_skip")
        ],
        [
            InlineKeyboardButton("🔌 Disconnect Reader", callback_data="read_disconnect_all")
        ],
        [
            InlineKeyboardButton("⏹ Stop Batch", callback_data="read_stop")
        ],
    ])


def _reader_loaded_keyboard() -> InlineKeyboardMarkup:
    """Shown immediately after the ZIP has been accepted."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔌 Disconnect Reader", callback_data="read_disconnect_all")
    ]])


def _reader_manifest_from_text(text: str):
    lines = [line.strip() for line in text.splitlines()]
    first_nonempty = next((line for line in lines if line), "")
    if first_nonempty != "Bulk Session Delivery":
        raise ValueError("This is not a Server 1 bulk session package.")

    order = []
    twofa_by_phone = {}
    for line in lines:
        if "|" not in line:
            continue
        left, right = line.split("|", 1)
        phone = _reader_normalize_phone(left)
        if not (7 <= len(phone) <= 15):
            continue
        if phone not in twofa_by_phone:
            order.append(phone)
        twofa = right.strip()
        if twofa.casefold() in {"not set", "none", "n/a", "na", "-", ""}:
            twofa = None
        twofa_by_phone[phone] = twofa

    if not order:
        raise ValueError("accounts.txt does not contain any account rows.")
    return order, twofa_by_phone


def _reader_unpack_zip(zip_path: str, user_id: int):
    """Validate a Server 1 package and copy only accounts.txt + .session files."""
    temp_dir = Path(tempfile.mkdtemp(prefix=f"server1_reader_{int(user_id)}_"))
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            infos = [info for info in zf.infolist() if not info.is_dir()]
            if len(infos) > READER_MAX_ACCOUNTS + 20:
                raise ValueError("ZIP contains too many files.")

            total = sum(max(0, info.file_size) for info in infos)
            if total > READER_MAX_UNCOMPRESSED_BYTES:
                raise ValueError("ZIP is too large after extraction.")

            manifest_info = next(
                (info for info in infos if Path(info.filename).name.casefold() == "accounts.txt"),
                None,
            )
            if not manifest_info:
                raise ValueError("accounts.txt is missing from the ZIP.")
            if manifest_info.file_size > 512 * 1024:
                raise ValueError("accounts.txt is unexpectedly large.")

            manifest_text = zf.read(manifest_info).decode("utf-8", errors="replace")
            order, twofa_by_phone = _reader_manifest_from_text(manifest_text)

            session_infos = {}
            for info in infos:
                base = Path(info.filename).name
                if not base.lower().endswith(".session"):
                    continue
                if info.file_size <= 0 or info.file_size > 10 * 1024 * 1024:
                    continue
                phone = _reader_normalize_phone(Path(base).stem)
                if not phone:
                    continue
                session_infos.setdefault(phone, info)

            items = []
            for phone in order:
                info = session_infos.get(phone)
                if not info:
                    continue
                out_path = temp_dir / f"{phone}.session"
                out_path.write_bytes(zf.read(info))
                items.append({
                    "phone": phone,
                    "twofa_password": twofa_by_phone.get(phone),
                    "session_path": str(out_path),
                    "status": "queued",
                })
                if len(items) >= READER_MAX_ACCOUNTS:
                    break

            if not items:
                raise ValueError("No matching .session files were found for accounts.txt.")

            return temp_dir, items
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def _reader_cancel_auto_disconnect(user_id: int):
    user_id = int(user_id)
    task = reader_auto_disconnect_tasks.pop(user_id, None)
    if task and task is not asyncio.current_task() and not task.done():
        task.cancel()


async def _reader_cleanup_user(user_id: int, *, cancel_timer: bool = True):
    global reader_account_manager
    user_id = int(user_id)

    if cancel_timer:
        _reader_cancel_auto_disconnect(user_id)

    batch = reader_batches.pop(user_id, None)

    if reader_account_manager:
        await reader_account_manager.remove_owner(user_id)

    if batch:
        temp_dir = batch.get("temp_dir")
        if temp_dir:
            shutil.rmtree(str(temp_dir), ignore_errors=True)

    for key in list(reader_last_otp):
        if key[0] == user_id:
            reader_last_otp.pop(key, None)


async def _reader_auto_disconnect_job(user_id: int, batch_id: str):
    user_id = int(user_id)
    try:
        await asyncio.sleep(READER_AUTO_DISCONNECT_SECONDS)

        batch = reader_batches.get(user_id)
        if not batch or batch.get("batch_id") != batch_id:
            return

        await _reader_cleanup_user(user_id, cancel_timer=False)

        if reader_application:
            minutes = max(1, READER_AUTO_DISCONNECT_SECONDS // 60)
            await reader_application.bot.send_message(
                user_id,
                "🔌 <b>Reader Disconnected</b>\n\n"
                f"The reader automatically disconnected after <b>{minutes} minutes</b>.\n"
                "Telegram authorization was not revoked. "
                "Send <code>/read</code> and upload the same ZIP again whenever you want to reconnect.",
                parse_mode="HTML",
            )
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("Reader auto-disconnect failed user=%s", user_id)
    finally:
        current = reader_auto_disconnect_tasks.get(user_id)
        if current is asyncio.current_task():
            reader_auto_disconnect_tasks.pop(user_id, None)


def _reader_schedule_auto_disconnect(user_id: int, batch_id: str):
    _reader_cancel_auto_disconnect(user_id)
    reader_auto_disconnect_tasks[int(user_id)] = asyncio.create_task(
        _reader_auto_disconnect_job(int(user_id), str(batch_id))
    )


async def _reader_ensure_loaded(user_id: int, phone: str):
    batch = reader_batches.get(int(user_id))
    if not batch:
        return False, "Reader batch not found. Upload the ZIP again with /read."

    phone = _reader_normalize_phone(phone)
    item = next((x for x in batch.get("items", []) if x.get("phone") == phone), None)
    if not item:
        return False, "This number is not part of your current reader batch."

    if not reader_account_manager:
        return False, "Reader is not initialized."

    return await reader_account_manager.add_client(
        int(user_id),
        phone,
        item["session_path"],
        item.get("twofa_password"),
    )


async def _reader_send_current(user_id: int):
    """Send the next usable number; invalid sessions are skipped automatically."""
    user_id = int(user_id)
    batch = reader_batches.get(user_id)
    if not batch or batch.get("status") != "active" or not reader_application:
        return

    while True:
        idx = int(batch.get("index", 0))
        items = batch.get("items", [])

        if idx >= len(items):
            batch["status"] = "completed"
            batch["current_phone"] = None
            await reader_application.bot.send_message(
                user_id,
                "🎉 <b>Batch Complete</b>\n\n"
                f"Processed <b>{len(items)}</b> session(s).\n\n"
                "You can still use the OTP message's <b>Manage Sessions</b> and "
                "<b>Request New OTP</b> buttons while this batch remains loaded.\n"
                "Send /read again to load the ZIP again or another package.",
                parse_mode="HTML",
            )
            return

        item = items[idx]
        phone = item["phone"]
        ok, reason = await _reader_ensure_loaded(user_id, phone)
        if not ok:
            item["status"] = "invalid"
            batch["index"] = idx + 1
            await reader_application.bot.send_message(
                user_id,
                f"⚠️ <b>Skipped {idx + 1}/{len(items)}</b>\n\n"
                f"📱 <code>{phone}</code>\n"
                f"Reason: {reason}",
                parse_mode="HTML",
            )
            continue

        item["status"] = "waiting_otp"
        batch["current_phone"] = phone
        batch.setdefault("pending_rerequest", set()).discard(phone)

        await reader_application.bot.send_message(
            user_id,
            f"📦 <b>Number {idx + 1}/{len(items)}</b>\n\n"
            f"📱 Number: <code>{phone}</code>\n\n"
            "Request the Telegram login code for this number now.\n"
            "As soon as its OTP arrives, I will send the OTP + 2FA and then "
            "automatically send the next number.\n\n"
            "⏱ <b>Reader auto-disconnects after 10 minutes.</b> "
            "You can upload the same ZIP again to reconnect.",
            parse_mode="HTML",
            reply_markup=_reader_waiting_keyboard(),
        )
        return


async def reader_otp_callback(*, owner_id, phone, otp, twofa_password=None):
    """AccountManager callback for Telegram 777000 login-code messages."""
    if not reader_application:
        return

    owner_id = int(owner_id)
    phone = _reader_normalize_phone(phone)

    async with _reader_lock(owner_id):
        batch = reader_batches.get(owner_id)
        if not batch:
            return

        # Ignore duplicate delivery of the same Telegram service message.
        dup_key = (owner_id, phone)
        previous = reader_last_otp.get(dup_key)
        now = time.time()
        if previous and previous[0] == otp and now - previous[1] < 20:
            return
        reader_last_otp[dup_key] = (otp, now)

        current_phone = batch.get("current_phone")
        pending = batch.setdefault("pending_rerequest", set())
        is_current = current_phone == phone and batch.get("status") == "active"
        is_rerequest = phone in pending

        if not is_current and not is_rerequest:
            logger.info(
                "Suppressed unrequested reader OTP user=%s phone=%s", owner_id, phone
            )
            return

        if is_rerequest:
            pending.discard(phone)

        item = next((x for x in batch.get("items", []) if x.get("phone") == phone), None)
        twofa = (item or {}).get("twofa_password") or twofa_password

        if is_current:
            idx = int(batch.get("index", 0))
            total = len(batch.get("items", []))
            heading = f"✅ <b>OTP Received — {idx + 1}/{total}</b>"
        else:
            heading = "🔄 <b>New OTP Received</b>"

        msg = (
            f"{heading}\n\n"
            f"📱 Number: <code>{phone}</code>\n"
            f"📩 OTP: <code>{otp}</code>"
        )
        if twofa:
            msg += f"\n🔐 2FA: <code>{twofa}</code>"
        msg += (
            "\n\nKeep these details private."
            "\n\n🔌 <b>Reader disconnects automatically after 10 minutes.</b> "
            "If you want to reuse the same ZIP before that, tap <b>Disconnect Reader</b> first. "
            "After disconnect, send <code>/read</code> and upload the same ZIP again to reconnect."
        )

        await reader_application.bot.send_message(
            owner_id,
            msg,
            parse_mode="HTML",
            reply_markup=_reader_keyboard(phone),
        )

        if not is_current:
            return

        # Advance only for the first OTP of the current queue item. Re-request
        # OTPs for older numbers never move the queue.
        if item:
            item["status"] = "otp_received"
        batch["current_phone"] = None
        batch["index"] = int(batch.get("index", 0)) + 1

    await asyncio.sleep(0.5)
    await _reader_send_current(owner_id)


async def reader_read_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = int(update.effective_user.id)
    if not _reader_credentials_ok():
        await update.message.reply_text(
            "❌ Reader credentials are not configured. Set READER_API_ID and "
            "READER_API_HASH (or DEFAULT_API_ID / DEFAULT_API_HASH) on the bot."
        )
        return

    old = reader_batches.get(user_id)
    if old and old.get("status") == "active":
        await update.message.reply_text(
            "⚠️ A reader batch is already active. Use /cancelread first, then /read again."
        )
        return

    if old:
        await _reader_cleanup_user(user_id)

    context.user_data["reader_waiting_zip"] = True
    await update.message.reply_text(
        "📎 <b>Send ZIP file</b>\n\n"
        "The bot will process the numbers one at a time.",
        parse_mode="HTML",
    )


async def reader_zip_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("reader_waiting_zip"):
        return
    document = update.message.document
    if not document:
        return

    filename = document.file_name or ""
    if not filename.lower().endswith(".zip"):
        await update.message.reply_text("❌ Please send a ZIP file.")
        return
    if document.file_size and document.file_size > READER_MAX_ZIP_BYTES:
        await update.message.reply_text("❌ ZIP file is too large.")
        return

    context.user_data.pop("reader_waiting_zip", None)
    user_id = int(update.effective_user.id)
    status = await update.message.reply_text("⏳ Checking session package...")
    download_dir = Path(tempfile.mkdtemp(prefix="reader_upload_"))
    zip_path = download_dir / "package.zip"

    try:
        tg_file = await document.get_file()
        await tg_file.download_to_drive(custom_path=str(zip_path))
        temp_dir, items = _reader_unpack_zip(str(zip_path), user_id)

        await _reader_cleanup_user(user_id)
        batch_id = uuid.uuid4().hex
        reader_batches[user_id] = {
            "batch_id": batch_id,
            "temp_dir": str(temp_dir),
            "source_name": filename,
            "items": items,
            "index": 0,
            "status": "active",
            "current_phone": None,
            "pending_rerequest": set(),
        }

        _reader_schedule_auto_disconnect(user_id, batch_id)

        await status.edit_text(
            "✅ <b>ZIP Loaded</b>\n\n"
            f"Accounts: <b>{len(items)}</b>\n"
            "Starting Number 1...\n\n"
            "⏱ <b>Session will disconnect automatically after 10 minutes.</b>\n"
            "After disconnect, you can send <code>/read</code> and upload the same ZIP again.",
            parse_mode="HTML",
            reply_markup=_reader_loaded_keyboard(),
        )
        await _reader_send_current(user_id)
    except zipfile.BadZipFile:
        await status.edit_text("❌ Invalid ZIP file.")
    except Exception:
        logger.exception("Reader ZIP load failed")
        await status.edit_text(
            "❌ Could not read this ZIP file. Please send a valid ZIP file."
        )
    finally:
        shutil.rmtree(download_dir, ignore_errors=True)


async def reader_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = int(update.effective_user.id)
    batch = reader_batches.get(user_id)
    if not batch:
        await update.message.reply_text("No reader batch is loaded. Send /read.")
        return
    total = len(batch.get("items", []))
    idx = int(batch.get("index", 0))
    current = batch.get("current_phone") or "—"
    await update.message.reply_text(
        "📊 <b>Reader Status</b>\n\n"
        f"Status: <b>{batch.get('status', 'unknown')}</b>\n"
        f"Progress: <b>{min(idx + 1, total) if batch.get('status') == 'active' else min(idx, total)}/{total}</b>\n"
        f"Current: <code>{current}</code>",
        parse_mode="HTML",
    )


async def reader_cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = int(update.effective_user.id)
    context.user_data.pop("reader_waiting_zip", None)
    if not reader_batches.get(user_id):
        await update.message.reply_text("No reader batch is loaded.")
        return
    await _reader_cleanup_user(user_id)
    await update.message.reply_text(
        "⏹ Reader batch closed. Local session copies were disconnected/deleted.\n"
        "Telegram authorizations were not revoked, so the original ZIP can be uploaded again."
    )


async def _reader_render_sessions(user_id: int, phone: str):
    phone = _reader_normalize_phone(phone)
    ok, reason = await _reader_ensure_loaded(user_id, phone)
    if not ok:
        return f"❌ Could not open <code>{phone}</code>: {reason}", None

    auths = await reader_account_manager.get_authorizations(user_id, phone)
    if auths is None:
        return f"❌ Could not fetch active sessions for <code>{phone}</code>.", None

    lines = [f"📱 <b>Active Sessions — {phone}</b>", ""]
    buttons = []
    for idx, auth in enumerate(auths, 1):
        current = bool(getattr(auth, "current", False))
        marker = "⭐ Reader" if current else "📟 Active"
        device = getattr(auth, "device_model", None) or "Unknown device"
        app_name = getattr(auth, "app_name", None) or "Telegram"
        app_version = getattr(auth, "app_version", None) or ""
        platform = getattr(auth, "platform", None) or ""
        system = getattr(auth, "system_version", None) or ""
        country = getattr(auth, "country", None) or ""
        date_active = getattr(auth, "date_active", None)
        date_str = date_active.strftime("%d/%m/%Y %H:%M") if date_active else "N/A"

        lines.append(
            f"{idx}. <b>{marker}</b> — {device}\n"
            f"   App: {app_name} {app_version}\n"
            f"   Platform: {platform} {system}\n"
            f"   Last active: {date_str}" + (f"\n   Country: {country}" if country else "")
        )

        if current:
            buttons.append([
                InlineKeyboardButton(
                    "⚠️ Logout Reader Session",
                    callback_data=f"read_logoutask:{phone}",
                )
            ])
        else:
            hash_id = int(getattr(auth, "hash", 0) or 0)
            buttons.append([
                InlineKeyboardButton(
                    f"❌ Terminate #{idx}: {device[:16]}",
                    callback_data=f"read_kill:{phone}:{hash_id}",
                )
            ])

    buttons.append([
        InlineKeyboardButton("🔄 Refresh", callback_data=f"read_sessions:{phone}")
    ])
    buttons.append([
        InlineKeyboardButton("🔌 Disconnect Reader Only", callback_data=f"read_disconnect:{phone}")
    ])
    buttons.append([
        InlineKeyboardButton("⬅️ Close", callback_data="read_close")
    ])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def reader_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    user_id = int(query.from_user.id)

    if not data.startswith("read_"):
        return
    await query.answer()

    batch = reader_batches.get(user_id)
    if not batch:
        await query.message.reply_text("❌ Reader batch expired. Upload the ZIP again with /read.")
        return

    if data == "read_skip":
        batch = reader_batches.get(user_id)
        if not batch or batch.get("status") != "active":
            await query.message.reply_text("❌ No active number to skip.")
            return

        idx = int(batch.get("index", 0) or 0)
        items = batch.get("items", [])
        phone = _reader_normalize_phone(batch.get("current_phone") or "")

        if idx >= len(items) or not phone:
            await query.message.reply_text("❌ No active number to skip.")
            return

        item = items[idx]
        item["status"] = "skipped"
        batch.setdefault("pending_rerequest", set()).discard(phone)
        batch["current_phone"] = None
        batch["index"] = idx + 1

        if reader_account_manager:
            await reader_account_manager.remove_client(user_id, phone)

        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        await query.message.reply_text(
            f"⏭️ <b>Skipped</b>\n\n"
            f"📱 <code>{phone}</code>\n"
            "Moving to the next number...",
            parse_mode="HTML",
        )

        await _reader_send_current(user_id)
        return

    if data == "read_disconnect_all":
        await _reader_cleanup_user(user_id)
        await query.message.reply_text(
            "🔌 <b>Reader Disconnected</b>\n\n"
            "All reader connections were closed locally. Telegram authorization was not revoked.\n"
            "Send <code>/read</code> and upload the same ZIP again whenever you want to reconnect.",
            parse_mode="HTML",
        )
        return

    if data == "read_stop":
        await _reader_cleanup_user(user_id)
        await query.message.reply_text(
            "⏹ Reader stopped. No Telegram authorization was revoked; the original ZIP remains reusable."
        )
        return

    if data.startswith("read_rereq:"):
        phone = _reader_normalize_phone(data.split(":", 1)[1])
        ok, reason = await _reader_ensure_loaded(user_id, phone)
        if not ok:
            await query.message.reply_text(f"❌ {reason}")
            return
        batch.setdefault("pending_rerequest", set()).add(phone)
        await query.message.reply_text(
            "🔄 <b>Waiting for New OTP</b>\n\n"
            f"📱 <code>{phone}</code>\n"
            "Request a new Telegram login code now. The next OTP for this number will be sent here.",
            parse_mode="HTML",
        )
        return

    if data.startswith("read_sessions:"):
        phone = data.split(":", 1)[1]
        text, markup = await _reader_render_sessions(user_id, phone)
        if markup:
            await query.message.reply_text(text, parse_mode="HTML", reply_markup=markup)
        else:
            await query.message.reply_text(text, parse_mode="HTML")
        return

    if data.startswith("read_kill:"):
        try:
            _, phone, hash_raw = data.split(":", 2)
            hash_id = int(hash_raw)
        except Exception:
            await query.message.reply_text("❌ Invalid session action.")
            return
        ok, msg = await reader_account_manager.terminate_session(user_id, phone, hash_id)
        await query.message.reply_text(("✅ " if ok else "❌ ") + msg)
        return

    if data.startswith("read_logoutask:"):
        phone = _reader_normalize_phone(data.split(":", 1)[1])
        await query.message.reply_text(
            "⚠️ <b>Logout Reader Session?</b>\n\n"
            f"📱 <code>{phone}</code>\n\n"
            "This permanently revokes the Telegram authorization used by this .session file. "
            "After logout, re-uploading the same ZIP will NOT restore this account.\n\n"
            "Use <b>Disconnect Reader Only</b> instead if you want the ZIP to stay reusable.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚫 Logout Permanently", callback_data=f"read_logoutyes:{phone}")],
                [InlineKeyboardButton("↩️ Cancel", callback_data="read_close")],
            ]),
        )
        return

    if data.startswith("read_logoutyes:"):
        phone = _reader_normalize_phone(data.split(":", 1)[1])
        ok, msg = await reader_account_manager.terminate_own_session(user_id, phone)
        if ok:
            item = next((x for x in batch.get("items", []) if x.get("phone") == phone), None)
            if item:
                item["status"] = "logged_out"
            await query.message.reply_text(
                "✅ Reader session logged out permanently. This account's .session authorization is now revoked."
            )
            # If user revoked the currently-waiting number, skip it and continue.
            if batch.get("current_phone") == phone and batch.get("status") == "active":
                batch["current_phone"] = None
                batch["index"] = int(batch.get("index", 0)) + 1
                await _reader_send_current(user_id)
        else:
            await query.message.reply_text(f"❌ {msg}")
        return

    if data.startswith("read_disconnect:"):
        phone = _reader_normalize_phone(data.split(":", 1)[1])
        await reader_account_manager.remove_client(user_id, phone)
        await query.message.reply_text(
            "🔌 Reader disconnected locally. Telegram authorization was not revoked, so this ZIP/session can be opened again."
        )
        return

    if data == "read_close":
        try:
            await query.delete_message()
        except Exception:
            pass
        return


async def reader_shutdown(application):
    for task in list(reader_auto_disconnect_tasks.values()):
        if task and not task.done():
            task.cancel()
    reader_auto_disconnect_tasks.clear()

    if reader_account_manager:
        await reader_account_manager.stop_all()

    for batch in list(reader_batches.values()):
        temp_dir = batch.get("temp_dir")
        if temp_dir:
            shutil.rmtree(str(temp_dir), ignore_errors=True)


def main() -> None:
    global reader_application, reader_account_manager

    if not BOT_TOKEN:
        raise SystemExit("Please set the BOT_TOKEN environment variable.")

    app = Application.builder().token(BOT_TOKEN).post_shutdown(reader_shutdown).build()
    reader_application = app
    if _reader_credentials_ok():
        reader_account_manager = AccountManager(
            int(READER_API_ID), READER_API_HASH, otp_callback=reader_otp_callback
        )
    else:
        logger.warning("Reader mode disabled until READER_API_ID/READER_API_HASH or DEFAULT credentials are configured.")

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

    app.add_handler(conv, group=0)

    # Independent Server 1 bulk session reader. These handlers live in a
    # separate group so they do not disturb the existing session-generator
    # conversation flow.
    app.add_handler(CommandHandler("read", reader_read_cmd), group=1)
    app.add_handler(CommandHandler("readstatus", reader_status_cmd), group=1)
    app.add_handler(CommandHandler("cancelread", reader_cancel_cmd), group=1)
    app.add_handler(CallbackQueryHandler(reader_callback_handler, pattern=r"^read_"), group=1)
    app.add_handler(MessageHandler(filters.Document.ALL, reader_zip_handler), group=1)

    # Start the health-check HTTP server in a background thread so Render
    # can detect an open port, while the bot itself keeps polling Telegram.
    threading.Thread(target=start_health_server, daemon=True).start()

    logger.info("Bot started. Polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
