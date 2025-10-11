"""
TCGPlayer Last Sold Price Monitor using Playwright.
Monitors specific card pages for recent sales and price history.
"""

import asyncio
import json
import logging
import sys
import time
from datetime import datetime
from typing import List, Dict, Any, Optional
from pathlib import Path

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

# Add the project root to the Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from configs.config import (
    TCGPLAYER_PAGES_TO_MONITOR,
    MONITORING_INTERVAL_SECONDS,
    HEADLESS_MODE,
    MAX_PRICE_ALERT,
    MIN_CONDITION,
    DISCORD_WEBHOOK_URL,
    ALERT_ALL_NEW_SALES,
    DATA_FILE,
    LOG_FILE
)

from src.data_classes import LastSoldRecord
from src.utils import (
    extract_price_from_text,
    extract_date_from_text,
    extract_condition_from_text,
    send_discord_alert,
    send_startup_notification
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class TCGPlayerLastSoldMonitor:
    """Monitor for TCGPlayer last sold prices."""
    
    def __init__(self):
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.data_file = Path(DATA_FILE)
        self.previous_records: Dict[str, List[LastSoldRecord]] = {}
        self.load_previous_data()
    
    def load_previous_data(self) -> None:
        """Load previous monitoring data from file."""
        if self.data_file.exists():
            try:
                with open(self.data_file, 'r') as f:
                    data = json.load(f)
                    for page_url, records_data in data.items():
                        self.previous_records[page_url] = [
                            LastSoldRecord.from_dict(record) for record in records_data
                        ]
                logger.info(f"Loaded previous data for {len(self.previous_records)} pages")
            except Exception as e:
                logger.error(f"Failed to load previous data: {e}")
                self.previous_records = {}
        else:
            self.previous_records = {}
    
    def save_data(self) -> None:
        """Save current monitoring data to file."""
        try:
            data = {}
            for page_url, records in self.previous_records.items():
                data[page_url] = [record.to_dict() for record in records]
            
            with open(self.data_file, 'w') as f:
                json.dump(data, f, indent=2)
            logger.info("Data saved successfully")
        except Exception as e:
            logger.error(f"Failed to save data: {e}")
    
    async def start_browser(self) -> None:
        """Start the browser and create context."""
        playwright = await async_playwright().start()
        self.browser = await playwright.chromium.launch(headless=HEADLESS_MODE)
        self.context = await self.browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        )
        logger.info("Browser started")
    
    async def close_browser(self) -> None:
        """Close browser and cleanup."""
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        logger.info("Browser closed")
    
    async def scrape_last_sold(self, page_url: str) -> List[LastSoldRecord]:
        """Scrape last sold information from a TCGPlayer page by opening the sales history modal."""
        if not self.context:
            raise RuntimeError("Browser context not initialized")
        
        page = await self.context.new_page()
        records = []
        
        try:
            logger.info(f"Scraping last sold data from: {page_url}")
            await page.goto(page_url, wait_until='networkidle')
            
            # Wait for page to load completely
            await page.wait_for_timeout(3000)
            
            # Extract card title
            title_selectors = [
                'h1.product-details__name',
                '.product-details__name',
                'h1[data-testid="product-title"]',
                '.product-title',
                'h1'
            ]
            
            card_title = "Unknown Card"
            for selector in title_selectors:
                try:
                    title_element = await page.query_selector(selector)
                    if title_element:
                        card_title = await title_element.inner_text()
                        card_title = card_title.strip()
                        if card_title and card_title != "Unknown Card":
                            break
                except:
                    continue
            
            # Look for and click the "View More Data" or "Sales History" button
            sales_history_button_selectors = [
                'button:has-text("View More Data")',
                'button:has-text("Sales History")',
                'button:has-text("Price History")',
                'button:has-text("Market Data")',
                'a:has-text("View More Data")',
                'a:has-text("Sales History")',
                '.view-more-data',
                '.sales-history-button',
                '.price-history-button',
                '[data-testid="view-more-data"]',
                '[data-testid="sales-history"]'
            ]
            
            logger.info("Looking for sales history button...")
            button_clicked = False
            for selector in sales_history_button_selectors:
                try:
                    button = await page.query_selector(selector)
                    if button:
                        await button.click()
                        logger.info(f"✅ Clicked sales history button: {selector}")
                        button_clicked = True
                        break
                    else:
                        logger.info(f"❌ Button not found: {selector}")
                except Exception as e:
                    logger.info(f"❌ Error with selector {selector}: {e}")
                    continue
            
            if not button_clicked:
                logger.info("Trying to find buttons by text content...")
                # Try to find any button or link containing "more" or "history"
                all_buttons = await page.query_selector_all('button, a')
                logger.info(f"Found {len(all_buttons)} buttons/links on page")
                
                for i, button in enumerate(all_buttons):
                    try:
                        text = await button.inner_text()
                        if text and any(keyword in text.lower() for keyword in ['view more', 'sales history', 'price history', 'market data', 'more data']):
                            await button.click()
                            logger.info(f"✅ Clicked button {i} with text: '{text}'")
                            button_clicked = True
                            break
                        elif text and len(text.strip()) > 0:
                            logger.info(f"Button {i} text: '{text}'")
                    except Exception as e:
                        logger.info(f"Error reading button {i}: {e}")
                        continue
            
            if button_clicked:
                logger.info("Button clicked! Waiting for modal to appear...")
                # Wait for the modal to appear
                await page.wait_for_timeout(2000)
                
                # Look for the sales history modal/table
                modal_selectors = [
                    '.modal',
                    '.sales-history-modal',
                    '.price-history-modal',
                    '.market-data-modal',
                    '[role="dialog"]',
                    '.overlay',
                    '.popup'
                ]
                
                logger.info("Looking for sales history modal...")
                modal_found = False
                for selector in modal_selectors:
                    try:
                        modal = await page.query_selector(selector)
                        if modal:
                            logger.info(f"✅ Found modal: {selector}")
                            # Look for sales history table within the modal
                            table_selectors = [
                                'table',
                                '.sales-table',
                                '.price-history-table',
                                '.market-data-table',
                                '.transactions-table'
                            ]
                            
                            for table_selector in table_selectors:
                                table = await modal.query_selector(table_selector)
                                if table:
                                    logger.info(f"✅ Found table in modal: {table_selector}")
                                    records = await self.extract_sales_from_table(table, card_title, page_url)
                                    if records:
                                        logger.info(f"✅ Extracted {len(records)} records from table")
                                        modal_found = True
                                        break
                                    else:
                                        logger.info(f"❌ No records extracted from table: {table_selector}")
                            
                            if modal_found:
                                break
                        else:
                            logger.info(f"❌ Modal not found: {selector}")
                    except Exception as e:
                        logger.info(f"❌ Error looking for modal {selector}: {e}")
                        continue
                
                if not modal_found:
                    logger.info("Modal not found, trying to find sales data directly on page...")
                    # Try to find sales data directly on the page after clicking
                    records = await self.extract_sales_from_page(page, card_title, page_url)
                    if records:
                        logger.info(f"✅ Found {len(records)} records from page")
                    else:
                        logger.info("❌ No records found from page")
            else:
                logger.info("❌ No sales history button found - trying to get most recent sale price directly...")
                # Skip main page scraping and go directly to most recent sale price
                records = []
            
            # If no sales history found, try to get most recent sale price
            if not records:
                most_recent_sale = await self.get_most_recent_sale_price(page)
                if most_recent_sale > 0:
                    record = LastSoldRecord(
                        title=card_title,
                        price=most_recent_sale,
                        condition="Most Recent Sale",
                        sold_date="Recent",
                        url=page_url
                    )
                    records.append(record)
                    logger.info(f"Using most recent sale price: {card_title} - ${most_recent_sale}")
                else:
                    # Fallback to current market price
                    current_price = await self.get_current_market_price(page)
                    if current_price > 0:
                        record = LastSoldRecord(
                            title=card_title,
                            price=current_price,
                            condition="Current Market",
                            sold_date="Current",
                            url=page_url
                        )
                        records.append(record)
                        logger.info(f"Using current market price: {card_title} - ${current_price}")
            
            logger.info(f"Found {len(records)} last sold records for {card_title}")
            return records
            
        except Exception as e:
            logger.error(f"Error scraping last sold data from {page_url}: {e}")
            return []
        finally:
            await page.close()
    
    async def extract_sales_from_table(self, table, card_title: str, page_url: str) -> List[LastSoldRecord]:
        """Extract sales records from a table element."""
        records = []
        try:
            rows = await table.query_selector_all('tr')
            for row in rows[1:]:  # Skip header row
                try:
                    row_text = await row.inner_text()
                    if row_text:
                        # Extract price, date, and condition from row text
                        price = extract_price_from_text(row_text)
                        if price > 0:
                            date = extract_date_from_text(row_text)
                            condition = extract_condition_from_text(row_text)
                            
                            record = LastSoldRecord(
                                title=card_title,
                                price=price,
                                condition=condition,
                                sold_date=date,
                                url=page_url
                            )
                            records.append(record)
                            logger.info(f"Found from table: {card_title} - ${price} ({condition}) - {date}")
                except:
                    continue
        except:
            pass
        return records
    
    async def extract_sales_from_page(self, page, card_title: str, page_url: str) -> List[LastSoldRecord]:
        """Extract sales records from the page after clicking the button."""
        records = []
        try:
            # Look for any elements containing sales data
            sales_elements = await page.query_selector_all('*')
            for element in sales_elements:
                try:
                    text = await element.inner_text()
                    if text and any(keyword in text.lower() for keyword in ['last sold', 'recent sale', 'sold for', 'last sale', 'sold on']):
                        price = extract_price_from_text(text)
                        if price > 0:
                            date = extract_date_from_text(text)
                            condition = extract_condition_from_text(text)
                            
                            record = LastSoldRecord(
                                title=card_title,
                                price=price,
                                condition=condition,
                                sold_date=date,
                                url=page_url
                            )
                            records.append(record)
                            logger.info(f"Found from page: {card_title} - ${price} ({condition}) - {date}")
                except:
                    continue
        except:
            pass
        return records
    
    async def get_most_recent_sale_price(self, page: Page) -> float:
        """Get most recent sale price from TCGPlayer price points section."""
        logger.info("Looking for most recent sale price...")
        
        try:
            # Get ALL elements with the price-points__upper__price class
            elements = await page.query_selector_all('.price-points__upper__price')
            logger.info(f"Found {len(elements)} elements with price-points__upper__price class")
            
            if len(elements) >= 2:
                # Get the SECOND element (index 1) - the most recent sale
                second_element = elements[1]
                text = await second_element.inner_text()
                price = extract_price_from_text(text)
                
                if price > 0:
                    logger.info(f"✅ Found most recent sale price: ${price} (second element)")
                    return price
                else:
                    logger.info(f"❌ Second element found but no price: '{text}'")
            elif len(elements) == 1:
                # Only one element found, use it
                first_element = elements[0]
                text = await first_element.inner_text()
                price = extract_price_from_text(text)
                
                if price > 0:
                    logger.info(f"✅ Found price: ${price} (only one element found)")
                    return price
                else:
                    logger.info(f"❌ Only element found but no price: '{text}'")
            else:
                logger.info("❌ No price-points__upper__price elements found")
                
        except Exception as e:
            logger.info(f"❌ Error finding price-points__upper__price elements: {e}")
        
        # Fallback to other selectors if the main one fails
        fallback_selectors = [
            '.price-points_upper_price',    # Alternative spelling
            '.most-recent-sale',
            '.recent-sale-price',
            '[data-testid="most-recent-sale"]',
            '.price-points .upper .price'
        ]
        
        for selector in fallback_selectors:
            try:
                element = await page.query_selector(selector)
                if element:
                    text = await element.inner_text()
                    price = extract_price_from_text(text)
                    if price > 0:
                        logger.info(f"✅ Found price using fallback selector: ${price} - {selector}")
                        return price
            except Exception as e:
                logger.info(f"❌ Error with fallback selector {selector}: {e}")
                continue
        
        logger.info("❌ No most recent sale price found")
        return 0.0
    
    async def get_current_market_price(self, page: Page) -> float:
        """Get current market price as fallback."""
        price_selectors = [
            '.market-price',
            '.current-price',
            '.price-value',
            '[data-testid="price"]',
            '.product-price',
            '.marketplace-price'
        ]
        
        for selector in price_selectors:
            try:
                element = await page.query_selector(selector)
                if element:
                    text = await element.inner_text()
                    price = extract_price_from_text(text)
                    if price > 0:
                        return price
            except:
                continue
        
        return 0.0
    
    def compare_records(self, page_url: str, current_records: List[LastSoldRecord]) -> List[Dict[str, Any]]:
        """Compare current records with previous ones and return changes."""
        previous = self.previous_records.get(page_url, [])
        changes = []
        
        # Check for new sales
        current_prices = {record.price for record in current_records}
        previous_prices = {record.price for record in previous}
        
        new_prices = current_prices - previous_prices
        for record in current_records:
            if record.price in new_prices:
                # Always alert on new sales if ALERT_ALL_NEW_SALES is enabled
                if ALERT_ALL_NEW_SALES:
                    changes.append({
                        'type': 'new_sale',
                        'record': record,
                        'message': f"💰 New Sale: {record.title} - ${record.price} ({record.condition}) - {record.sold_date}"
                    })
        
        return changes
    
    async def monitor_pages(self) -> None:
        """Monitor all configured pages for last sold data."""
        logger.info(f"Starting to monitor {len(TCGPLAYER_PAGES_TO_MONITOR)} pages for last sold data")
        
        for page_url in TCGPLAYER_PAGES_TO_MONITOR:
            try:
                current_records = await self.scrape_last_sold(page_url)
                
                if current_records:
                    changes = self.compare_records(page_url, current_records)
                    
                    # Send alerts for changes
                    for change in changes:
                        logger.info(change['message'])
                        send_discord_alert(change['message'], DISCORD_WEBHOOK_URL)
                    
                    # Update stored records
                    self.previous_records[page_url] = current_records
                
                # Small delay between pages
                await asyncio.sleep(2)
                
            except Exception as e:
                logger.error(f"Error monitoring page {page_url}: {e}")
        
        # Save updated data
        self.save_data()
    
    async def run_monitoring_loop(self) -> None:
        """Run the main monitoring loop."""
        logger.info("Starting TCGPlayer last sold monitoring loop...")
        
        try:
            await self.start_browser()
            
            # Send startup notification to Discord
            send_startup_notification(DISCORD_WEBHOOK_URL, TCGPLAYER_PAGES_TO_MONITOR, MONITORING_INTERVAL_SECONDS)
            
            while True:
                start_time = time.time()
                
                await self.monitor_pages()
                
                elapsed = time.time() - start_time
                sleep_time = max(0, MONITORING_INTERVAL_SECONDS - elapsed)
                
                logger.info(f"Monitoring cycle complete. Next check in {sleep_time:.1f} seconds")
                await asyncio.sleep(sleep_time)
                
        except KeyboardInterrupt:
            logger.info("Monitoring stopped by user")
        except Exception as e:
            logger.error(f"Monitoring error: {e}")
        finally:
            await self.close_browser()


async def main():
    """Main entry point."""
    monitor = TCGPlayerLastSoldMonitor()
    await monitor.run_monitoring_loop()
# --- add this in scripts/tcgplayer_last_sold_monitor.py ---

def fetch_last_sold_once(url: str) -> dict:
    """
    TEMPORARY stub so the Render deploy works.
    Replace this with real scraping logic later,
    or call into existing utilities here.
    """
    return {
        "url": url,
        "most_recent_sale": None,
        "note": "stub fetch_last_sold_once is wired; implement real scrape next"
    }


if __name__ == "__main__":
    asyncio.run(main())
