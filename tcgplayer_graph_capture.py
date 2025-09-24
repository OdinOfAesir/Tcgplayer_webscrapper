"""
TCGPlayer Price Graph Capture Script
Captures price history graphs from TCGPlayer and sends them to Discord.
Runs once on startup, then every hour on the hour.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional

from playwright.async_api import async_playwright, Browser, BrowserContext, Page
import requests

from config import TCGPLAYER_PAGES_TO_MONITOR

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('graph_capture.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Configuration
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1420256969452945438/GTxKqljdcDsyispXGcHZM8RTH8fFWQ55b1xBWpI_zC5Znyp7pZLc1He9D2q6zQMDbAfR"
HEADLESS_MODE = True
GRAPH_CAPTURE_DIR = "captured_graphs"


class TCGPlayerGraphCapture:
    """Capture price graphs from TCGPlayer pages."""
    
    def __init__(self):
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.capture_dir = Path(GRAPH_CAPTURE_DIR)
        self.capture_dir.mkdir(exist_ok=True)
    
    async def start_browser(self) -> None:
        """Start the browser and create context."""
        playwright = await async_playwright().start()
        self.browser = await playwright.chromium.launch(headless=HEADLESS_MODE)
        self.context = await self.browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        )
        logger.info("Browser started for graph capture")
    
    async def close_browser(self) -> None:
        """Close browser and cleanup."""
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        logger.info("Browser closed")
    
    async def capture_price_graph(self, page_url: str) -> Optional[str]:
        """Capture the price history graph from a TCGPlayer page."""
        if not self.context:
            raise RuntimeError("Browser context not initialized")
        
        page = await self.context.new_page()
        
        try:
            logger.info(f"Capturing price graph from: {page_url}")
            await page.goto(page_url, wait_until='networkidle')
            
            # Wait for page to load completely
            await page.wait_for_timeout(3000)
            
            # Look for the chart container with the price history
            chart_selectors = [
                'div[data-testid="History_Line"]',
                '.chart-container',
                '.martech-charts-chart',
                'canvas[data-v-8daf4e1f]',
                'div.chart-container canvas'
            ]
            
            chart_element = None
            for selector in chart_selectors:
                try:
                    element = await page.query_selector(selector)
                    if element:
                        chart_element = element
                        logger.info(f"Found chart element with selector: {selector}")
                        break
                except Exception as e:
                    logger.info(f"Selector {selector} failed: {e}")
                    continue
            
            if not chart_element:
                logger.error("No chart element found")
                return None
            
            # Generate filename with timestamp
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            card_name = self.extract_card_name_from_url(page_url)
            filename = f"{card_name}_{timestamp}.png"
            filepath = self.capture_dir / filename
            
            # Capture screenshot of the chart area
            await chart_element.screenshot(path=str(filepath))
            logger.info(f"Graph captured: {filepath}")
            
            return str(filepath)
            
        except Exception as e:
            logger.error(f"Error capturing graph from {page_url}: {e}")
            return None
        finally:
            await page.close()
    
    def extract_card_name_from_url(self, url: str) -> str:
        """Extract card name from TCGPlayer URL."""
        try:
            if 'product/' in url:
                parts = url.split('/')
                if len(parts) > 4:
                    card_name = parts[4].replace('-', '_')
                    return card_name
        except:
            pass
        return "unknown_card"
    
    async def send_graph_to_discord(self, image_path: str, page_url: str) -> bool:
        """Send captured graph to Discord webhook."""
        if not DISCORD_WEBHOOK_URL:
            logger.error("No Discord webhook URL configured")
            return False
        
        try:
            # Read the image file
            with open(image_path, 'rb') as f:
                image_data = f.read()
            
            # Prepare the Discord message
            card_name = self.extract_card_name_from_url(page_url)
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            message = f"""📊 **TCGPlayer Price Graph Captured**

🃏 **Card:** {card_name.replace('_', ' ').title()}
⏰ **Time:** {timestamp}
🔗 **URL:** {page_url}

