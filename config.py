"""
Configuration file for Telegram Stories Viewer Bot
"""

import os
from dataclasses import dataclass
from typing import Optional
from dotenv import load_dotenv

# Load environment variables
load_dotenv()


@dataclass
class BotConfig:
    """Bot configuration"""
    token: str
    admin_ids: list

    @classmethod
    def from_env(cls):
        token = os.getenv('BOT_TOKEN')
        if not token:
            raise ValueError("BOT_TOKEN is required in .env")

        admin_ids_str = os.getenv('ADMIN_IDS', '')
        admin_ids = [int(id.strip()) for id in admin_ids_str.split(',') if id.strip()] if admin_ids_str else []

        return cls(token=token, admin_ids=admin_ids)


@dataclass
class UserbotConfig:
    """Userbot configuration"""
    api_id: int
    api_hash: str
    phone: str
    session_name: str

    @classmethod
    def from_env(cls):
        api_id = os.getenv('USERBOT_API_ID')
        api_hash = os.getenv('USERBOT_API_HASH')
        phone = os.getenv('USERBOT_PHONE')

        if not all([api_id, api_hash, phone]):
            raise ValueError("USERBOT_API_ID, USERBOT_API_HASH, USERBOT_PHONE required")

        return cls(
            api_id=int(api_id),
            api_hash=api_hash,
            phone=phone,
            session_name=os.getenv('USERBOT_SESSION', 'userbot_session')
        )


@dataclass
class DatabaseConfig:
    """Database configuration"""
    host: str
    port: int
    name: str
    user: str
    password: str
    min_pool_size: int
    max_pool_size: int

    @classmethod
    def from_env(cls):
        return cls(
            host=os.getenv('DB_HOST', 'localhost'),
            port=int(os.getenv('DB_PORT', 5432)),
            name=os.getenv('DB_NAME', 'stories_bot'),
            user=os.getenv('DB_USER', 'postgres'),
            password=os.getenv('DB_PASS', ''),
            min_pool_size=int(os.getenv('DB_MIN_POOL_SIZE', 5)),
            max_pool_size=int(os.getenv('DB_MAX_POOL_SIZE', 20))
        )


@dataclass
class AppConfig:
    """Application configuration"""
    debug: bool
    log_level: str
    max_stories_per_request: int
    request_timeout: int
    flood_wait_delay: float

    @classmethod
    def from_env(cls):
        return cls(
            debug=os.getenv('DEBUG', 'false').lower() == 'true',
            log_level=os.getenv('LOG_LEVEL', 'INFO'),
            max_stories_per_request=int(os.getenv('MAX_STORIES_PER_REQUEST', 100)),
            request_timeout=int(os.getenv('REQUEST_TIMEOUT', 60)),
            flood_wait_delay=float(os.getenv('FLOOD_WAIT_DELAY', 0.5))
        )


@dataclass
class Config:
    """Main configuration"""
    bot: BotConfig
    userbot: UserbotConfig
    database: DatabaseConfig
    app: AppConfig

    @classmethod
    def load(cls):
        """Load all configurations"""
        return cls(
            bot=BotConfig.from_env(),
            userbot=UserbotConfig.from_env(),
            database=DatabaseConfig.from_env(),
            app=AppConfig.from_env()
        )


# Singleton
_config: Optional[Config] = None


def get_config() -> Config:
    """Get configuration"""
    global _config
    if _config is None:
        _config = Config.load()
    return _config