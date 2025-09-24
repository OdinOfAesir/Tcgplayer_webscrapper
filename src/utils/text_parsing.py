"""
Text parsing utilities for extracting data from TCGPlayer pages.
"""

import re
from typing import List


def extract_price_from_text(text: str) -> float:
    """Extract price from text."""
    # Look for price patterns like $123.45, $1,234.56, etc.
    price_patterns = [
        r'\$(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)',  # $1,234.56
        r'\$(\d+\.\d{2})',  # $123.45
        r'\$(\d+)',  # $123
    ]
    
    for pattern in price_patterns:
        matches = re.findall(pattern, text)
        if matches:
            try:
                price_str = matches[0].replace(',', '')
                return float(price_str)
            except:
                continue
    
    return 0.0


def extract_date_from_text(text: str) -> str:
    """Extract date from text."""
    # Look for date patterns
    date_patterns = [
        r'(\d{1,2}/\d{1,2}/\d{4})',  # MM/DD/YYYY
        r'(\d{1,2}/\d{1,2}/\d{2})',  # MM/DD/YY
        r'(\d{4}-\d{2}-\d{2})',  # YYYY-MM-DD
        r'(\w+ \d{1,2}, \d{4})',  # Month DD, YYYY
        r'(\d{1,2}/\d{1,2})',  # MM/DD (current year)
        r'(\w+ \d{1,2})',  # Month DD (current year)
    ]
    
    for pattern in date_patterns:
        matches = re.findall(pattern, text)
        if matches:
            return matches[0]
    
    return "Unknown Date"


def extract_condition_from_text(text: str) -> str:
    """Extract condition from text."""
    conditions = [
        "Mint", "Near Mint", "Lightly Played", "Moderately Played", "Heavily Played", "Damaged",
        "NM", "LP", "MP", "HP", "DMG",  # Abbreviations
        "Japanese", "English",  # Language variants
        "Foil", "Non-Foil", "Holo", "Non-Holo"  # Foil variants
    ]
    
    text_lower = text.lower()
    for condition in conditions:
        if condition.lower() in text_lower:
            return condition
    
    return "Unknown Condition"
