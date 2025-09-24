"""
Configuration loader for TCGPlayer card monitoring.
Loads configuration from config.yaml file.
"""

import yaml
from pathlib import Path
from typing import List, Dict, Any, Optional

# Path to the config file
CONFIG_FILE = Path(__file__).parent / "config.yaml"

# Global config variable
_config: Optional[Dict[str, Any]] = None


def load_config() -> Dict[str, Any]:
    """Load configuration from YAML file."""
    global _config
    
    if _config is not None:
        return _config
    
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"Configuration file not found: {CONFIG_FILE}")
    
    try:
        with open(CONFIG_FILE, 'r') as f:
            _config = yaml.safe_load(f)
        return _config
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML configuration: {e}")
    except Exception as e:
        raise RuntimeError(f"Failed to load configuration: {e}")


def get_config_value(key_path: str, default: Any = None) -> Any:
    """
    Get a configuration value using dot notation.
    
    Args:
        key_path: Dot-separated path to the config value (e.g., 'monitoring.interval_seconds')
        default: Default value if key is not found
    
    Returns:
        Configuration value or default
    """
    config = load_config()
    
    # Split the key path and navigate through the config
    keys = key_path.split('.')
    value = config
    
    try:
        for key in keys:
            value = value[key]
        return value
    except (KeyError, TypeError):
        return default


# Configuration constants for backward compatibility
def _get_tcgplayer_pages() -> List[str]:
    """Get TCGPlayer pages to monitor."""
    return get_config_value('tcgplayer_pages_to_monitor', [])


def _get_monitoring_interval() -> int:
    """Get monitoring interval in seconds."""
    return get_config_value('monitoring.interval_seconds', 60)


def _get_headless_mode() -> bool:
    """Get headless mode setting."""
    return get_config_value('monitoring.headless_mode', True)


def _get_max_price_alert() -> float:
    """Get maximum price alert threshold."""
    return get_config_value('monitoring.max_price_alert', 100.0)


def _get_min_condition() -> str:
    """Get minimum condition filter."""
    return get_config_value('monitoring.min_condition', "Lightly Played")


def _get_discord_webhook_url() -> str:
    """Get Discord webhook URL."""
    return get_config_value('alerts.discord_webhook_url', "")


def _get_alert_all_new_sales() -> bool:
    """Get alert all new sales setting."""
    return get_config_value('alerts.alert_all_new_sales', True)


def _get_email_alerts() -> bool:
    """Get email alerts setting."""
    return get_config_value('alerts.email_alerts', False)


def _get_alert_email() -> Optional[str]:
    """Get alert email address."""
    return get_config_value('alerts.alert_email', None)


def _get_data_file() -> str:
    """Get data file path."""
    return get_config_value('storage.data_file', "card_data.json")


def _get_log_file() -> str:
    """Get log file path."""
    return get_config_value('storage.log_file', "monitor.log")


# Export configuration constants for backward compatibility
TCGPLAYER_PAGES_TO_MONITOR = _get_tcgplayer_pages()
MONITORING_INTERVAL_SECONDS = _get_monitoring_interval()
HEADLESS_MODE = _get_headless_mode()
MAX_PRICE_ALERT = _get_max_price_alert()
MIN_CONDITION = _get_min_condition()
DISCORD_WEBHOOK_URL = _get_discord_webhook_url()
ALERT_ALL_NEW_SALES = _get_alert_all_new_sales()
EMAIL_ALERTS = _get_email_alerts()
ALERT_EMAIL = _get_alert_email()
DATA_FILE = _get_data_file()
LOG_FILE = _get_log_file()