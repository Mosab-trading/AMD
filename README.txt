# MA BTC Sync Bot V1
15m closed candles. SMA7/25/99. BTC determines LONG/SHORT/WAIT; coins must match BTC state.
LONG = close > MA99 and MA25. SHORT = close < MA99 and MA25. Otherwise WAIT.
$300 notional, target 20x with fallback, max 20 positions.
SL -40% ROI via Binance Algo Service. TP1 +100% closes 50% and moves SL to breakeven. TP2 +150% closes rest.
Basket +$20 closes all positions owned by this bot.
Loss window -$100 pauses 3 hours. Three losing basket cycles pause 1 hour.
Telegram includes live Binance USDT Futures balance.
All USDT perpetuals are scanned except very weak liquidity. Because no exact liquidity number was specified, default MIN_QUOTE_VOLUME is 5,000,000 USDT/24h and is configurable in Railway.
The bot never adopts unknown existing account positions.
