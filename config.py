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
    admin_ids: list[int]

    @classmethod
    def from_env(cls):
        token = os.getenv('BOT_TOKEN')
        if not token:
            raise ValueError("BOT_TOKEN is not set in .env file")

        # Admin IDs (vergul bilan ajratilgan)
        admin_ids_str = os.getenv('ADMIN_IDS', '')
        admin_ids = [int(id.strip()) for id in admin_ids_str.split(',') if id.strip()]

        return cls(
            token=token,
            admin_ids=admin_ids
        )


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

        if not api_id or not api_hash or not phone:
            raise ValueError("USERBOT_API_ID, USERBOT_API_HASH, and USERBOT_PHONE must be set")

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
            port=int(os.getenv('DB_PORT', '5432')),
            name=os.getenv('DB_NAME', 'stories_bot'),
            user=os.getenv('DB_USER', 'postgres'),
            password=os.getenv('DB_PASS', ''),
            min_pool_size=int(os.getenv('DB_MIN_POOL_SIZE', '5')),
            max_pool_size=int(os.getenv('DB_MAX_POOL_SIZE', '20'))
        )

    @property
    def dsn(self) -> str:
        """Get database DSN"""
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"


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
            debug=os.getenv('DEBUG', 'False').lower() == 'true',
            log_level=os.getenv('LOG_LEVEL', 'INFO'),
            max_stories_per_request=int(os.getenv('MAX_STORIES_PER_REQUEST', '100')),
            request_timeout=int(os.getenv('REQUEST_TIMEOUT', '60')),
            flood_wait_delay=float(os.getenv('FLOOD_WAIT_DELAY', '0.5'))
        )


@dataclass
class Config:
    """Main configuration container"""
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

    def validate(self):
        """Validate configuration"""
        errors = []

        if not self.bot.token:
            errors.append("Bot token is empty")

        if self.userbot.api_id <= 0:
            errors.append("Invalid API ID")

        if len(self.userbot.api_hash) < 32:
            errors.append("Invalid API Hash")

        if not self.userbot.phone.startswith('+'):
            errors.append("Phone number must start with +")

        if not self.database.password:
            errors.append("Database password is empty")

        if errors:
            raise ValueError("Configuration errors:\n" + "\n".join(f"- {e}" for e in errors))

        return True


# Singleton instance
_config: Optional[Config] = None


def get_config() -> Config:
    """Get configuration singleton"""
    global _config
    if _config is None:
        _config = Config.load()
        _config.validate()
    return _config


# Convenience functions
def get_bot_config() -> BotConfig:
    return get_config().bot


def get_userbot_config() -> UserbotConfig:
    return get_config().userbot


def get_db_config() -> DatabaseConfig:
    return get_config().database


def get_app_config() -> AppConfig:
    return get_config().app