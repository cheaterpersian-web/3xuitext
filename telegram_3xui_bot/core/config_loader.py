import os
from typing import List
from pydantic import ValidationError
from .config_models import AppConfig, PanelConfig, AdminCredentials, BotConfig

ENV_VAR_TOKEN = 'TELEGRAM_BOT_TOKEN'
ENV_VAR_PANEL_BASE_URL = 'PANEL_BASE_URL'
ENV_VAR_PANEL_USERNAME = 'PANEL_USERNAME'
ENV_VAR_PANEL_PASSWORD = 'PANEL_PASSWORD'
ENV_VAR_PANEL_INSECURE = 'PANEL_INSECURE'
ENV_VAR_ADMIN_IDS = 'BOT_ADMIN_IDS'
ENV_VAR_PER_USER_LIMIT = 'PER_USER_LIMIT'
ENV_VAR_SUBSCRIPTION_BASE_URL = 'SUBSCRIPTION_BASE_URL'

class ConfigError(RuntimeError):
    pass

def _parse_admin_ids(value: str) -> List[int]:
    if not value:
        return []
    result: List[int] = []
    for part in value.split(','):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError:
            raise ConfigError(f"Invalid BOT_ADMIN_IDS entry: {part}")
    return result

def _normalize_base_url(value: str) -> str:
    v = (value or '').strip()
    if not v:
        return v
    low = v.lower()
    if low.startswith('http://') or low.startswith('https://'):
        return v
    # Handle malformed like 'example.com://' => make it 'https://example.com'
    if '://' in v and not low.startswith(('http://', 'https://')):
        host, rest = v.split('://', 1)
        # if rest empty or a path, assume https and prepend
        if not rest or rest.startswith('/'):
            return f'https://{host}{rest}'
    # Default: assume https
    return f'https://{v}'

def load_app_config() -> AppConfig:
    token = os.getenv(ENV_VAR_TOKEN)
    base_url_raw = os.getenv(ENV_VAR_PANEL_BASE_URL)
    username = os.getenv(ENV_VAR_PANEL_USERNAME)
    password = os.getenv(ENV_VAR_PANEL_PASSWORD)
    insecure = os.getenv(ENV_VAR_PANEL_INSECURE, '0') in ('1', 'true', 'TRUE', 'yes', 'on')
    admin_ids_raw = os.getenv(ENV_VAR_ADMIN_IDS, '')
    per_user_limit_raw = os.getenv(ENV_VAR_PER_USER_LIMIT, '1')
    subscription_base_url = os.getenv(ENV_VAR_SUBSCRIPTION_BASE_URL, '')

    if not token or not base_url_raw or not username or not password:
        missing = [name for name, val in [(ENV_VAR_TOKEN, token),(ENV_VAR_PANEL_BASE_URL, base_url_raw),(ENV_VAR_PANEL_USERNAME, username),(ENV_VAR_PANEL_PASSWORD, password)] if not val]
        raise ConfigError(f"Missing required env vars: {', '.join(missing)}")

    try:
        per_user_limit = int(per_user_limit_raw)
    except ValueError:
        raise ConfigError(f"Invalid PER_USER_LIMIT: {per_user_limit_raw}")

    panel = PanelConfig(base_url=_normalize_base_url(base_url_raw), insecure=insecure)
    admin = AdminCredentials(username=username, password=password)
    bot = BotConfig(token=token, admin_numeric_ids=_parse_admin_ids(admin_ids_raw), per_user_limit=per_user_limit)
    app = AppConfig(panel=panel, admin=admin, bot=bot, subscription_base_url=subscription_base_url)
    return app
