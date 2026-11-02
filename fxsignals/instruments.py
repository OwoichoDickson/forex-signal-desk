"""Instrument specifications: pip conventions, contract sizes and pip-value conversion."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional


@dataclass(frozen=True)
class Instrument:
    name: str           # e.g. "EURUSD"
    base: str           # e.g. "EUR" (XAU for gold)
    quote: str          # e.g. "USD"
    pip_size: float     # price increment that counts as one pip
    contract_size: float  # units per standard lot
    price_decimals: int

    @property
    def is_jpy(self) -> bool:
        return self.quote == "JPY"

    @property
    def is_metal(self) -> bool:
        return self.base in ("XAU", "XAG")

    def to_pips(self, price_distance: float) -> float:
        return abs(price_distance) / self.pip_size

    def round_price(self, price: Optional[float]) -> Optional[float]:
        return None if price is None else round(price, self.price_decimals)

    def pip_value_per_lot_quote(self) -> float:
        """Value of one pip for one standard lot, in the quote currency."""
        return self.pip_size * self.contract_size


def instrument(name: str) -> Instrument:
    name = name.upper()
    base, quote = name[:3], name[3:6]
    if base == "XAU":
        # Gold: 1 lot = 100 oz, 1 pip = $0.10 (so 1 pip per lot = $10), 2-decimal quotes.
        return Instrument(name, base, quote, 0.1, 100.0, 2)
    if quote == "JPY":
        return Instrument(name, base, quote, 0.01, 100_000.0, 3)
    return Instrument(name, base, quote, 0.0001, 100_000.0, 5)


class RateBook:
    """Currency conversion from live mid prices of the monitored pairs.

    ``rates`` maps pair name -> price (e.g. {"USDJPY": 157.5}). Conversion tries the
    direct pair, the inverse pair, then a cross through USD.
    """

    def __init__(self, rates: Mapping[str, Optional[float]]):
        self.rates = {k.upper(): v for k, v in rates.items() if v}

    def convert(self, amount: float, from_ccy: str, to_ccy: str) -> Optional[float]:
        rate = self.rate(from_ccy, to_ccy)
        return None if rate is None else amount * rate

    def rate(self, from_ccy: str, to_ccy: str) -> Optional[float]:
        from_ccy, to_ccy = from_ccy.upper(), to_ccy.upper()
        if from_ccy == to_ccy:
            return 1.0
        direct = self.rates.get(from_ccy + to_ccy)
        if direct:
            return direct
        inverse = self.rates.get(to_ccy + from_ccy)
        if inverse:
            return 1.0 / inverse
        if "USD" not in (from_ccy, to_ccy):
            a, b = self.rate(from_ccy, "USD"), self.rate("USD", to_ccy)
            if a and b:
                return a * b
        return None


def pip_value_per_lot(inst: Instrument, account_ccy: str, book: RateBook) -> Optional[float]:
    """Pip value for one standard lot, in the account currency (None if no conversion rate)."""
    return book.convert(inst.pip_value_per_lot_quote(), inst.quote, account_ccy)
