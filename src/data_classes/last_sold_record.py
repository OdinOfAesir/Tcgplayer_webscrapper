"""
Data class representing a last sold record from TCGPlayer.
"""

from datetime import datetime
from typing import Dict, Any


class LastSoldRecord:
    """Represents a last sold record."""
    
    def __init__(self, title: str, price: float, condition: str, sold_date: str, url: str):
        self.title = title
        self.price = price
        self.condition = condition
        self.sold_date = sold_date
        self.url = url
        self.timestamp = datetime.now()
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON storage."""
        return {
            'title': self.title,
            'price': self.price,
            'condition': self.condition,
            'sold_date': self.sold_date,
            'url': self.url,
            'timestamp': self.timestamp.isoformat()
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'LastSoldRecord':
        """Create from dictionary."""
        record = cls(
            title=data['title'],
            price=data['price'],
            condition=data['condition'],
            sold_date=data['sold_date'],
            url=data['url']
        )
        record.timestamp = datetime.fromisoformat(data['timestamp'])
        return record
