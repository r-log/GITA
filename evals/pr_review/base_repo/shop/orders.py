"""Order pricing. Money is integer cents; rates are Decimals."""

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal


@dataclass
class LineItem:
    product_id: int
    unit_price_cents: int
    quantity: int


@dataclass
class Order:
    items: list[LineItem] = field(default_factory=list)
    discount_percent: Decimal = Decimal("0")

    def add(self, item: LineItem) -> None:
        if item.quantity <= 0:
            raise ValueError("quantity must be positive")
        self.items.append(item)


def subtotal_cents(order: Order) -> int:
    return sum(item.unit_price_cents * item.quantity for item in order.items)


def _round_cents(amount: Decimal) -> int:
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def total_cents(order: Order, tax_rate: Decimal) -> int:
    """Discount first, then tax on the discounted amount."""
    subtotal = Decimal(subtotal_cents(order))
    discounted = subtotal * (Decimal("1") - order.discount_percent / Decimal("100"))
    return _round_cents(discounted * (Decimal("1") + tax_rate))
