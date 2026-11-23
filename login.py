"""Log in to your Telegram account once, so the bot can send messages as you.

The login is saved to data/user_session.txt (inside the bot's data folder),
where the bot finds it automatically. Anyone who has that file has full access
to your Telegram account, so keep it secret.

Usage:  python login.py            (or, on the server: docker compose run --rm kdbot python login.py)
        python login.py --print    also print the session string (for hosts without a data folder)
"""

import os
import sys

from dotenv import load_dotenv
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

from kdbot.config import session_file

load_dotenv()
api_id = int(os.environ.get("API_ID") or input("API_ID: "))
api_hash = os.environ.get("API_HASH") or input("API_HASH: ")
path = session_file(os.environ.get("DATA_DIR", "data"))

print("\nLog in with the Telegram account that will send the messages.")
print("Phone number format: +<country code><number>, e.g. +201001234567\n")

with TelegramClient(StringSession(), api_id, api_hash) as client:
    me = client.get_me()
    session = client.session.save()

os.makedirs(os.path.dirname(path), exist_ok=True)
with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
    f.write(session)

print(f"\n✅ Logged in as {me.first_name} (@{me.username or me.id}). Login saved.")
if "--print" in sys.argv:
    print("\nSESSION_STRING (keep it secret):\n" + session)
