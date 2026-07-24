# Telethon Session String Generator Bot

Ek Telegram bot jo users ko unka Telethon `StringSession` generate karke deta hai.

## Flow

1. `/start` → bot API_ID maangega.
   - User agar apna API_ID/API_HASH nahi dena chahta, to `/skip` bhej sakta hai —
     tab bot owner ke set kiye hue default API_ID/API_HASH use honge (agar configure kiye ho).
2. API_ID diya to → bot API_HASH maangega.
3. Fir bot phone number maangega (country code ke sath, e.g. `+919876543210`).
4. Bot us number par Telegram se OTP bhejwayega.
5. **Important:** Login galti se incomplete/invalid na ho, isliye bot user ko bolega
   ki OTP ke digits ke beech space rakh kar bheje — jaise code `12345` ho to
   `1 2 3 4 5` bheje. Isse Telegram ka auto-detection code ko cancel nahi karta.
6. Agar account par 2FA (Two-Step Verification) on hai, bot password maangega.
7. Login successful hone par bot session string generate karke user ko bhej dega.

## Setup

```bash
pip install -r requirements.txt

export BOT_TOKEN="123456:ABC-your-bot-token-from-BotFather"

# Optional — sirf tab kaam aayenge jab user /skip bhejega
export DEFAULT_API_ID="12345"
export DEFAULT_API_HASH="0123456789abcdef0123456789abcdef"

python bot.py
```

- `BOT_TOKEN`: apna bot token @BotFather se lo.
- `DEFAULT_API_ID` / `DEFAULT_API_HASH`: apna API_ID/API_HASH https://my.telegram.org
  se lo, agar chahte ho ki users `/skip` use kar sakein.

## Security Notes

- Generated session string us Telegram account ka **full access** deta hai.
  Ise kabhi bhi publicly share mat karo, aur bot ka server bhi trusted hona chahiye
  (bot khud har generated session string ko dekh sakta hai).
- Yeh bot temporary hi Telethon client bana kar use karta hai; login complete
  ya cancel hone par client disconnect ho jata hai.
- Production me deploy karte waqt, in-memory `user_data` ke bajaye persistent/secure
  storage aur rate-limiting add karna consider karo agar bahut users honge.
