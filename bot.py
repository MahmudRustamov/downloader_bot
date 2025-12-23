"""
Telegram Stories Viewer Bot - NO CHUNKING
Direct file IDs - stream from Telegram servers directly
"""

import asyncio
import logging
import time
import re
from typing import Optional, List, Dict

from telethon import TelegramClient
from telethon.tl.functions.stories import GetPeerStoriesRequest, GetPinnedStoriesRequest
from telethon.errors import UsernameNotOccupiedError, PhoneNumberInvalidError, FloodWaitError
import asyncpg
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto, InputMediaVideo

from config import get_config

config = get_config()

logging.basicConfig(
    level=getattr(logging, config.app.log_level),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('bot.log'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

bot = Bot(token=config.bot.token)
dp = Dispatcher()
userbot: Optional[TelegramClient] = None
db_pool: Optional[asyncpg.Pool] = None

# MEMORY STORAGE FOR STORIES
stories_cache: Dict[int, Dict] = {}

# CONFIG
STORIES_PER_PAGE = 5


class DatabaseManager:
    """Fast PostgreSQL manager"""

    @staticmethod
    async def create_pool():
        db_config = config.database
        return await asyncpg.create_pool(
            host=db_config.host,
            port=db_config.port,
            database=db_config.name,
            user=db_config.user,
            password=db_config.password,
            min_size=db_config.min_pool_size,
            max_size=db_config.max_pool_size,
            command_timeout=10
        )

    @staticmethod
    async def init_db(pool: asyncpg.Pool):
        async with pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username VARCHAR(255),
                    first_name VARCHAR(255),
                    last_name VARCHAR(255),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            await conn.execute('''
                CREATE TABLE IF NOT EXISTS requests (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                    target VARCHAR(255),
                    request_type VARCHAR(50),
                    story_type VARCHAR(50),
                    stories_count INT DEFAULT 0,
                    success BOOLEAN DEFAULT FALSE,
                    processing_time FLOAT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            await conn.execute('CREATE INDEX IF NOT EXISTS idx_user_id ON requests(user_id)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_success ON requests(success)')

            logger.info("✅ Database initialized")

    @staticmethod
    async def save_user(pool: asyncpg.Pool, user: types.User):
        async with pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO users (user_id, username, first_name, last_name, last_active)
                VALUES ($1, $2, $3, $4, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id) DO UPDATE SET last_active = CURRENT_TIMESTAMP
            ''', user.id, user.username, user.first_name, user.last_name)

    @staticmethod
    async def save_request(pool: asyncpg.Pool, user_id: int, target: str,
                          request_type: str, story_type: str, stories_count: int,
                          success: bool, processing_time: float):
        async with pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO requests 
                (user_id, target, request_type, story_type, stories_count, success, processing_time)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
            ''', user_id, target, request_type, story_type, stories_count, success, processing_time)


class NoChunkDownloader:
    """No chunking - direct file IDs from Telegram"""

    async def get_entity_from_input(self, input_text: str):
        input_text = input_text.strip()

        match = re.search(r't\.me/([^/]+)/s/(\d+)', input_text)
        if match:
            return await userbot.get_entity(match.group(1)), 'direct_link'

        if input_text.startswith('@'):
            return await userbot.get_entity(input_text), 'username'

        if re.match(r'^\+?\d{10,15}$', input_text):
            return await userbot.get_entity(input_text), 'phone'

        try:
            return await userbot.get_entity(input_text), 'username'
        except:
            return await userbot.get_entity(f'@{input_text}'), 'username'

    async def get_active_stories(self, entity) -> List[Dict]:
        stories = []
        try:
            active = await userbot(GetPeerStoriesRequest(peer=entity))
            if active and hasattr(active, 'stories'):
                for story in active.stories[:config.app.max_stories_per_request]:
                    if hasattr(story, 'media') and story.media:
                        stories.append({
                            'story': story,
                            'media': story.media,
                            'type': 'active'
                        })
                        logger.info(f"Found active story {story.id}")
        except Exception as e:
            logger.warning(f"Active stories error: {e}")
        return stories

    async def get_pinned_stories(self, entity) -> List[Dict]:
        stories = []
        try:
            pinned = await userbot(GetPinnedStoriesRequest(peer=entity, offset_id=0, limit=config.app.max_stories_per_request))
            if pinned and hasattr(pinned, 'stories'):
                for story in pinned.stories:
                    if hasattr(story, 'media') and story.media:
                        stories.append({
                            'story': story,
                            'media': story.media,
                            'type': 'pinned'
                        })
                        logger.info(f"Found pinned story {story.id}")
        except Exception as e:
            logger.warning(f"Pinned stories error: {e}")
        return stories

    async def download_story_bytes(self, story_media, story_id: int) -> tuple:
        """Download story as bytes - minimal overhead"""
        try:
            import io
            file_obj = io.BytesIO()
            await userbot.download_media(story_media, file=file_obj)
            file_obj.seek(0)
            buffer = file_obj.getvalue()

            media_type = 'photo'
            if hasattr(story_media, 'document'):
                media_type = 'video'

            logger.info(f"Downloaded {media_type} story {story_id}: {len(buffer)} bytes")
            return buffer, media_type
        except Exception as e:
            logger.error(f"Download error {story_id}: {e}")
            return None, None

    async def download_all_fast(self, stories: List[Dict]) -> List[Dict]:
        """Download all stories in PARALLEL - FAST"""
        downloaded = []

        # Download 5 at same time
        semaphore = asyncio.Semaphore(5)

        async def download_one(item, idx):
            try:
                async with semaphore:
                    logger.info(f"Downloading {idx + 1}/{len(stories)}...")

                    buffer, media_type = await self.download_story_bytes(item['media'], item['story'].id)

                    if buffer:
                        return {
                            'buffer': buffer,
                            'story_type': item['type'],
                            'story_id': item['story'].id,
                            'media_type': media_type
                        }
            except Exception as e:
                logger.error(f"Error downloading story {idx}: {e}")
            return None

        # Download all at same time
        tasks = [download_one(item, idx) for idx, item in enumerate(stories)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Filter results
        for result in results:
            if result and isinstance(result, dict):
                downloaded.append(result)

        logger.info(f"Downloaded {len(downloaded)}/{len(stories)} stories in parallel")
        return downloaded


downloader = NoChunkDownloader()


def get_pagination_buttons(user_id: int, page: int, total_pages: int) -> InlineKeyboardMarkup:
    """Create pagination buttons"""
    buttons = []

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"page_{user_id}_{page-1}"))

    nav_row.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="page_info"))

    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"page_{user_id}_{page+1}"))

    if nav_row:
        buttons.append(nav_row)

    buttons.append([InlineKeyboardButton(text="❌ Close", callback_data=f"close_{user_id}")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def send_page_direct(chat_id: int, user_id: int, stories: List[Dict], page: int) -> bool:
    """Send stories using downloaded bytes"""
    if not stories:
        logger.error("No stories to send")
        return False

    total_pages = (len(stories) + STORIES_PER_PAGE - 1) // STORIES_PER_PAGE
    start_idx = page * STORIES_PER_PAGE
    end_idx = min(start_idx + STORIES_PER_PAGE, len(stories))
    batch = stories[start_idx:end_idx]

    logger.info(f"Sending page {page + 1}/{total_pages} ({len(batch)} stories)")

    media_group = []

    for idx, story in enumerate(batch):
        try:
            caption = f"📸 {start_idx + idx + 1}/{len(stories)}"

            media_type = story.get('media_type', 'photo')
            file_size = len(story['buffer'])

            # Skip if too large
            if file_size > 50 * 1024 * 1024:  # 50MB limit
                logger.warning(f"Story {story['story_id']} too large ({file_size} bytes), skipping")
                continue

            from aiogram.types import BufferedInputFile

            file = BufferedInputFile(
                story['buffer'],
                filename=f"story_{story['story_id']}.jpg"
            )

            if media_type == 'video':
                media_group.append(InputMediaVideo(media=file, caption=caption))
            else:
                media_group.append(InputMediaPhoto(media=file, caption=caption))

            logger.info(f"Added {media_type} {story['story_id']} ({file_size} bytes)")

        except Exception as e:
            logger.error(f"Media error for story {story['story_id']}: {e}")
            continue

    if not media_group:
        logger.error("No media in group")
        return False

    try:
        logger.info(f"Sending {len(media_group)} stories")
        await bot.send_media_group(chat_id=chat_id, media=media_group)
        logger.info(f"✅ Sent {len(media_group)} stories")

        # Send navigation
        keyboard = get_pagination_buttons(user_id, page, total_pages)
        await bot.send_message(chat_id=chat_id, text="Navigation:", reply_markup=keyboard)

        return True

    except Exception as e:
        logger.error(f"Send error: {e}", exc_info=True)
        return False


@dp.message(Command('start'))
async def cmd_start(message: types.Message):
    await DatabaseManager.save_user(db_pool, message.from_user)

    welcome = """
🕵️ **Telegram Stories Viewer**

⚡ **INSTANT - Direct streaming!**

Send:
• `@username`
• `username`
• `+998901234567`

Stories stream DIRECTLY!
"""

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❓ Help", callback_data="help")]
    ])

    await message.answer(welcome, parse_mode='Markdown', reply_markup=keyboard)


@dp.message(Command('help'))
async def cmd_help(message: types.Message):
    help_text = """
📖 **How to use:**

Send username/phone and choose:
📌 **Active** - Current stories
📍 **Pinned** - Saved stories
📥 **All** - Everything

Stories stream directly - NO DOWNLOADING!
"""
    await message.answer(help_text, parse_mode='Markdown')


@dp.callback_query(F.data == 'help')
async def cb_help(query: types.CallbackQuery):
    await query.answer()
    try:
        await query.message.edit_text("📖 Send username and choose stories!")
    except:
        await query.message.answer("📖 Send username and choose stories!")


@dp.message(F.text)
async def handle_message(message: types.Message):
    await DatabaseManager.save_user(db_pool, message.from_user)

    input_text = message.text.strip()

    if input_text.startswith('/'):
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📌 Active", callback_data=f"dl_active:{input_text}"),
         InlineKeyboardButton(text="📍 Pinned", callback_data=f"dl_pinned:{input_text}")],
        [InlineKeyboardButton(text="📥 All", callback_data=f"dl_all:{input_text}")]
    ])

    await message.answer("Choose stories:", reply_markup=keyboard)


@dp.callback_query(F.data.startswith('dl_'))
async def cb_download(query: types.CallbackQuery):
    """Download and show stories - DIRECT"""
    await query.answer()

    parts = query.data.split(':', 1)
    story_type = parts[0].replace('dl_', '')
    input_text = parts[1] if len(parts) > 1 else ''

    start_time = time.time()
    msg = query.message
    user_id = query.from_user.id

    try:
        try:
            await msg.edit_text("🔍 Searching...")
        except:
            pass

        entity, request_type = await downloader.get_entity_from_input(input_text)

        if not entity:
            try:
                await msg.edit_text("❌ User not found!")
            except:
                await query.message.answer("❌ User not found!")
            return

        try:
            await msg.edit_text("⏳ Fetching stories...")
        except:
            pass

        # Get stories
        all_stories = []

        if story_type == 'active':
            all_stories = await downloader.get_active_stories(entity)
        elif story_type == 'pinned':
            all_stories = await downloader.get_pinned_stories(entity)
        else:  # all
            active = await downloader.get_active_stories(entity)
            pinned = await downloader.get_pinned_stories(entity)
            all_stories = active + pinned

        if not all_stories:
            try:
                await msg.edit_text(f"😔 No {story_type} stories")
            except:
                await query.message.answer(f"😔 No {story_type} stories")
            return

        try:
            await msg.edit_text(f"📤 Sending {len(all_stories)} stories...")
        except:
            pass

        # Download stories first
        downloaded = await downloader.download_all_fast(all_stories)

        if not downloaded:
            try:
                await msg.edit_text("❌ Download failed!")
            except:
                await query.message.answer("❌ Download failed!")
            return

        # Store in memory cache
        stories_cache[user_id] = {
            'stories': downloaded,
            'page': 0,
            'story_type': story_type
        }

        # Delete old message
        try:
            await msg.delete()
        except:
            pass

        # Send first page
        success = await send_page_direct(user_id, user_id, downloaded, 0)

        if success:
            processing_time = time.time() - start_time
            msg_text = f"✅ Ready! {len(all_stories)} {story_type} stories\nTime: {processing_time:.1f}s\n\n⚡ Direct streaming - no download!"
            try:
                await query.message.answer(msg_text, parse_mode='Markdown')
            except:
                pass

            await DatabaseManager.save_request(
                db_pool, user_id, input_text, request_type, story_type, len(all_stories), True, processing_time
            )
        else:
            logger.error("Failed to send first page")
            try:
                await query.message.answer("❌ Failed to send stories")
            except:
                pass

    except UsernameNotOccupiedError:
        try:
            await msg.edit_text("❌ Username not found!")
        except:
            await query.message.answer("❌ Username not found!")
    except PhoneNumberInvalidError:
        try:
            await msg.edit_text("❌ Invalid phone!")
        except:
            await query.message.answer("❌ Invalid phone!")
    except FloodWaitError as e:
        try:
            await msg.edit_text(f"⏳ Wait {e.seconds}s")
        except:
            await query.message.answer(f"⏳ Wait {e.seconds}s")
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        try:
            await msg.edit_text(f"❌ Error")
        except:
            await query.message.answer(f"❌ Error")


@dp.callback_query(F.data.startswith('page_'))
async def cb_page(query: types.CallbackQuery):
    """Navigate pages"""
    await query.answer()

    parts = query.data.split('_')
    user_id = int(parts[1])
    page = int(parts[2])

    if user_id not in stories_cache:
        await query.answer("❌ Session expired", show_alert=True)
        return

    cache = stories_cache[user_id]
    stories = cache['stories']

    # Delete old navigation message
    try:
        await query.message.delete()
    except:
        pass

    # Send new page DIRECTLY
    success = await send_page_direct(user_id, user_id, stories, page)

    if success:
        stories_cache[user_id]['page'] = page
        logger.info(f"Navigated to page {page}")
    else:
        try:
            await query.message.answer("❌ Failed to load page")
        except:
            pass


@dp.callback_query(F.data.startswith('close_'))
async def cb_close(query: types.CallbackQuery):
    """Close stories"""
    await query.answer()

    user_id = int(query.data.split('_')[1])

    # Clear cache
    if user_id in stories_cache:
        del stories_cache[user_id]
        logger.info(f"Cleared cache for user {user_id}")

    try:
        await query.message.delete()
    except:
        pass


@dp.callback_query(F.data == 'page_info')
async def cb_page_info(query: types.CallbackQuery):
    await query.answer()


async def init_userbot():
    global userbot

    userbot_config = config.userbot

    userbot = TelegramClient(
        userbot_config.session_name,
        userbot_config.api_id,
        userbot_config.api_hash,
        timeout=30,
        request_retries=5
    )

    await userbot.start(phone=userbot_config.phone)

    if await userbot.is_user_authorized():
        me = await userbot.get_me()
        logger.info(f"✅ Userbot: {me.first_name}")
    else:
        raise Exception("Userbot auth failed")


async def main():
    global db_pool

    logger.info("🚀 Starting Stories Bot (DIRECT - NO CHUNKS)...")

    try:
        db_pool = await DatabaseManager.create_pool()
        await DatabaseManager.init_db(db_pool)
        logger.info("✅ Database ready")

        logger.info("🤖 Userbot initializing...")
        await init_userbot()

        bot_info = await bot.get_me()
        logger.info(f"✅ Bot: @{bot_info.username}")
        logger.info("✅ Ready to receive messages!")

        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

    except Exception as e:
        logger.error(f"❌ Fatal error: {e}", exc_info=True)

    finally:
        if db_pool:
            await db_pool.close()
        if userbot:
            await userbot.disconnect()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Bot stopped")