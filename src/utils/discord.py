"""
Discord integration utilities for sending alerts and notifications.
"""

import logging
import requests
from typing import List

logger = logging.getLogger(__name__)


def send_discord_alert(message: str, webhook_url: str) -> None:
    """Send alert to Discord webhook."""
    if not webhook_url:
        return
    
    try:
        payload = {
            "content": message,
            "username": "TCGPlayer Last Sold Monitor"
        }
        response = requests.post(webhook_url, json=payload, timeout=10)
        response.raise_for_status()
        logger.info("Discord alert sent successfully")
    except Exception as e:
        logger.error(f"Failed to send Discord alert: {e}")


def send_startup_notification(webhook_url: str, pages_to_monitor: List[str], monitoring_interval_seconds: int) -> None:
    """Send startup notification to Discord."""
    if not webhook_url:
        logger.info("No Discord webhook configured - skipping startup notification")
        return
    
    try:
        # Create a nice startup message
        card_count = len(pages_to_monitor)
        check_interval = monitoring_interval_seconds // 60  # Convert to minutes
        
        # Extract card names from URLs for a cleaner message
        card_names = []
        for url in pages_to_monitor:
            # Try to extract card name from URL
            if 'product/' in url:
                parts = url.split('/')
                if len(parts) > 4:
                    card_name = parts[4].replace('-', ' ').title()
                    card_names.append(card_name)
                else:
                    card_names.append("Unknown Card")
            else:
                card_names.append("Unknown Card")
        
        # Create the startup message
        startup_message = f"""🚀 **TCGPlayer Monitor Started!**

📊 **Monitoring {card_count} cards:**
{chr(10).join([f"• {name}" for name in card_names])}

⏰ **Check interval:** Every {check_interval} minutes
🔔 **Alerts:** New sales only
📈 **Tracking:** Last sold prices

✅ Ready to monitor! You'll get notified when new sales are detected."""
        
        payload = {
            "content": startup_message,
            "username": "TCGPlayer Last Sold Monitor"
        }
        
        response = requests.post(webhook_url, json=payload, timeout=10)
        response.raise_for_status()
        logger.info("Startup notification sent to Discord")
        
    except Exception as e:
        logger.error(f"Failed to send startup notification: {e}")
