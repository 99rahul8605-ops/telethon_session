"""
Telethon Session String Generator Bot
--------------------------------------
Flow:
1. /start -> asks for API_ID (user can send /skip to use default API_ID/API_HASH
   configured by the bot owner via environment variables).
2. If not skipped -> asks for API_HASH too.
3. Asks for phone number (with country code, e.g. +919876543210).
4. Sends OTP to that number via Telegram.
5. Asks user to enter the OTP with a SPACE between every digit
   (e.g. "1 2 3 4 5") — this prevents Telegram's client from auto
   invalidating the code when it detects a raw login-code pattern
   being forwarded/pasted into a chat.
6. If the account has Two-Step Verification (2FA) enabled, asks for
   the password.
7. Generates the Telethon StringSession and sends it to the user,
   then cleans up the temporary client.

Run:
    pip install -r requirements.txt
    export BOT_TOKEN="123456:ABC-your-bot-token"
    # Optional, only used when the user sends /skip:
    export DEFAULT_API_ID="12345"
    export DEFAULT_API_HASH="0123456789abcdef0123456789abcdef"
    python bot.py

WARNING: A session string grants full access to the Telegram account
it was generated for. Never share it publicly, and only run this bot
on infrastructure you trust — the bot itself will see every session
string it generates.
"""

import logging
import os

from telegram import Update
from telegram.ext import (
    Application,
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
API_ID, API_HASH, PHONE, OTP, PASSWORD = range(5)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "👋 Welcome to the Session String Generator.\n\n"
        "Send me your Telegram *API_ID*.\n"
        "Don't have one? Send /skip to use the bot's default API_ID/API_HASH "
        "(if the bot owner has configured one).\n\n"
        "You can cancel anytime with /cancel.",
        parse_mode="Markdown",
    )
    return API_ID


async def skip_api(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not DEFAULT_API_ID or not DEFAULT_API_HASH:
        await update.message.reply_text(
            "⚠️ No default API_ID/API_HASH is configured on this bot.\n"
            "Please send your own API_ID (get one at https://my.telegram.org)."
        )
        return API_ID

    context.user_data["api_id"] = int(DEFAULT_API_ID)
    context.user_data["api_hash"] = DEFAULT_API_HASH
    await update.message.reply_text(
        "✅ Using the bot's default API_ID/API_HASH.\n\n"
        "Now send your phone number with country code, e.g. +919876543210"
    )
    return PHONE


async def get_api_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("API_ID must be a number. Please try again, or /skip.")
        return API_ID

    context.user_data["api_id"] = int(text)
    await update.message.reply_text("Great. Now send your *API_HASH*.", parse_mode="Markdown")
    return API_HASH


async def get_api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["api_hash"] = update.message.text.strip()
    await update.message.reply_text(
        "Now send your phone number with country code, e.g. +919876543210"
    )
    return PHONE


async def get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone = update.message.text.strip()
    api_id = context.user_data["api_id"]
    api_hash = context.user_data["api_hash"]

    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()

    try:
        sent = await client.send_code_request(phone)
    except ApiIdInvalidError:
        await update.message.reply_text(
            "❌ Invalid API_ID/API_HASH combination. Send /start to try again."
        )
        await client.disconnect()
        return ConversationHandler.END
    except PhoneNumberInvalidError:
        await update.message.reply_text(
            "❌ That phone number looks invalid. Please send it again with country code."
        )
        await client.disconnect()
        return PHONE
    except FloodWaitError as e:
        await update.message.reply_text(
            f"⏳ Too many attempts. Telegram asks you to wait {e.seconds} seconds, then /start again."
        )
        await client.disconnect()
        return ConversationHandler.END

    context.user_data["client"] = client
    context.user_data["phone"] = phone
    context.user_data["phone_code_hash"] = sent.phone_code_hash

    await update.message.reply_text(
        "📩 OTP sent to your Telegram account.\n\n"
        "⚠️ IMPORTANT: To stop Telegram from auto-invalidating the code, "
        "type it back with a SPACE between every digit.\n"
        "Example: if the code is 12345, send it as:\n"
        "1 2 3 4 5"
    )
    return OTP


async def get_otp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    code = raw.replace(" ", "")

    if not code.isdigit():
        await update.message.reply_text(
            "That doesn't look like a valid code. Please resend it with spaces "
            "between digits, e.g. 1 2 3 4 5"
        )
        return OTP

    client: TelegramClient = context.user_data["client"]
    phone = context.user_data["phone"]
    phone_code_hash = context.user_data["phone_code_hash"]

    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
    except PhoneCodeInvalidError:
        await update.message.reply_text(
            "❌ Incorrect code. Please resend it with spaces between digits, e.g. 1 2 3 4 5"
        )
        return OTP
    except SessionPasswordNeededError:
        await update.message.reply_text(
            "🔒 Your account has Two-Step Verification enabled.\n"
            "Please send your 2FA password."
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
        "✅ Login successful! Here is your Telethon session string:\n\n"
        f"`{session_string}`\n\n"
        "⚠️ Keep this secret — anyone with this string has full access to your account.",
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
    await update.message.reply_text("Cancelled. Send /start to begin again.")
    return ConversationHandler.END


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Please set the BOT_TOKEN environment variable.")

    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            API_ID: [
                CommandHandler("skip", skip_api),
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
