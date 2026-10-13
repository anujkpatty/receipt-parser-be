from pydantic import BaseModel, Field
from typing import Literal, Optional, List

# === Receipt Models ===

class Item(BaseModel):
    name: str
    price: Optional[float]

class ReceiptSummary(BaseModel):
    store_name: str
    store_address: str
    store_number: Optional[int]
    items: List[Item]
    tax: Optional[float]
    total: Optional[float]
    date: Optional[str] = Field(
        pattern=r'\d{4}-\d{2}-\d{2}',
        description="Date in the format YYYY-MM-DD"
    )
    payment_method: Literal["cash", "credit", "debit", "check", "other"]


# === Expense Tracking Models ===

Category = Literal[
    "alcohol",
    "beverages",
    "food",
    "groceries",
    "household",
    "personal_care",
    "entertainment",
    "transportation",
    "other",
]
CATEGORIES = list(Category.__args__)

Period = Literal["receipt", "day", "week", "month"]
Severity = Literal["info", "warning", "critical"]

class CategorizedItem(BaseModel):
    index: int = Field(description="Index of the item in the input list")
    category: Category

class CategorizedItems(BaseModel):
    items: List[CategorizedItem]

class SpendingRule(BaseModel):
    # category=None means the rule applies to total spend across all categories.
    category: Optional[Category] = None
    period: Period
    limit: float = Field(gt=0)
