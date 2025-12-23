from telethon import TelegramClient
from config import USERBOT_API_ID, USERBOT_API_HASH, USERBOT_PHONE_NUMBER

client = TelegramClient("userbot_session", USERBOT_API_ID, USERBOT_API_HASH)

async def init_userbot():
    await client.start(phone=USERBOT_PHONE_NUMBER)
    print("Userbot connected")
    return client
