"""Run this once on your own computer to log in to your Telegram account.

It prints a SESSION_STRING to put in your .env file. Anyone who has that string
has full access to your Telegram account, so keep it secret.
"""

import os

from dotenv import load_dotenv
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

load_dotenv()
api_id = int(os.environ.get("API_ID") or input("API_ID: "))
api_hash = os.environ.get("API_HASH") or input("API_HASH: ")

with TelegramClient(StringSession(), api_id, api_hash) as client:
    print("\nLogged in. Put this in your .env as SESSION_STRING:\n")
    print(client.session.save())
