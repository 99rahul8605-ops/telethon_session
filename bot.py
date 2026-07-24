"""
Telethon Session String Generator Bot (Advanced / Inline-button edition)
-------------------------------------------------------------------------
Flow:
1. /start -> sends a stylish welcome message with an inline "Generate Session"
   button.
2. Tapping the button starts the flow: asks for API_ID, with an inline
   "Skip (use default)" button for users who don't want to provide their own
   API_ID/API_HASH (requires the bot owner to configure defaults).
3. If not skipped -> asks for API_HASH.
4. Asks for the phone number.
5. Sends an OTP to that number via Telegram.
6. Asks the user to enter the OTP with a SPACE between every digit
   (e.g. "1 2 3 4 5") — this prevents Telegram's client from auto
   invalidating the code when it detects a raw login-code pattern being
   forwarded/pasted into a chat.
7. If the account has Two-Step Verification (2FA) enabled, asks for the
   password.
8. Generates the Telethon StringSession and sends it to the user, then
   cleans up the temporary client.

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

import logging
import os

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

from telethon import TelegramClient
from telethon.errors import (
    ApiIdInvalidError,
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DEFAULT_API_ID = os.environ.get("DEFAULT_API_ID")
DEFAULT_API_HASH = os.environ.get("DEFAULT_API_HASH")

# Conversation states
MENU, API_ID, API_HASH, PHONE, OTP, PASSWORD = range(6)

WELCOME_TEXT = (
    "✨ *Welcome to Session Generator Bot* ✨\n\n"
    "🔐 I can generate a secure *Telethon Session String* for your Telegram "
    "account in just a few simple steps.\n\n"
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


def skip_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⏭️ Skip (use default)", callback_data="skip_api")]]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        WELCOME_TEXT,
        parse_mode="Markdown",
        reply_markup=generate_button(),
    )
    return MENU


async def ask_api_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
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
    api_id = context.user_data["api_id"]
    api_hash = context.user_data["api_hash"]

    status_msg = await update.message.reply_text("⏳ Sending OTP, please wait...")

    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()

    try:
        sent = await client.send_code_request(phone)
    except ApiIdInvalidError:
        await status_msg.edit_text(
            "❌ Invalid API_ID/API_HASH combination. Send /start to try again."
        )
        await client.disconnect()
        return ConversationHandler.END
    except PhoneNumberInvalidError:
        await status_msg.edit_text(
            "❌ That phone number looks invalid. Please send it again with country code."
        )
        await client.disconnect()
        return PHONE
    except FloodWaitError as e:
        await status_msg.edit_text(
            f"⏳ Too many attempts. Telegram asks you to wait {e.seconds}s, then /start again."
        )
        await client.disconnect()
        return ConversationHandler.END

    context.user_data["client"] = client
    context.user_data["phone"] = phone
    context.user_data["phone_code_hash"] = sent.phone_code_hash

    await status_msg.edit_text(
        "📩 *Step 3/4 — Enter OTP*\n\n"
        "A login code has been sent to your Telegram account.\n\n"
        "⚠️ *Important:* To stop Telegram from auto-invalidating the code, "
        "type it back with a *space between every digit*.\n\n"
        "Example — if the code is `12345`, send it as:\n"
        "`1 2 3 4 5`",
        parse_mode="Markdown",
    )
    return OTP


async def get_otp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    code = raw.replace(" ", "")

    if not code.isdigit():
        await update.message.reply_text(
            "❌ That doesn't look like a valid code.\n"
            "Please resend it with spaces between digits, e.g. `1 2 3 4 5`",
            parse_mode="Markdown",
        )
        return OTP

    client: TelegramClient = context.user_data["client"]
    phone = context.user_data["phone"]
    phone_code_hash = context.user_data["phone_code_hash"]

    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
    except PhoneCodeInvalidError:
        await update.message.reply_text(
            "❌ Incorrect code. Please resend it with spaces between digits, "
            "e.g. `1 2 3 4 5`",
            parse_mode="Markdown",
        )
        return OTP
    except SessionPasswordNeededError:
        await update.message.reply_text(
            "🔒 *Step 4/4 — Two-Step Verification*\n\n"
            "Your account has 2FA enabled. Please send your password.",
            parse_mode="Markdown",
        )
        return PASSWORD

    return await finish(update, context)


async def get_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    password = update.message.text
    client: TelegramClient = context.user_data["client"]

    try:
        await client.sign_in(password=password)
    except PasswordHashInvalidError:
        await update.message.reply_text("❌ Wrong password. Please send it again.")
        return PASSWORD

    return await finish(update, context)


async def finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    client: TelegramClient = context.user_data["client"]
    session_string = client.session.save()

    await update.message.reply_text(
        "🎉 *Login Successful!*\n\n"
        "Here is your Telethon session string:\n\n"
        f"`{session_string}`\n\n"
        "⚠️ *Keep this secret* — anyone with this string has full access to "
        "your account. Never share it publicly.\n\n"
        "Send /start to generate another one.",
        parse_mode="Markdown",
    )

    await client.disconnect()
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    client = context.user_data.get("client")
    if client:
        await client.disconnect()
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
            MENU: [CallbackQueryHandler(ask_api_id, pattern="^generate$")],
            API_ID: [
                CallbackQueryHandler(skip_api, pattern="^skip_api$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_api_id),
            ],
            API_HASH: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_api_hash)],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_phone)],
            OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_otp)],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_password)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv)
    logger.info("Bot started. Polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
