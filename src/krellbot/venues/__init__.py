"""Venue adapters: base protocol plus Kraken, Coinbase, Binance, Gate, Pionex, OKX, Bybit, KuCoin, Ourbit, Toobit, XT, MEXC, Deepcoin, LBank, Bitget, and Crypto.com spot.

`Venue` lives in `krellbot.venues.base`. Every adapter accepts Decimal for
size/price, and `qty`/`stop` are quantized to the venue's lot/price decimals
with `ROUND_DOWN` before they leave this process.
"""

from __future__ import annotations

from .base import (
    Balance,
    Fill,
    KeyPerms,
    OpenOrder,
    OrderRef,
    PairRules,
    Truth,
    Venue,
    WithdrawCapableError,
)

__all__ = [
    "Balance",
    "BinanceVenue",
    "BitgetVenue",
    "BybitVenue",
    "CoinbaseVenue",
    "CryptocomVenue",
    "DeepcoinVenue",
    "Fill",
    "GateVenue",
    "KeyPerms",
    "KrakenVenue",
    "KucoinVenue",
    "LbankVenue",
    "MexcVenue",
    "OkxVenue",
    "OpenOrder",
    "OrderRef",
    "OurbitVenue",
    "PairRules",
    "PionexVenue",
    "ToobitVenue",
    "Truth",
    "Venue",
    "WithdrawCapableError",
    "XtVenue",
]


def __dir__() -> list[str]:
    """Keep lazy public adapters discoverable without importing them."""
    return sorted(set(globals()) | set(__all__))


def __getattr__(name: str) -> object:
    """Load remote adapters only when their public re-export is requested."""
    if name == "BinanceVenue":
        from .binance import BinanceVenue

        globals()[name] = BinanceVenue
        return BinanceVenue
    if name == "BitgetVenue":
        from .bitget import BitgetVenue

        globals()[name] = BitgetVenue
        return BitgetVenue
    if name == "BybitVenue":
        from .bybit import BybitVenue

        globals()[name] = BybitVenue
        return BybitVenue
    if name == "CoinbaseVenue":
        from .coinbase import CoinbaseVenue

        globals()[name] = CoinbaseVenue
        return CoinbaseVenue
    if name == "CryptocomVenue":
        from .cryptocom import CryptocomVenue

        globals()[name] = CryptocomVenue
        return CryptocomVenue
    if name == "DeepcoinVenue":
        from .deepcoin import DeepcoinVenue

        globals()[name] = DeepcoinVenue
        return DeepcoinVenue
    if name == "GateVenue":
        from .gate import GateVenue

        globals()[name] = GateVenue
        return GateVenue
    if name == "PionexVenue":
        from .pionex import PionexVenue

        globals()[name] = PionexVenue
        return PionexVenue
    if name == "KucoinVenue":
        from .kucoin import KucoinVenue

        globals()[name] = KucoinVenue
        return KucoinVenue
    if name == "LbankVenue":
        from .lbank import LbankVenue

        globals()[name] = LbankVenue
        return LbankVenue
    if name == "MexcVenue":
        from .mexc import MexcVenue

        globals()[name] = MexcVenue
        return MexcVenue
    if name == "KrakenVenue":
        from .kraken import KrakenVenue

        globals()[name] = KrakenVenue
        return KrakenVenue
    if name == "OkxVenue":
        from .okx import OkxVenue

        globals()[name] = OkxVenue
        return OkxVenue
    if name == "OurbitVenue":
        from .ourbit import OurbitVenue

        globals()[name] = OurbitVenue
        return OurbitVenue
    if name == "ToobitVenue":
        from .toobit import ToobitVenue

        globals()[name] = ToobitVenue
        return ToobitVenue
    if name == "XtVenue":
        from .xt import XtVenue

        globals()[name] = XtVenue
        return XtVenue
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
