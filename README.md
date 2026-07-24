# Telegram Session String Generator Bot (Telethon + Pyrogram)

Ek Telegram bot jo users ko unka session string generate karke deta hai —
user apni marzi se **Telethon** ya **Pyrogram** choose kar sakta hai.

## Flow

1. `/start` → bot ek stylish *welcome message* bhejta hai with an inline
   **🚀 Generate Session** button.
2. Button tap karte hi bot poochta hai: **🐍 Telethon** ya **🔥 Pyrogram** —
   jo bhi library chahiye, user tap kar ke choose kar sakta hai.
3. Fir bot API_ID maangega, sath me ek inline **⏭️ Skip (use default)** button
   bhi hoga — agar user apna API_ID/API_HASH nahi dena chahta, to bot owner ke
   configure kiye hue default credentials use ho jaate hain (Telethon aur
   Pyrogram dono same API_ID/API_HASH format use karte hain).
4. API_ID diya to → bot API_HASH maangega.
5. Fir bot phone number maangega (country code ke sath, e.g. `+919876543210`).
6. Bot us number par Telegram se OTP bhejwayega.
7. **Important:** Login galti se incomplete/invalid na ho, isliye bot user ko bolega
   ki OTP ke digits ke beech space rakh kar bheje — jaise code `12345` ho to
   `1 2 3 4 5` bheje. Isse Telegram ka auto-detection code ko cancel nahi karta.
8. Agar account par 2FA (Two-Step Verification) on hai, bot password maangega.
9. Login successful hone par bot chuni gayi library ke hisab se session string
   generate karke user ko bhej dega, step-by-step progress ke sath
   (Step 1/4, 2/4, 3/4, 4/4).

## Deploying on Render

Render ke **Web Service** ko health-check pass karne ke liye ek open `$PORT`
chahiye hota hai — warna deploy "unhealthy"/failed dikhta hai, chahe bot khud
web traffic serve na kare. Isliye bot ab background me ek chhota HTTP server
bhi chalata hai jo sirf `200 OK` return karta hai:

- Server `$PORT` env var (Render automatically set karta hai) par bind hota hai,
  agar `$PORT` set nahi hai to default `8080` use hota hai.
- Yeh ek daemon thread me chalta hai, taaki bot ki actual Telegram-polling
  process bilkul normal chalti rahe.
- Render dashboard me:
  - **Service type:** Web Service
  - **Health Check Path:** `/`
  - Build command: `pip install -r requirements.txt`
  - Start command: `python bot.py`
  - Environment variables: `BOT_TOKEN` (aur optionally `DEFAULT_API_ID`,
    `DEFAULT_API_HASH`)

Agar tum Render par **Background Worker** service type use karte ho (jo port
bind nahi maangta), to yeh health server harm nahi karega — bas thread me
chalta rahega, chahe koi usko hit kare ya na kare.

## Notes on Pyrogram

- `tgcrypto` optional hai but strongly recommended — Pyrogram ki speed
  behtar ho jaati hai isse.
- Agar official `pyrogram` package install/import issues de raha ho tumhare
  environment me, maintained forks jaise `pyrofork` ya `kurigram` bhi drop-in
  replacement ki tarah kaam karte hain (same `from pyrogram import Client`
  import path).

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
