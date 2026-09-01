BTC + Liquidity Reversal Staged — Railway package

Strategy:
- BTC 15m closed candle: MA25/MA99 direction + global exit gate.
- Liquidity Reversal TRAP/PO3+RSI selects coins and entry timing.
- $300 notional after leverage per position.
- Up to 20 positions.
- Target leverage 20x; falls back one-by-one to the highest lower leverage.
- SL: -50% leveraged ROI.
- TP1: +100% ROI, close 50%, then SL to breakeven.
- TP2: +150% ROI, close 25%.
- TP3: +200% ROI, close final 25%.
- Basket target: estimated NET +$20 after close fees.
- After basket close, wait for a new closed BTC 15m candle.
- Excluded: BNBUSDT, DOGEUSDT, BCHUSDT.

Railway:
Put these files at the repository root and set the variables from .env.example.
