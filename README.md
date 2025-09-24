# TCGPlayer Card Monitor

A simple Playwright-based script to monitor specific TCGPlayer card pages for new listings and price changes.

## Features

- **Page-specific monitoring**: Monitor any TCGPlayer card page by URL
- **Price change detection**: Alerts when prices change on existing listings
- **New listing alerts**: Notifications when new listings appear
- **Price threshold alerts**: Custom alerts for cards under a certain price
- **Condition filtering**: Only monitor cards in specified conditions or better
- **Discord notifications**: Send alerts to Discord webhook
- **Data persistence**: Tracks listings over time with JSON storage

## Quick Start

### 1. Installation

```bash
# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers
playwright install
```

### 2. Configuration

Edit `config.py` to add your TCGPlayer pages and settings:

```python
# Add your specific card pages here
TCGPLAYER_PAGES_TO_MONITOR = [
    "https://www.tcgplayer.com/product/123456/magic-the-gathering-card-name",
    "https://www.tcgplayer.com/product/789012/magic-the-gathering-another-card",
    # Add more URLs as needed
]

# Set your Discord webhook URL for alerts
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/YOUR_WEBHOOK_URL"

# Configure monitoring settings
MONITORING_INTERVAL_SECONDS = 300  # Check every 5 minutes
MAX_PRICE_ALERT = 100.0  # Alert if any listing is under this price
MIN_CONDITION = "Lightly Played"  # Only monitor cards in this condition or better
```

### 3. Run the Monitor

```bash
python tcgplayer_monitor.py
```

## Configuration Options

### Pages to Monitor
Add TCGPlayer product URLs to `TCGPLAYER_PAGES_TO_MONITOR` list. These should be direct links to specific card pages.

### Monitoring Settings
- `MONITORING_INTERVAL_SECONDS`: How often to check for changes (default: 5 minutes)
- `HEADLESS_MODE`: Set to `False` to see the browser window
- `MAX_PRICE_ALERT`: Alert threshold for low prices
- `MIN_CONDITION`: Minimum card condition to monitor

### Alert Settings
- `DISCORD_WEBHOOK_URL`: Discord webhook for notifications
- `EMAIL_ALERTS`: Enable email alerts (not implemented yet)
- `ALERT_EMAIL`: Email address for alerts

## How It Works

1. **Scraping**: Uses Playwright to load TCGPlayer pages and extract listing data
2. **Comparison**: Compares current listings with previously stored data
3. **Alerting**: Sends Discord notifications for:
   - New listings
   - Price changes
   - Cards under price threshold
4. **Storage**: Saves listing data to `card_data.json` for persistence

## Output

The script will:
- Log all activity to `monitor.log`
- Save listing data to `card_data.json`
- Send Discord alerts for changes
- Print status updates to console

## Example Discord Alerts

- 🆕 New listing: Black Lotus - $15,000.00 (Near Mint)
- 💰 Price change: Mox Pearl - $8,500.00 → $8,200.00 (Near Mint)
- 🚨 ALERT: Time Walk - $45.00 (Lightly Played) - Under $100.00!

## Stopping the Monitor

Press `Ctrl+C` to stop monitoring. The script will save current data and close gracefully.

## Troubleshooting

### No listings found
- Check if the TCGPlayer page URL is correct
- Verify the page has listings available
- Try running with `HEADLESS_MODE = False` to see what's happening

### Discord alerts not working
- Verify your Discord webhook URL is correct
- Check that the webhook has permission to send messages

### Browser issues
- Make sure Playwright browsers are installed: `playwright install`
- Try running with `HEADLESS_MODE = False` to debug

## Adding More Pages

Simply add more TCGPlayer URLs to the `TCGPLAYER_PAGES_TO_MONITOR` list in `config.py`:

```python
TCGPLAYER_PAGES_TO_MONITOR = [
    "https://www.tcgplayer.com/product/123456/magic-the-gathering-card-name",
    "https://www.tcgplayer.com/product/789012/magic-the-gathering-another-card",
    "https://www.tcgplayer.com/product/345678/magic-the-gathering-third-card",
    # Add as many as you want to monitor
]
```

The script will automatically monitor all pages in the list.

