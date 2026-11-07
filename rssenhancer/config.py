"""Single TOML configuration for RssEnhancer (Python 3.12+)."""
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"

# Existing call sites use descriptive setting names; these map directly to TOML paths.
KEYS = {
    "RSSENHANCER_PORT": "app.port",
    "PORT": "app.port",
    "RSSENHANCER_HOST": "app.host",
    "RSSENHANCER_PUBLIC_URL": "app.public_url",
    "RSSENHANCER_DIR": "app.data_dir",
    "RSSENHANCER_LOG_DIR": "app.log_dir",
    "RSSENHANCER_ACCESS_LOG": "app.access_log",
    "SUBSCRIPTION_ADDRESS": "subscriptions.address",
    "IMAP_HOST": "subscriptions.imap.host",
    "IMAP_USER": "subscriptions.imap.user",
    "IMAP_PASSWORD": "subscriptions.imap.password",
    "IMAP_FOLDER": "subscriptions.imap.folder",
    "SUBSCRIPTION_POLL_MINUTES": "subscriptions.poll_minutes",
    "SUBSCRIPTION_LOOKBACK_DAYS": "subscriptions.lookback_days",
    "SUBSCRIPTION_FEED_TITLE": "subscriptions.feed_title",
    "SUBSCRIPTION_FEED_FORMAT": "subscriptions.feed_format",
    "SUBSCRIPTION_SKIP_KEYWORDS": "subscriptions.skip_keywords",
    "SUBSCRIPTION_SKIP_KEYWORDS_FILE": "subscriptions.skip_keywords_file",
    "SUBSCRIPTION_MIN_TOPIC_CHARS": "subscriptions.min_topic_chars",
    "SUMMARY_LIMIT_WORDS": "subscriptions.summary.limit_words",
    "SUMMARY_LONG_WORDS": "subscriptions.summary.long_words",
    "SUMMARY_FACTOR": "subscriptions.summary.factor",
    "SUMMARY_RATIO": "subscriptions.summary.ratio",
    "AI_PROVIDER": "ai.provider",
    "ANTHROPIC_API_KEY": "ai.anthropic.api_key",
    "ANTHROPIC_AUTH_TOKEN": "ai.anthropic.auth_token",
    "ANTHROPIC_BASE_URL": "ai.anthropic.base_url",
    "ANTHROPIC_MODEL": "ai.anthropic.model",
    "GEMINI_API_KEY": "ai.gemini.api_key",
    "GEMINI_MODEL": "ai.gemini.model",
    "OPENAI_API_KEY": "ai.openai.api_key",
    "OPENAI_BASE_URL": "ai.openai.base_url",
    "OPENAI_MODEL": "ai.openai.model",
    "DEEPSEEK_API_KEY": "ai.deepseek.api_key",
    "DEEPSEEK_BASE_URL": "ai.deepseek.base_url",
    "DEEPSEEK_MODEL": "ai.deepseek.model",
    "LOCAL_BASE_URL": "ai.local.base_url",
    "LOCAL_MODEL": "ai.local.model",
    "LOCAL_API_KEY": "ai.local.api_key",
    "LOCAL_ON_MISMATCH": "ai.local.on_mismatch",
    "LOCAL_EXTRA_BODY": "ai.local.extra_body",
    "RAPID_MLX_BIN": "ai.rapid_mlx.binary",
    "RAPID_MLX_ARGS": "ai.rapid_mlx.args",
    "RAPID_MLX_STARTUP_SECONDS": "ai.rapid_mlx.startup_seconds",
    "RAPID_MLX_STOP_AFTER_POLL": "ai.rapid_mlx.stop_after_poll",
}
_data = None
_signature = None

def _read():
    global _data, _signature
    if not CONFIG_PATH.is_file():
        _data, _signature = {}, None
        return _data
    stat = CONFIG_PATH.stat()
    signature = (stat.st_mtime_ns, stat.st_size)
    if signature != _signature:
        with CONFIG_PATH.open("rb") as f:
            _data = tomllib.load(f)
        _signature = signature
    return _data

def get(path, default=None):
    value = _read()
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return default if value is None else value

def setting(name, default=None):
    return get(KEYS.get(name, name), default)

def signature():
    try:
        s = CONFIG_PATH.stat()
        return s.st_mtime_ns, s.st_size
    except OSError:
        return None
