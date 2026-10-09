import logging
import re
from pathlib import Path

from telethon import TelegramClient, events, functions
from telethon.sessions import SQLiteSession


class AccountManager:
    """Manage Telethon SQLite sessions loaded from an approved Server 1 ZIP.

    This is adapted from the OTP panel's AccountManager behavior: listen only to
    Telegram service-account messages (777000), extract login OTPs, expose
    authorization/session management, and support disconnect vs permanent logout.
    """

    def __init__(self, api_id: int, api_hash: str, otp_callback=None):
        self.api_id = int(api_id)
        self.api_hash = str(api_hash)
        self.otp_callback = otp_callback
        self.clients = {}          # (owner_id, normalized_phone) -> TelegramClient
        self.meta = {}             # key -> {session_path, twofa_password}

    @staticmethod
    def normalize_phone(value: str) -> str:
        return re.sub(r"\D", "", str(value or ""))

    def _key(self, owner_id: int, phone: str):
        return int(owner_id), self.normalize_phone(phone)

    async def add_client(
        self,
        owner_id: int,
        phone: str,
        session_path: str,
        twofa_password=None,
    ):
        """Open and validate one Telethon SQLite session file.

        Returns (True, None) on success or (False, reason) on failure.
        The connected account phone is checked against the expected filename/
        manifest phone so a mismatched session cannot silently enter the batch.
        """
        key = self._key(owner_id, phone)
        expected_phone = key[1]

        existing = self.clients.get(key)
        if existing:
            try:
                if not existing.is_connected():
                    await existing.connect()
                if await existing.is_user_authorized():
                    return True, None
            except Exception:
                pass
            await self.remove_client(owner_id, phone)

        path = Path(session_path)
        if not path.is_file():
            return False, "session file missing"

        try:
            session = SQLiteSession(str(path))
            client = TelegramClient(session, self.api_id, self.api_hash)
            await client.connect()
        except Exception as exc:
            logging.warning("Could not open session %s: %s", path, exc)
            return False, f"could not open session: {exc}"

        try:
            authorized = await client.is_user_authorized()
        except Exception as exc:
            await client.disconnect()
            return False, f"authorization check failed: {exc}"

        if not authorized:
            await client.disconnect()
            return False, "session is invalid, expired, or logged out"

        try:
            me = await client.get_me()
            actual_phone = self.normalize_phone(getattr(me, "phone", ""))
            if actual_phone and expected_phone and actual_phone != expected_phone:
                await client.disconnect()
                return False, (
                    f"session phone mismatch (expected {expected_phone}, got {actual_phone})"
                )
        except Exception as exc:
            await client.disconnect()
            return False, f"could not verify account identity: {exc}"

        self.clients[key] = client
        self.meta[key] = {
            "session_path": str(path),
            "twofa_password": twofa_password,
        }

        @client.on(events.NewMessage(from_users=777000))
        async def otp_handler(event, _owner=int(owner_id), _phone=expected_phone):
            text = event.message.message or ""
            match = re.search(r"\b(\d{5,6})\b", text)
            if not match:
                match = re.search(r"Login code:\s*(\d+)", text, re.I)
            if not match:
                return

            otp = match.group(1)
            if self.otp_callback:
                try:
                    await self.otp_callback(
                        owner_id=_owner,
                        phone=_phone,
                        otp=otp,
                        twofa_password=(self.meta.get((_owner, _phone)) or {}).get(
                            "twofa_password"
                        ),
                    )
                except Exception:
                    logging.exception(
                        "Reader OTP callback failed owner=%s phone=%s", _owner, _phone
                    )

        logging.info("Reader client started owner=%s phone=%s", owner_id, expected_phone)
        return True, None

    async def ensure_client(self, owner_id: int, phone: str):
        key = self._key(owner_id, phone)
        client = self.clients.get(key)
        if not client:
            return None
        try:
            if not client.is_connected():
                await client.connect()
            if not await client.is_user_authorized():
                return None
            return client
        except Exception:
            return None

    async def get_authorizations(self, owner_id: int, phone: str):
        client = await self.ensure_client(owner_id, phone)
        if not client:
            return None
        try:
            result = await client(functions.account.GetAuthorizationsRequest())
            return result.authorizations
        except Exception as exc:
            logging.warning("Could not fetch authorizations for %s: %s", phone, exc)
            return None

    async def terminate_session(self, owner_id: int, phone: str, hash_id: int):
        """Terminate a different Telegram device session, keeping this reader session."""
        client = await self.ensure_client(owner_id, phone)
        if not client:
            return False, "No active reader connection for this number."
        try:
            await client(functions.account.ResetAuthorizationRequest(hash=int(hash_id)))
            return True, "Session terminated."
        except Exception as exc:
            return False, str(exc)

    async def terminate_own_session(self, owner_id: int, phone: str):
        """Permanently revoke the reader's current Telegram authorization."""
        key = self._key(owner_id, phone)
        client = self.clients.get(key)
        if not client:
            return False, "No active reader connection for this number."
        try:
            await client.log_out()
        except Exception as exc:
            return False, str(exc)
        finally:
            self.clients.pop(key, None)
            self.meta.pop(key, None)
        return True, "Reader session logged out and revoked."

    async def remove_client(self, owner_id: int, phone: str):
        """Disconnect locally without revoking Telegram authorization."""
        key = self._key(owner_id, phone)
        client = self.clients.pop(key, None)
        self.meta.pop(key, None)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def remove_owner(self, owner_id: int):
        keys = [key for key in self.clients if key[0] == int(owner_id)]
        for _, phone in keys:
            await self.remove_client(owner_id, phone)

    async def stop_all(self):
        for key, client in list(self.clients.items()):
            try:
                await client.disconnect()
            except Exception:
                pass
        self.clients.clear()
        self.meta.clear()