📈 Price history graph captured successfully!"""
            
            # Prepare files for Discord
            files = {
                'file': (image_path, image_data, 'image/png')
            }
            
            # Prepare payload
            payload = {
                'content': message,
                'username': 'TCGPlayer Graph Capture'
            }
            
            # Send to Discord
            response = requests.post(
                DISCORD_WEBHOOK_URL,
                data={'payload_json': json.dumps(payload)},
                files=files,
                timeout=30
            )
            response.raise_for_status()
            
            logger.info("Graph sent to Discord successfully")
            return True
            
        except Exception as e:
            logger.error(f"Failed to send graph to Discord: {e}")
            return False
    
    async def capture_all_graphs(self) -> None:
        """Capture graphs from all configured pages."""
        logger.info(f"Starting graph capture for {len(TCGPLAYER_PAGES_TO_MONITOR)} pages")
        
        for page_url in TCGPLAYER_PAGES_TO_MONITOR:
            try:
                # Capture the graph
                image_path = await self.capture_price_graph(page_url)
                
                if image_path:
                    # Send to Discord
                    success = await self.send_graph_to_discord(image_path, page_url)
                    if success:
                        logger.info(f"Successfully captured and sent graph for {page_url}")
                    else:
                        logger.error(f"Failed to send graph for {page_url}")
                else:
                    logger.error(f"Failed to capture graph for {page_url}")
                
                # Small delay between pages
                await asyncio.sleep(2)
                
            except Exception as e:
                logger.error(f"Error processing page {page_url}: {e}")
    
    async def run_hourly_capture(self) -> None:
        """Run the main hourly capture loop."""
        logger.info("Starting TCGPlayer graph capture service...")
        
        try:
            await self.start_browser()
            
            # Send startup notification
            await self.send_startup_notification()
            
            # Run initial capture
            logger.info("Running initial graph capture...")
            await self.capture_all_graphs()
            
            # Calculate next hour
            now = datetime.now()
            next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            logger.info(f"Next capture scheduled for: {next_hour}")
            
            while True:
                # Wait until the next hour
                now = datetime.now()
                if now >= next_hour:
                    logger.info("Hourly capture triggered")
                    await self.capture_all_graphs()
                    
                    # Schedule next hour
                    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
                    logger.info(f"Next capture scheduled for: {next_hour}")
                
                # Sleep for 1 minute and check again
                await asyncio.sleep(60)
                
        except KeyboardInterrupt:
            logger.info("Graph capture service stopped by user")
        except Exception as e:
            logger.error(f"Graph capture service error: {e}")
        finally:
            await self.close_browser()
    
    async def send_startup_notification(self) -> None:
        """Send startup notification to Discord."""
        if not DISCORD_WEBHOOK_URL:
            logger.info("No Discord webhook configured - skipping startup notification")
            return
        
        try:
            card_count = len(TCGPLAYER_PAGES_TO_MONITOR)
            startup_message = f"""🚀 **TCGPlayer Graph Capture Service Started!**

📊 **Monitoring {card_count} cards for price graphs:**
{chr(10).join([f"• {self.extract_card_name_from_url(url).replace('_', ' ').title()}" for url in TCGPLAYER_PAGES_TO_MONITOR])}

⏰ **Schedule:** Every hour on the hour
📈 **Capturing:** Price history graphs
📁 **Storage:** {GRAPH_CAPTURE_DIR}/

✅ Service is running! You'll receive graph captures every hour."""

            payload = {
                "content": startup_message,
                "username": "TCGPlayer Graph Capture"
            }

            response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
            response.raise_for_status()
            logger.info("Startup notification sent to Discord")

        except Exception as e:
            logger.error(f"Failed to send startup notification: {e}")


async def main():
    """Main entry point."""
    capture_service = TCGPlayerGraphCapture()
    await capture_service.run_hourly_capture()


if __name__ == "__main__":
    asyncio.run(main())
