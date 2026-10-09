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
  - **Health Check Path:** `/health`
  - Build command: `pip install -r requirements.txt`
  - Start command: `python bot.py`
  - Environment variables: `BOT_TOKEN` (aur optionally `DEFAULT_API_ID`,
    `DEFAULT_API_HASH`)

Agar tum Render par **Background Worker** service type use karte ho (jo port
bind nahi maangta), to yeh health server harm nahi karega — bas thread me
chalta rahega, chahe koi usko hit kare ya na kare.

### UptimeRobot

Create an **HTTP(s)** monitor with this URL:

```text
https://YOUR-SERVICE-NAME.onrender.com/health
```

The endpoint supports both `GET` and `HEAD` and returns HTTP `200` with:

```json
{"status":"ok","service":"telegram-session-bot"}
```

Before adding it to UptimeRobot, open the `/health` URL once in a browser. If it
returns the JSON above, the monitor has a valid HTTP target.

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

## Vercel note

`api/index.py` now exports a top-level FastAPI `app`, so Vercel can detect the
Python entrypoint successfully.

However, the Telegram session-generator flow in `bot.py` uses long-running
polling and keeps a live Telethon/Pyrogram login client in memory between the
phone, OTP and 2FA steps. Vercel Functions are stateless/ephemeral, so the
actual bot should be run on an always-on process host with:

```bash
python bot.py
```

Use Vercel only for the included HTTP entrypoint/health endpoint, not for the
polling bot process itself.

---

## Server 1 Bulk Session ZIP Reader

This build also supports the independent reader flow:

1. Send `/read`.
2. Upload the exact **Server 1 Bulk Session Package** ZIP produced by the OTP panel.
3. The package must contain `accounts.txt` beginning with `Bulk Session Delivery` plus numeric `.session` files.
4. The reader opens the first session, shows Number 1, and listens for Telegram service messages from `777000`.
5. When an OTP arrives, it sends **Number + OTP + 2FA + Request New OTP + Manage Sessions**, then automatically moves to the next number.
6. Re-request on an older number only forwards that number's next OTP; it does not disturb the main queue.
7. Manage Sessions can terminate other Telegram device sessions or permanently log out the reader session.
8. **Disconnect Reader Only** closes the local client without revoking Telegram authorization, so the original ZIP remains reusable.

Reader commands:

- `/read`
- `/readstatus`
- `/cancelread`

Reader environment:

```env
READER_API_ID=
READER_API_HASH=
```

If these are empty, the reader falls back to `DEFAULT_API_ID` / `DEFAULT_API_HASH`.

### ZIP reuse

Stopping/cancelling the reader or using **Disconnect Reader Only** does not revoke Telegram authorization, so the same original ZIP can be uploaded again. Before reusing the same ZIP, disconnect the reader connection first; do not run the same ZIP simultaneously in multiple reader instances. If the user chooses **Logout Reader Session**, Telegram revokes that authorization permanently and that account inside the old ZIP will no longer work.


## Reader auto-disconnect

Reader connections automatically disconnect locally after 10 minutes. This does not revoke Telegram authorization. The same original ZIP can be uploaded again with `/read` to reconnect. A `Disconnect Reader` button is shown immediately after ZIP upload and while waiting for OTP.


## Skip number

While waiting for an OTP, the user can tap **Skip Number**. The reader disconnects that number locally, marks it skipped, and immediately moves to the next number in the ZIP.
