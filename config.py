"""
Configuration for TCGPlayer card monitoring.
"""

# TCGPlayer URLs to monitor - add your specific card pages here
TCGPLAYER_PAGES_TO_MONITOR = [
    # Example URLs - replace with your actual card pages
    "https://www.tcgplayer.com/product/649586/pokemon-japan-m-p-promotional-cards-pikachu-020-m-p",
    "https://www.tcgplayer.com/product/593355/pokemon-sv-prismatic-evolutions-prismatic-evolutions-elite-trainer-box?page=1&Language=English",
    "https://www.tcgplayer.com/product/593466/pokemon-sv-prismatic-evolutions-prismatic-evolutions-surprise-box?page=1&Language=English",
    "https://www.tcgplayer.com/product/600518/pokemon-sv-prismatic-evolutions-prismatic-evolutions-booster-bundle?page=1&Language=English",
    "https://www.tcgplayer.com/product/528038/pokemon-sv-paldean-fates-paldean-fates-booster-pack?page=1&Language=English",
    "https://www.tcgplayer.com/product/504467/pokemon-sv-scarlet-and-violet-151-151-booster-pack?page=1&Language=English"
]

# Monitoring settings
MONITORING_INTERVAL_SECONDS = 60  # Check every 60 seconds"

HEADLESS_MODE = True  # Set to False to see browser window
MAX_PRICE_ALERT = 100.0  # Alert if any listing is under this price
MIN_CONDITION = "Lightly Played"  # Only monitor cards in this condition or better

# Alert settings
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1420205527295721615/cevkt66p_FCRmPTkg1b4r0lUuejguOfZb4j1v_fo6u6Imb2AU3qVF2S6SdnAWbm6_oUP"
ALERT_ALL_NEW_SALES = True  # Alert for ALL new sales regardless of price
EMAIL_ALERTS = False  # Set to True to enable email alerts
ALERT_EMAIL = None  # Your email for alerts

# Data storage
DATA_FILE = "card_data.json"
LOG_FILE = "monitor.log"

