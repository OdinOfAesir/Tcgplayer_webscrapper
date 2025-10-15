# TCGPlayer Card Scraper

A professional-grade monitoring system for TCGPlayer card prices and sales data.

## Project Structure

```
card_scraper/
├── src/                          # Source code package
│   ├── data_classes/             # Data models
│   │   ├── __init__.py
│   │   └── last_sold_record.py   # LastSoldRecord data class
│   ├── utils/                    # Utility functions
│   │   ├── __init__.py
│   │   ├── text_parsing.py       # Text extraction utilities
│   │   └── discord.py            # Discord integration
│   └── __init__.py
├── scripts/                      # Executable scripts
│   └── tcgplayer_last_sold_monitor.py  # Main monitoring script
├── configs/                      # Configuration files
│   ├── config.py                 # Configuration loader
│   └── config.yaml               # YAML configuration
├── requirements.txt              # Python dependencies
└── README.md                     # This file
```

## Features

- **Structured Configuration**: YAML-based configuration with hierarchical organization
- **Modular Design**: Clean separation of concerns with dedicated modules
- **Data Classes**: Type-safe data models for card records
- **Utility Functions**: Reusable text parsing and Discord integration
- **Professional Structure**: Follows Python packaging best practices

## Configuration

Edit `configs/config.yaml` to customize:

- **TCGPlayer URLs**: Add card pages to monitor
- **Monitoring Settings**: Check intervals, headless mode, price thresholds
- **Alert Settings**: Discord webhook, email notifications
- **Storage**: Data and log file locations

## Usage

1. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure**:
   Edit `configs/config.yaml` with your TCGPlayer URLs and Discord webhook

3. **Run Monitor**:
   ```bash
   python scripts/tcgplayer_last_sold_monitor.py
   ```

## REST API

- `POST /last-sold`: Extracts the most recent sale for a given listing URL.
- `POST /sales-snapshot`: Captures the sales history snapshot dialog for a product page.
- `POST /active-listings`: Returns every active listing for a product ID, paginating through all result pages.

### Active Listings Endpoint

1. **Start the FastAPI app** (for local development):
   ```bash
   uvicorn app:app --reload
   ```
2. **Call the endpoint** with the product ID you want to inspect:
   ```bash
   curl -X POST http://127.0.0.1:8000/active-listings \
     -H "Content-Type: application/json" \
     -d '{"productId": "12345"}'
   ```
3. **Response payload**:
   ```json
   {
     "product_id": "12345",
     "url": "https://www.tcgplayer.com/...",
     "listings": [
       {
         "condition": "Near Mint",
         "price": 12.5,
         "shippingPrice": 0.99,
         "sellerName": "Example Seller",
         "quantityAvailable": 4,
         "additionalInfo": null
       }
     ],
     "pages_scanned": 2,
     "timestamp": "...",
     "elapsed_ms": 4210
   }
   ```
   Each listing reflects the condition, price, shipping cost, seller name, available quantity, and any extra notes. Shipping defaults to `0.0` when the page does not provide a value.

## Architecture

- **Data Classes**: `LastSoldRecord` for type-safe data handling
- **Utils**: Text parsing and Discord integration utilities
- **Config**: YAML-based configuration with Python loader
- **Scripts**: Main monitoring logic with clean imports
