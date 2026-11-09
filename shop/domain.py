"""Offers are built here from stock and seller rules. No model output sets a price or a line."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Product:
    id: str
    name: str
    unit_cents: int
    stock: int


@dataclass(frozen=True)
class Line:
    product_id: str
    name: str
    qty: int
    unit_cents: int


@dataclass(frozen=True)
class Draft:
    kind: str  # exact, substitute, partial
    lines: tuple[Line, ...]

    @property
    def total_cents(self) -> int:
        return sum(line.qty * line.unit_cents for line in self.lines)


def _line(product: Product, qty: int) -> Line:
    return Line(product.id, product.name, qty, product.unit_cents)


def build_offers(wanted: Product, qty: int, substitutes: list[Product]) -> list[Draft]:
    """Exact if it fits. Otherwise what is in stock, topped up from approved substitutes."""
    if qty <= 0:
        return []
    if wanted.stock >= qty:
        return [Draft("exact", (_line(wanted, qty),))]
    drafts: list[Draft] = []
    lines = [_line(wanted, wanted.stock)] if wanted.stock else []
    missing = qty - wanted.stock
    for substitute in substitutes:
        if missing == 0:
            break
        take = min(substitute.stock, missing)
        if take:
            lines.append(_line(substitute, take))
            missing -= take
    if missing == 0 and lines:
        drafts.append(Draft("substitute", tuple(lines)))
    if wanted.stock:
        drafts.append(Draft("partial", (_line(wanted, wanted.stock),)))
    return drafts
