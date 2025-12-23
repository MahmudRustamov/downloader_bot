"""
Telegram Stories Viewer Bot
Python 3.11 + PostgreSQL
"""

import asyncio
from datetime import datetime
from typing import Optional, List, Dict
import logging

from telethon import TelegramClient
from telethon.tl.functions.stories import GetPeerStoriesRequest, GetPinnedStoriesRequest
from telethon.errors import UsernameNotOccupiedError, PhoneNumberInvalidError, FloodWaitError
import asyncpg
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile

from config import get_config, Config

# Initialize config
config = get_config()

# Logging setup
logging.basicConfig(
    level=getattr(logging, config.app.log_level),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize bot and userbot
bot = Bot(token=config.bot.token)
dp = Dispatcher()
userbot: Optional[TelegramClient] = None
db_pool: Optional[asyncpg.Pool] = None


class DatabaseManager:
    """PostgreSQL database manager"""

    @staticmethod
    async def create_pool():
        """Create database connection pool"""
        db_config = config.database
        return await asyncpg.create_pool(
            host=db_config.host,
            port=db_config.port,
            database=db_config.name,
            user=db_config.user,
            password=db_config.password,
            min_size=db_config.min_pool_size,
            max_size=db_config.max_pool_size
        )

    @staticmethod
    async def init_db(pool: asyncpg.Pool):
        """Initialize database tables"""
        async with pool.acquire() as conn:
            # Users table
            await conn.execute('''
                               CREATE TABLE IF NOT EXISTS users
                               (
                                   user_id
                                   BIGINT
                                   PRIMARY
                                   KEY,
                                   username
                                   VARCHAR
                               (
                                   255
                               ),
                                   first_name VARCHAR
                               (
                                   255
                               ),
                                   last_name VARCHAR
                               (
                                   255
                               ),
                                   is_active BOOLEAN DEFAULT TRUE,
                                   is_blocked BOOLEAN DEFAULT FALSE,
                                   created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                                   last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                                   )
                               ''')

            # Requests table
            await conn.execute('''
                               CREATE TABLE IF NOT EXISTS requests
                               (
                                   id
                                   SERIAL
                                   PRIMARY
                                   KEY,
                                   user_id
                                   BIGINT
                                   REFERENCES
                                   users
                               (
                                   user_id
                               ),
                                   target VARCHAR
                               (
                                   255
                               ),
                                   request_type VARCHAR
                               (
                                   50
                               ),
                                   stories_count INT DEFAULT 0,
                                   success BOOLEAN DEFAULT FALSE,
                                   error_message TEXT,
                                   processing_time FLOAT,
                                   created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                                   )
                               ''')

            # Statistics table
            await conn.execute('''
                               CREATE TABLE IF NOT EXISTS statistics
                               (
                                   id
                                   SERIAL
                                   PRIMARY
                                   KEY,
                                   date
                                   DATE
                                   DEFAULT
                                   CURRENT_DATE,
                                   total_users
                                   INT
                                   DEFAULT
                                   0,
                                   active_users
                                   INT
                                   DEFAULT
                                   0,
                                   total_requests
                                   INT
                                   DEFAULT
                                   0,
                                   successful_requests
                                   INT
                                   DEFAULT
                                   0,
                                   failed_requests
                                   INT
                                   DEFAULT
                                   0,
                                   total_stories
                                   INT
                                   DEFAULT
                                   0,
                                   UNIQUE
                               (
                                   date
                               )
                                   )
                               ''')

            # Create indexes
            await conn.execute('''
                               CREATE INDEX IF NOT EXISTS idx_user_id ON requests(user_id);
                               CREATE INDEX IF NOT EXISTS idx_created_at ON requests(created_at);
                               CREATE INDEX IF NOT EXISTS idx_success ON requests(success);
                               CREATE INDEX IF NOT EXISTS idx_stats_date ON statistics(date);
                               ''')

            logger.info("Database initialized successfully")

    @staticmethod
    async def save_user(pool: asyncpg.Pool, user: types.User):
        """Save or update user info"""
        async with pool.acquire() as conn:
            await conn.execute('''
                               INSERT INTO users (user_id, username, first_name, last_name, last_active)
                               VALUES ($1, $2, $3, $4, $5) ON CONFLICT (user_id) DO
                               UPDATE
                                   SET username = $2,
                                   first_name = $3,
                                   last_name = $4,
                                   last_active = $5,
                                   is_active = TRUE
                               ''', user.id, user.username, user.first_name, user.last_name, datetime.now())

    @staticmethod
    async def save_request(pool: asyncpg.Pool, user_id: int, target: str,
                           request_type: str, stories_count: int,
                           success: bool, processing_time: float,
                           error_message: str = None):
        """Save request log"""
        async with pool.acquire() as conn:
            await conn.execute('''
                               INSERT INTO requests (user_id, target, request_type, stories_count,
                                                     success, processing_time, error_message)
                               VALUES ($1, $2, $3, $4, $5, $6, $7)
                               ''', user_id, target, request_type, stories_count, success,
                               processing_time, error_message)

    @staticmethod
    async def update_daily_stats(pool: asyncpg.Pool):
        """Update daily statistics"""
        async with pool.acquire() as conn:
            await conn.execute('''
                               INSERT INTO statistics (date, total_users, active_users, total_requests,
                                                       successful_requests, failed_requests, total_stories)
                               SELECT CURRENT_DATE,
                                      (SELECT COUNT(*) FROM users),
                                      (SELECT COUNT(*) FROM users WHERE last_active::date = CURRENT_DATE),
                    (SELECT COUNT(*) FROM requests WHERE created_at::date = CURRENT_DATE),
                    (SELECT COUNT(*) FROM requests WHERE created_at::date = CURRENT_DATE AND success = TRUE),
                    (SELECT COUNT(*) FROM requests WHERE created_at::date = CURRENT_DATE AND success = FALSE),
                    (SELECT COALESCE(SUM(stories_count), 0) FROM requests WHERE created_at::date = CURRENT_DATE)
                               ON CONFLICT (date) DO
                               UPDATE SET
                                   total_users = EXCLUDED.total_users,
                                   active_users = EXCLUDED.active_users,
                                   total_requests = EXCLUDED.total_requests,
                                   successful_requests = EXCLUDED.successful_requests,
                                   failed_requests = EXCLUDED.failed_requests,
                                   total_stories = EXCLUDED.total_stories
                               ''')

    @staticmethod
    async def get_user_stats(pool: asyncpg.Pool, user_id: int) -> dict:
        """Get user statistics"""
        async with pool.acquire() as conn:
            row = await conn.fetchrow('''
                                      SELECT COUNT(*) as total_requests,
                                             COUNT(*)    FILTER (WHERE success = TRUE) as successful_requests, COALESCE(SUM(stories_count), 0) as total_stories
                                      FROM requests
                                      WHERE user_id = $1
                                      ''', user_id)
            return dict(row) if row else {}

    @staticmethod
    async def get_global_stats(pool: asyncpg.Pool) -> dict:
        """Get global statistics"""
        async with pool.acquire() as conn:
            row = await conn.fetchrow('''
                                      SELECT (SELECT COUNT(*) FROM users)                           as total_users,
                                             (SELECT COUNT(*) FROM users WHERE is_active = TRUE)    as active_users,
                                             (SELECT COUNT(*) FROM requests)                        as total_requests,
                                             (SELECT COUNT(*) FROM requests WHERE success = TRUE)   as successful_requests,
                                             (SELECT COALESCE(SUM(stories_count), 0) FROM requests) as total_stories,
                                             (SELECT COUNT(*) FROM requests WHERE created_at::date = CURRENT_DATE) as today_requests
                                      ''')
            return dict(row) if row else {}


class StoriesDownloader:
    """Stories downloader using Telethon userbot"""

    @staticmethod
    async def get_entity_from_input(input_text: str):
        """Get Telegram entity from username, phone or link"""
        import re

        input_text = input_text.strip()

        # Direct story link: t.me/username/s/story_id
        story_link_pattern = r't\.me/([^/]+)/s/(\d+)'
        match = re.search(story_link_pattern, input_text)
        if match:
            username = match.group(1)
            return await userbot.get_entity(username), 'direct_link'

        # Username with @
        if input_text.startswith('@'):
            return await userbot.get_entity(input_text), 'username'

        # Phone number
        if re.match(r'^\+?\d{10,15}$', input_text):
            return await userbot.get_entity(input_text), 'phone'

        # Username without @
        try:
            return await userbot.get_entity(input_text), 'username'
        except:
            return await userbot.get_entity(f'@{input_text}'), 'username'

    @staticmethod
    async def download_stories(entity) -> List[Dict]:
        """Download active and pinned stories"""
        all_stories = []
        max_stories = config.app.max_stories_per_request

        try:
            # Get active stories
            active = await userbot(GetPeerStoriesRequest(peer=entity))
            if active and hasattr(active, 'stories') and active.stories:
                for story in active.stories.stories[:max_stories]:
                    if hasattr(story, 'media') and story.media:
                        all_stories.append({
                            'story': story,
                            'type': 'active'
                        })
                        logger.info(f"Found active story ID: {story.id}")
        except Exception as e:
            logger.error(f"Error getting active stories: {e}")

        try:
            # Get pinned stories
            remaining = max_stories - len(all_stories)
            if remaining > 0:
                pinned = await userbot(GetPinnedStoriesRequest(
                    peer=entity,
                    offset_id=0,
                    limit=remaining
                ))
                if pinned and hasattr(pinned, 'stories') and pinned.stories:
                    for story in pinned.stories:
                        if hasattr(story, 'media') and story.media:
                            all_stories.append({
                                'story': story,
                                'type': 'pinned'
                            })
                            logger.info(f"Found pinned story ID: {story.id}")
        except Exception as e:
            logger.error(f"Error getting pinned stories: {e}")

        if not all_stories:
            return []

        # Download media
        downloaded = []
        for item in all_stories:
            try:
                story = item['story']
                buffer = await userbot.download_media(story.media, file=bytes)

                if buffer:
                    # Determine media type
                    media_type = 'photo'
                    if hasattr(story.media, 'video'):
                        media_type = 'video'
                    elif hasattr(story.media, 'document'):
                        media_type = 'video'

                    downloaded.append({
                        'buffer': buffer,
                        'media_type': media_type,
                        'story_type': item['type'],
                        'story_id': story.id
                    })
                    logger.info(f"Downloaded {media_type} story ID: {story.id}")

                # Flood protection
                await asyncio.sleep(config.app.flood_wait_delay)

            except FloodWaitError as e:
                logger.warning(f"FloodWait: {e.seconds}s")
                await asyncio.sleep(e.seconds)
            except Exception as e:
                logger.error(f"Error downloading story {story.id}: {e}")

        return downloaded


@dp.message(Command('start'))
async def cmd_start(message: types.Message):
    """Handle /start command"""
    await DatabaseManager.save_user(db_pool, message.from_user)

    welcome_text = """
🕵️ **Telegram Stories Viewer Bot**

Anonim ravishda istalgan foydalanuvchining Telegram stories'larini ko'ring!

📝 **Qanday foydalanish:**
• Username yuboring: `@username`
• Username (@ siz): `username`
• Telefon raqami: `+998901234567`
• Story linki: `t.me/username/s/123456`

✨ **Bot nimalarni yuklab beradi:**
• 📌 Active stories (joriy aktiv)
• 📍 Pinned stories (mahkamlangan)

⚡️ Boshlash uchun username, telefon yoki linkni yuboring!

📊 Statistikani ko'rish: /stats
❓ Yordam: /help
    """
    await message.answer(welcome_text, parse_mode='Markdown')


@dp.message(Command('help'))
async def cmd_help(message: types.Message):
    """Handle /help command"""
    help_text = """
📖 **Yordam**

**Qo'llab-quvvatlanadigan formatlar:**
• `@username` - Username bilan
• `durov` - Username (@ siz)
• `+998901234567` - Telefon raqami
• `t.me/username/s/123456` - To'g'ridan-to'g'ri story linki

**Misol:**
```
@durov
durov
+79001234567
t.me/durov/s/123456
```

**Buyruqlar:**
• /start - Botni ishga tushirish
• /help - Yordam
• /stats - Statistika
• /about - Bot haqida

❓ Savollar bo'lsa, /start buyrug'ini yuboring.
    """
    await message.answer(help_text, parse_mode='Markdown')


@dp.message(Command('stats'))
async def cmd_stats(message: types.Message):
    """Handle /stats command"""
    await DatabaseManager.save_user(db_pool, message.from_user)

    user_stats = await DatabaseManager.get_user_stats(db_pool, message.from_user.id)
    global_stats = await DatabaseManager.get_global_stats(db_pool)

    stats_text = f"""
📊 **Sizning statistikangiz:**
• So'rovlar: {user_stats.get('total_requests', 0)}
• Muvaffaqiyatli: {user_stats.get('successful_requests', 0)}
• Yuklab olingan stories: {user_stats.get('total_stories', 0)}

🌍 **Umumiy statistika:**
• Jami foydalanuvchilar: {global_stats.get('total_users', 0):,}
• Aktiv foydalanuvchilar: {global_stats.get('active_users', 0):,}
• Jami so'rovlar: {global_stats.get('total_requests', 0):,}
• Bugungi so'rovlar: {global_stats.get('today_requests', 0):,}
• Yuklab olingan stories: {global_stats.get('total_stories', 0):,}
    """
    await message.answer(stats_text, parse_mode='Markdown')


@dp.message(Command('about'))
async def cmd_about(message: types.Message):
    """Handle /about command"""
    about_text = """
ℹ️ **Bot haqida**

🕵️ Telegram Stories Viewer Bot - anonim stories ko'rish uchun bot.

**Texnologiyalar:**
• Python 3.11
• Telethon (MTProto)
• aiogram (Bot API)
• PostgreSQL

**Xususiyatlar:**
✅ Active stories
✅ Pinned stories  
✅ Photo & Video qo'llab-quvvatlash
✅ Username/Telefon/Link orqali qidiruv

🔒 100% anonim va xavfsiz!

💻 Open Source: [GitHub](https://github.com/your-repo)
    """
    await message.answer(about_text, parse_mode='Markdown')


@dp.message(F.text)
async def handle_message(message: types.Message):
    """Handle all text messages"""
    await DatabaseManager.save_user(db_pool, message.from_user)

    input_text = message.text.strip()

    # Skip commands
    if input_text.startswith('/'):
        return

    start_time = datetime.now()
    status_msg = await message.answer("🔍 Qidiryapman...")

    try:
        # Get entity
        entity, request_type = await StoriesDownloader.get_entity_from_input(input_text)

        if not entity:
            await status_msg.edit_text("❌ Foydalanuvchi topilmadi!")
            processing_time = (datetime.now() - start_time).total_seconds()
            await DatabaseManager.save_request(
                db_pool, message.from_user.id, input_text,
                request_type, 0, False, processing_time, "User not found"
            )
            return

        await status_msg.edit_text("📥 Stories yuklanmoqda...")

        # Download stories
        stories = await StoriesDownloader.download_stories(entity)

        if not stories:
            await status_msg.edit_text("😔 Aktiv stories topilmadi!")
            processing_time = (datetime.now() - start_time).total_seconds()
            await DatabaseManager.save_request(
                db_pool, message.from_user.id, input_text,
                request_type, 0, False, processing_time, "No stories found"
            )
            return

        await status_msg.edit_text(f"📤 {len(stories)} ta story yuborilmoqda...")

        # Send stories
        for idx, story in enumerate(stories, 1):
            try:
                file = BufferedInputFile(
                    story['buffer'],
                    filename=f"story_{story['story_id']}.jpg"
                )

                caption = f"📸 Story #{idx}/{len(stories)}\n"
                caption += f"📌 Type: {story['story_type'].title()}\n"
                caption += f"🆔 ID: {story['story_id']}"

                if story['media_type'] == 'photo':
                    await message.answer_photo(file, caption=caption)
                else:
                    await message.answer_video(file, caption=caption)

                await asyncio.sleep(config.app.flood_wait_delay)

            except Exception as e:
                logger.error(f"Error sending story {story['story_id']}: {e}")

        await status_msg.delete()
        await message.answer(
            f"✅ {len(stories)} ta story muvaffaqiyatli yuborildi!\n\n"
            f"📊 Statistikani ko'rish: /stats"
        )

        processing_time = (datetime.now() - start_time).total_seconds()
        await DatabaseManager.save_request(
            db_pool, message.from_user.id, input_text,
            request_type, len(stories), True, processing_time
        )

        # Update daily stats
        await DatabaseManager.update_daily_stats(db_pool)

    except UsernameNotOccupiedError:
        await status_msg.edit_text("❌ Bunday username topilmadi!")
        processing_time = (datetime.now() - start_time).total_seconds()
        await DatabaseManager.save_request(
            db_pool, message.from_user.id, input_text,
            'username', 0, False, processing_time, "Username not found"
        )
    except PhoneNumberInvalidError:
        await status_msg.edit_text("❌ Telefon raqami noto'g'ri!")
        processing_time = (datetime.now() - start_time).total_seconds()
        await DatabaseManager.save_request(
            db_pool, message.from_user.id, input_text,
            'phone', 0, False, processing_time, "Invalid phone number"
        )
    except FloodWaitError as e:
        await status_msg.edit_text(f"⏳ Iltimos {e.seconds} soniya kuting va qayta urinib ko'ring.")
        processing_time = (datetime.now() - start_time).total_seconds()
        await DatabaseManager.save_request(
            db_pool, message.from_user.id, input_text,
            'unknown', 0, False, processing_time, f"FloodWait: {e.seconds}s"
        )
    except Exception as e:
        logger.error(f"Error processing request: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ Xatolik yuz berdi.\n\nQayta urinib ko'ring yoki /help ni ko'ring.")
        processing_time = (datetime.now() - start_time).total_seconds()
        await DatabaseManager.save_request(
            db_pool, message.from_user.id, input_text,
            'unknown', 0, False, processing_time, str(e)
        )


async def init_userbot():
    """Initialize Telethon userbot"""
    global userbot

    userbot_config = config.userbot

    userbot = TelegramClient(
        userbot_config.session_name,
        userbot_config.api_id,
        userbot_config.api_hash
    )

    await userbot.start(phone=userbot_config.phone)

    if await userbot.is_user_authorized():
        me = await userbot.get_me()
        logger.info(f"✅ Userbot connected: {me.first_name} (@{me.username})")
    else:
        logger.error("❌ Userbot not authorized!")
        raise Exception("Userbot authentication failed")


async def main():
    """Main function"""
    global db_pool

    logger.info("🚀 Starting Telegram Stories Viewer Bot...")

    # Initialize database
    logger.info("📦 Connecting to PostgreSQL...")
    db_pool = await DatabaseManager.create_pool()
    await DatabaseManager.init_db(db_pool)
    logger.info("✅ Database connected")

    # Initialize userbot
    logger.info("🤖 Initializing userbot...")
    await init_userbot()

    # Start bot
    logger.info("🤖 Starting bot...")
    logger.info(f"🔗 Bot username: @{(await bot.get_me()).username}")

    try:
        await dp.start_polling(bot)
    finally:
        logger.info("🛑 Shutting down...")
        await db_pool.close()
        await userbot.disconnect()
        logger.info("👋 Goodbye!")


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Stopped by user")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}", exc_info=True)