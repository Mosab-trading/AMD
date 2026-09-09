import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mosab-bots")


def env(name, default, cast=str):
    value = os.getenv(name, default)
    if cast is bool:
        return str(value).lower() in {"1", "true", "yes", "on"}
    return cast(value)


BOT_TYPE = "AMD_PO3_ORIGINAL"
BOT_NAME = env("BOT_NAME", "AMD Po3 Original")
START_BALANCE = env("START_BALANCE", 500, float)
LEVERAGE = env("LEVERAGE", 20, int)
NOTIONAL = env("POSITION_NOTIONAL", 200, float)
MAX_POSITIONS = env("MAX_POSITIONS", 20, int)
MAX_TOTAL_POSITIONS = env("MAX_TOTAL_POSITIONS", 50, int)
DRY_RUN = env("DRY_RUN", True, bool)
TIMEFRAME = env("TIMEFRAME", "5m")
SCAN_SECONDS = env("SCAN_SECONDS", 300, int)
CHECK_SECONDS = env("CHECK_SECONDS", 10, int)
TOP_N = env("TOP_N", 40, int)
MIN_QUOTE_VOLUME = env("MIN_QUOTE_VOLUME", 20_000_000, float)
BASE_URL = env("EXCHANGE_BASE_URL", "https://fapi.binance.com").rstrip("/")
STATE_DIR = Path(env("STATE_DIR", "/data"))
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TG_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("TG_CHAT_ID", "")
FEE_RATE = env("FEE_RATE", 0.0005, float)


class Market:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "MosabPaperBots/1.0"})

    def get(self, path, params=None):
        response = self.s.get(BASE_URL + path, params=params, timeout=20)
        response.raise_for_status()
        return response.json()

    def symbols(self):
        info = self.get("/fapi/v1/exchangeInfo")
        allowed = {
            x["symbol"] for x in info["symbols"]
            if x.get("status") == "TRADING"
            and x.get("quoteAsset") == "USDT"
            and x.get("contractType") == "PERPETUAL"
        }
        rows = self.get("/fapi/v1/ticker/24hr")
        ranked = sorted(
            (x for x in rows if x["symbol"] in allowed and float(x.get("quoteVolume", 0)) >= MIN_QUOTE_VOLUME),
            key=lambda x: float(x["quoteVolume"]), reverse=True,
        )
        return [x["symbol"] for x in ranked[:TOP_N]]

    def candles(self, symbol, interval=TIMEFRAME, limit=260):
        rows = self.get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})
        cols = ["open_time", "open", "high", "low", "close", "volume", "close_time", "qv", "n", "tb", "tq", "ignore"]
        df = pd.DataFrame(rows, columns=cols)
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        # Never generate a signal from the currently forming candle.
        return df.iloc[:-1].reset_index(drop=True)

    def prices(self):
        return {x["symbol"]: float(x["price"]) for x in self.get("/fapi/v1/ticker/price")}


def ema(s, n): return s.ewm(span=n, adjust=False).mean()
def sma(s, n): return s.rolling(n).mean()


def atr(df, n=14):
    pc = df.close.shift(1)
    tr = pd.concat([(df.high-df.low), (df.high-pc).abs(), (df.low-pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()


def rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def supertrend(df, n=10, mult=3.0):
    a = atr(df, n)
    hl2 = (df.high + df.low) / 2
    lower, upper = hl2 - mult*a, hl2 + mult*a
    fl, fu = lower.copy(), upper.copy()
    direction = pd.Series(1, index=df.index, dtype=int)
    for i in range(1, len(df)):
        fl.iat[i] = max(lower.iat[i], fl.iat[i-1]) if df.close.iat[i-1] > fl.iat[i-1] else lower.iat[i]
        fu.iat[i] = min(upper.iat[i], fu.iat[i-1]) if df.close.iat[i-1] < fu.iat[i-1] else upper.iat[i]
        if direction.iat[i-1] == -1 and df.close.iat[i] > fu.iat[i-1]: direction.iat[i] = 1
        elif direction.iat[i-1] == 1 and df.close.iat[i] < fl.iat[i-1]: direction.iat[i] = -1
        else: direction.iat[i] = direction.iat[i-1]
    line = pd.Series(np.where(direction == 1, fl, fu), index=df.index)
    return direction, line


def linreg_momentum(df, n=20):
    center = ((df.high.rolling(n).max()+df.low.rolling(n).min())/2 + sma(df.close, n))/2
    raw = df.close-center
    x = np.arange(n)
    return raw.rolling(n).apply(lambda y: np.polyfit(x, y, 1)[0]*(n-1)+np.polyfit(x, y, 1)[1], raw=True)


def squeeze(df, n=20, bb_mult=2.0, kc_mult=1.5):
    basis, dev = sma(df.close, n), bb_mult*df.close.rolling(n).std(ddof=0)
    bb_u, bb_l = basis+dev, basis-dev
    kma, kr = sma(df.close, n), sma(pd.concat([(df.high-df.low), (df.high-df.close.shift()).abs(), (df.low-df.close.shift()).abs()], axis=1).max(axis=1), n)
    kc_u, kc_l = kma+kc_mult*kr, kma-kc_mult*kr
    on = (bb_l > kc_l) & (bb_u < kc_u)
    off = (bb_l < kc_l) & (bb_u > kc_u)
    return on, off, linreg_momentum(df, n)


def session_vwap(df):
    day = pd.to_datetime(df.open_time, unit="ms", utc=True).dt.date
    typical = (df.high+df.low+df.close)/3
    return (typical*df.volume).groupby(day).cumsum()/df.volume.groupby(day).cumsum().replace(0, np.nan)


def fee_adjusted_three_loss_target(entry, stop, side):
    """Target whose net profit equals three net stop losses, including fees."""
    f = FEE_RATE
    if side == "LONG":
        net_loss_per_qty = (entry-stop)+f*(entry+stop)
        return (entry*(1+f)+3*net_loss_per_qty)/(1-f)
    net_loss_per_qty = (stop-entry)+f*(entry+stop)
    return (entry*(1-f)-3*net_loss_per_qty)/(1+f)


def trend_signal(df, htf):
    on, off, mom = squeeze(df)
    st_dir, st_line = supertrend(df)
    macd = ema(htf.close, 12)-ema(htf.close, 26)
    sig = sma(macd, 9)
    hist = macd-sig
    release = bool(on.iat[-2] and off.iat[-1])
    if not release: return None
    if mom.iat[-1] > 0 and mom.iat[-1] > mom.iat[-2] and hist.iat[-1] > 0 and st_dir.iat[-1] == 1:
        return {"side":"LONG", "stop":float(st_line.iat[-1]), "target":float(df.close.iat[-1]+3*atr(df).iat[-1]), "tag":"SQZ+MACD_MTF+ST"}
    if mom.iat[-1] < 0 and mom.iat[-1] < mom.iat[-2] and hist.iat[-1] < 0 and st_dir.iat[-1] == -1:
        return {"side":"SHORT", "stop":float(st_line.iat[-1]), "target":float(df.close.iat[-1]-3*atr(df).iat[-1]), "tag":"SQZ+MACD_MTF+ST"}
    return None


def liquidity_signal(df):
    basis = ema(df.close, 55)
    a55 = atr(df, 55)
    upper, lower = basis+4*a55, basis-4*a55
    a14 = atr(df, 14)
    recent = slice(-11, -1)
    lower_trap = bool((df.close.iloc[recent] < lower.iloc[recent]).any() and df.close.iat[-1] > lower.iat[-1])
    upper_trap = bool((df.close.iloc[recent] > upper.iloc[recent]).any() and df.close.iat[-1] < upper.iat[-1])
    # PO3: a compressed 20-bar range followed by a wick sweep and close-back.
    rh, rl = df.high.iloc[-22:-2].max(), df.low.iloc[-22:-2].min()
    width = rh-rl
    compressed = width/max(float(a55.iat[-2]), 1e-12) <= 8.0
    bull_po3 = compressed and df.low.iat[-1] < rl and df.close.iat[-1] > rl
    bear_po3 = compressed and df.high.iat[-1] > rh and df.close.iat[-1] < rh
    rv = rsi(df.close, 20).iat[-1]
    if (lower_trap or bull_po3) and rv < 55:
        stop = min(df.low.iloc[-2:].min(), rl)-0.5*a14.iat[-1]
        target = max(basis.iat[-1], rh)
        return {"side":"LONG", "stop":float(stop), "target":float(target), "tag":"TRAP/PO3+RSI"}
    if (upper_trap or bear_po3) and rv > 45:
        stop = max(df.high.iloc[-2:].max(), rh)+0.5*a14.iat[-1]
        target = min(basis.iat[-1], rl)
        return {"side":"SHORT", "stop":float(stop), "target":float(target), "tag":"TRAP/PO3+RSI"}
    return None


def amd_po3_signal(df):
    """Confirmed-bar AMD cycle: compression -> sweep -> return -> distribution."""
    if len(df) < 240:
        return None

    min_range_bars, max_range_bars = 12, 96
    range_win, stat_window = 20, 200
    compression_pct, tolerance = 25.0, 0.10
    min_width_pct, return_bars = 0.15, 8
    stop_atr_buffer, fib_extension = 0.40, 1.50
    distribution_timeout, cooldown_bars = 64, 10

    a14 = atr(df, 14)
    hi20 = df.high.rolling(range_win).max()
    lo20 = df.low.rolling(range_win).min()
    widths = hi20-lo20
    width_rank = widths.rolling(stat_window).apply(
        lambda values: 100.0*np.count_nonzero(values <= values[-1])/len(values), raw=True
    )

    # Pivots are consumed only after three right-hand bars have closed.
    pivot_lr = 3
    pivot_high = np.full(len(df), np.nan)
    pivot_low = np.full(len(df), np.nan)
    for j in range(pivot_lr, len(df)-pivot_lr):
        hs = df.high.iloc[j-pivot_lr:j+pivot_lr+1]
        ls = df.low.iloc[j-pivot_lr:j+pivot_lr+1]
        if df.high.iat[j] >= hs.max(): pivot_high[j] = df.high.iat[j]
        if df.low.iat[j] <= ls.min(): pivot_low[j] = df.low.iat[j]

    state, cooldown_until = "IDLE", -1
    range_start = range_high = range_low = range_width = atr_anchor = None
    sweep_side = sweep_bar = sweep_extreme = None
    dist_bar = dist_dir = entry = stop = target = None

    for i in range(stat_window+range_win-2, len(df)):
        close_i, high_i, low_i = float(df.close.iat[i]), float(df.high.iat[i]), float(df.low.iat[i])

        if state == "IDLE":
            if i < cooldown_until or pd.isna(width_rank.iat[i]) or width_rank.iat[i] > compression_pct:
                continue
            candidate_width = float(widths.iat[i])
            if candidate_width < min_width_pct/100.0*close_i:
                continue
            candidate_start = i-range_win+1
            confirmed_end = i-pivot_lr
            ph = pivot_high[candidate_start:confirmed_end+1]
            pl = pivot_low[candidate_start:confirmed_end+1]
            range_high = float(np.nanmax(ph)) if np.isfinite(ph).any() else float(hi20.iat[i])
            range_low = float(np.nanmin(pl)) if np.isfinite(pl).any() else float(lo20.iat[i])
            range_width = range_high-range_low
            if range_width < min_width_pct/100.0*close_i:
                continue
            range_start = candidate_start
            anchor_index = max(0, candidate_start-1)
            atr_anchor = float(a14.iat[anchor_index])
            if not math.isfinite(atr_anchor) or atr_anchor <= 0:
                atr_anchor = float(a14.iat[i])
            state = "ACCUM"
            continue

        if state == "ACCUM":
            age = i-range_start
            if age > max_range_bars:
                state, cooldown_until = "IDLE", i+cooldown_bars
                continue
            tol = tolerance*range_width
            breach_high = high_i > range_high+tol
            breach_low = low_i < range_low-tol
            if age < min_range_bars:
                if breach_high or breach_low:
                    state, cooldown_until = "IDLE", i+cooldown_bars
                continue
            if breach_high and breach_low:
                state, cooldown_until = "IDLE", i+cooldown_bars
                continue
            if breach_high or breach_low:
                sweep_side = 1 if breach_high else -1
                sweep_bar = i
                sweep_extreme = high_i if breach_high else low_i
                state = "SWEEP"
            else:
                continue

        if state == "SWEEP":
            sweep_extreme = max(sweep_extreme, high_i) if sweep_side == 1 else min(sweep_extreme, low_i)
            if i-sweep_bar > return_bars:
                state, cooldown_until = "IDLE", i+cooldown_bars
                continue
            if not (range_low <= close_i <= range_high):
                continue

            dist_dir = -1 if sweep_side == 1 else 1
            entry, dist_bar = close_i, i
            if dist_dir == 1:
                fib_leg = range_high-sweep_extreme
                stop = sweep_extreme-stop_atr_buffer*atr_anchor
                target = range_high+(fib_extension-1.0)*fib_leg
                side = "LONG"
            else:
                fib_leg = sweep_extreme-range_low
                stop = sweep_extreme+stop_atr_buffer*atr_anchor
                target = range_low-(fib_extension-1.0)*fib_leg
                side = "SHORT"
            if fib_leg <= 0 or not (stop < entry < target if side == "LONG" else target < entry < stop):
                state, cooldown_until = "IDLE", i+cooldown_bars
                continue
            if i == len(df)-1:
                return {"side":side, "stop":float(stop), "target":float(target), "tag":"AMD_PO3_SWEEP_RETURN"}
            state = "DIST"
            continue

        if state == "DIST":
            hit_target = high_i >= target if dist_dir == 1 else low_i <= target
            hit_stop = low_i <= stop if dist_dir == 1 else high_i >= stop
            if hit_target or hit_stop or i-dist_bar >= distribution_timeout:
                state, cooldown_until = "IDLE", i+cooldown_bars

    return None


def intraday_signal(df):
    e9, e21, mid = ema(df.close, 9), ema(df.close, 21), sma(df.close, 20)
    vw, a = session_vwap(df), atr(df, 14)
    st_dir, _ = supertrend(df)
    cross_up = e9.iat[-2] <= mid.iat[-2] and e9.iat[-1] > mid.iat[-1]
    cross_dn = e9.iat[-2] >= mid.iat[-2] and e9.iat[-1] < mid.iat[-1]
    if cross_up and df.close.iat[-1] > e21.iat[-1] and df.close.iat[-1] > vw.iat[-1] and st_dir.iat[-1] == 1:
        return {"side":"LONG", "stop":float(df.close.iat[-1]-1.5*a.iat[-1]), "target":float(df.close.iat[-1]+2*a.iat[-1]), "tag":"EMA/BB+VWAP"}
    if cross_dn and df.close.iat[-1] < e21.iat[-1] and df.close.iat[-1] < vw.iat[-1] and st_dir.iat[-1] == -1:
        return {"side":"SHORT", "stop":float(df.close.iat[-1]+1.5*a.iat[-1]), "target":float(df.close.iat[-1]-2*a.iat[-1]), "tag":"EMA/BB+VWAP"}
    # Range-mode ChartArt reversal, only when SuperTrend has flipped repeatedly.
    flips = int((st_dir.iloc[-12:].diff().fillna(0) != 0).sum())
    bb_basis, bb_dev = sma(df.close, 200), 2*df.close.rolling(200).std(ddof=0)
    bb_u, bb_l, rv = bb_basis+bb_dev, bb_basis-bb_dev, rsi(df.close, 6)
    if flips >= 2 and df.close.iat[-2] < bb_l.iat[-2] and df.close.iat[-1] > bb_l.iat[-1] and rv.iat[-2] <= 50 < rv.iat[-1]:
        return {"side":"LONG", "stop":float(df.close.iat[-1]-1.5*a.iat[-1]), "target":float(bb_basis.iat[-1]), "tag":"BB200+RSI6_RANGE"}
    if flips >= 2 and df.close.iat[-2] > bb_u.iat[-2] and df.close.iat[-1] < bb_u.iat[-1] and rv.iat[-2] >= 50 > rv.iat[-1]:
        return {"side":"SHORT", "stop":float(df.close.iat[-1]+1.5*a.iat[-1]), "target":float(bb_basis.iat[-1]), "tag":"BB200+RSI6_RANGE"}
    return None


def cocktail_signal(df, htf):
    """Adaptive, non-conflicting blend of the ten submitted strategies."""
    close = float(df.close.iat[-1])
    a = atr(df, 14)
    st_dir, _ = supertrend(df)
    e9, e21, s200 = ema(df.close, 9), ema(df.close, 21), sma(df.close, 200)
    vw = session_vwap(df)
    on, off, mom = squeeze(df)
    release = bool(on.iat[-2] and off.iat[-1])
    cross_up = e9.iat[-2] <= e21.iat[-2] and e9.iat[-1] > e21.iat[-1]
    cross_dn = e9.iat[-2] >= e21.iat[-2] and e9.iat[-1] < e21.iat[-1]
    macd = ema(htf.close, 12)-ema(htf.close, 26)
    hist = macd-sma(macd, 9)
    flips = int((st_dir.iloc[-12:].diff().fillna(0) != 0).sum())
    trend_strength = abs(e9.iat[-1]-e21.iat[-1])/max(float(a.iat[-1]), 1e-12)
    trending = flips <= 1 and trend_strength >= 0.25

    # Trend/expansion route: Squeeze or EMA trigger, with independent direction,
    # momentum, value and structure confirmations. No reversal input is mixed in.
    if trending and (release or cross_up or cross_dn):
        prev_high = float(df.high.iloc[-21:-1].max())
        prev_low = float(df.low.iloc[-21:-1].min())
        long_checks = [st_dir.iat[-1] == 1, close > s200.iat[-1], hist.iat[-1] > 0,
                       close > vw.iat[-1], mom.iat[-1] > 0 and mom.iat[-1] > mom.iat[-2],
                       close > prev_high]
        short_checks = [st_dir.iat[-1] == -1, close < s200.iat[-1], hist.iat[-1] < 0,
                        close < vw.iat[-1], mom.iat[-1] < 0 and mom.iat[-1] < mom.iat[-2],
                        close < prev_low]
        long_score, short_score = sum(long_checks), sum(short_checks)
        if (release or cross_up) and long_score >= 5 and long_score > short_score:
            return {"side":"LONG", "stop":close*(1-0.50/LEVERAGE), "target":close*(1+2.0/LEVERAGE), "tag":f"COCKTAIL_TREND_{long_score}/6"}
        if (release or cross_dn) and short_score >= 5 and short_score > long_score:
            return {"side":"SHORT", "stop":close*(1+0.50/LEVERAGE), "target":close*(1-2.0/LEVERAGE), "tag":f"COCKTAIL_TREND_{short_score}/6"}

    # Range/liquidity route: only active when the trend route is inactive.
    basis, a55 = ema(df.close, 55), atr(df, 55)
    upper, lower = basis+4*a55, basis-4*a55
    recent = slice(-11, -1)
    lower_trap = bool((df.close.iloc[recent] < lower.iloc[recent]).any() and close > lower.iat[-1])
    upper_trap = bool((df.close.iloc[recent] > upper.iloc[recent]).any() and close < upper.iat[-1])
    rh, rl = float(df.high.iloc[-22:-2].max()), float(df.low.iloc[-22:-2].min())
    compressed = (rh-rl)/max(float(a55.iat[-2]), 1e-12) <= 8.0
    bull_po3 = compressed and df.low.iat[-1] < rl and close > rl
    bear_po3 = compressed and df.high.iat[-1] > rh and close < rh
    bb_basis = sma(df.close, 200)
    bb_dev = 2*df.close.rolling(200).std(ddof=0)
    rv6, rv20 = rsi(df.close, 6), rsi(df.close, 20)
    bull_reclaim = df.close.iat[-1] > df.open.iat[-1] and rv6.iat[-1] > rv6.iat[-2]
    bear_reclaim = df.close.iat[-1] < df.open.iat[-1] and rv6.iat[-1] < rv6.iat[-2]
    if not trending and (lower_trap or bull_po3):
        score = sum([bull_reclaim, rv20.iat[-1] < 55, close < bb_basis.iat[-1],
                     df.low.iat[-1] < (bb_basis-bb_dev).iat[-1], close > rl])
        if score >= 3:
            return {"side":"LONG", "stop":close*(1-0.50/LEVERAGE), "target":close*(1+2.0/LEVERAGE), "tag":f"COCKTAIL_REVERSAL_{score}/5"}
    if not trending and (upper_trap or bear_po3):
        score = sum([bear_reclaim, rv20.iat[-1] > 45, close > bb_basis.iat[-1],
                     df.high.iat[-1] > (bb_basis+bb_dev).iat[-1], close < rh])
        if score >= 3:
            return {"side":"SHORT", "stop":close*(1+0.50/LEVERAGE), "target":close*(1-2.0/LEVERAGE), "tag":f"COCKTAIL_REVERSAL_{score}/5"}
    return None


class PaperBot:
    def __init__(self):
        self.market = Market()
        try: STATE_DIR.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            globals()["STATE_DIR"] = Path("./state"); STATE_DIR.mkdir(exist_ok=True)
        self.path = STATE_DIR/f"{BOT_TYPE.lower()}_state.json"
        self.state = self.load()
        self.last_scan = 0

    def load(self):
        if self.path.exists():
            try: return json.loads(self.path.read_text())
            except Exception: log.exception("STATE LOAD FAILED")
        return {"balance":START_BALANCE, "positions":{}, "closed":[], "last_signal":{}}

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        tmp.replace(self.path)

    def apply_fee_adjusted_targets(self):
        """Apply fee-adjusted 3-loss targets to restored open positions."""
        changed = False
        for p in self.state.get("positions", {}).values():
            if "initial_stop" not in p:
                p["initial_stop"] = p["stop"]
                changed = True
            target = fee_adjusted_three_loss_target(p["entry"], p["initial_stop"], p["side"])
            if not math.isclose(p.get("target", target), target, rel_tol=1e-12, abs_tol=1e-12):
                p["target"] = target
                changed = True
        if changed:
            self.save()

    def apply_staged_roi_management(self):
        """Apply the shared -50/+100/+150/+200 ROI plan to restored positions."""
        changed = False
        for p in self.state.get("positions", {}).values():
            entry, side = p["entry"], p["side"]
            initial_stop = entry*(1-0.50/LEVERAGE) if side == "LONG" else entry*(1+0.50/LEVERAGE)
            tp1 = entry*(1+1.00/LEVERAGE) if side == "LONG" else entry*(1-1.00/LEVERAGE)
            tp2 = entry*(1+1.50/LEVERAGE) if side == "LONG" else entry*(1-1.50/LEVERAGE)
            tp3 = entry*(1+2.00/LEVERAGE) if side == "LONG" else entry*(1-2.00/LEVERAGE)
            p.setdefault("initial_qty", p["qty"])
            p.setdefault("tp1_done", False)
            p.setdefault("tp2_done", False)
            p.setdefault("protected", p["tp1_done"])
            p.setdefault("realized_pnl", 0.0)
            p.update({"initial_stop":initial_stop, "tp1":tp1, "tp2":tp2, "tp3":tp3, "target":tp3})
            p["stop"] = entry if p["protected"] or p["tp1_done"] else initial_stop
            changed = True
        if changed:
            self.save()

    def notify(self, message):
        text = f"[{BOT_NAME}] {message}"
        log.info(text)
        if not TG_TOKEN or not TG_CHAT:
            log.warning("TELEGRAM DISABLED: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
            return
        for attempt in range(1, 4):
            try:
                response = requests.post(
                    f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                    data={"chat_id":TG_CHAT, "text":text, "disable_web_page_preview":True},
                    timeout=15,
                )
                response.raise_for_status()
                payload = response.json()
                if not payload.get("ok"):
                    raise RuntimeError(payload.get("description", "Telegram rejected the message"))
                return
            except Exception as exc:
                if attempt == 3:
                    log.error("TELEGRAM FAILED after 3 attempts: %s", exc)
                else:
                    log.warning("TELEGRAM attempt %s failed: %s", attempt, exc)
                    time.sleep(2)

    def open(self, symbol, price, signal):
        if symbol in self.state["positions"]: return
        if len(self.state["positions"]) >= MAX_POSITIONS: return
        side, stop, target = signal["side"], signal["stop"], signal["target"]
        if side == "LONG" and not stop < price < target: return
        if side == "SHORT" and not target < price < stop: return
        qty = NOTIONAL/price
        self.state["positions"][symbol] = {
            "side":side, "entry":price, "qty":qty, "notional":NOTIONAL,
            "stop":stop, "target":target, "tag":signal["tag"],
            "opened":datetime.now(timezone.utc).isoformat()
        }
        self.save()
        self.notify(f"OPEN {symbol} {side} | entry {price:.8g} | SL {stop:.8g} | TP {target:.8g} | {signal['tag']}")

    def open_cocktail(self, symbol, price, signal):
        side = signal["side"]
        stop = price*(1-0.50/LEVERAGE) if side == "LONG" else price*(1+0.50/LEVERAGE)
        tp1 = price*(1+1.00/LEVERAGE) if side == "LONG" else price*(1-1.00/LEVERAGE)
        tp2 = price*(1+1.50/LEVERAGE) if side == "LONG" else price*(1-1.50/LEVERAGE)
        tp3 = price*(1+2.00/LEVERAGE) if side == "LONG" else price*(1-2.00/LEVERAGE)
        qty = NOTIONAL/price
        self.state["positions"][symbol] = {
            "side":side, "entry":price, "qty":qty, "initial_qty":qty, "notional":NOTIONAL,
            "stop":stop, "initial_stop":stop, "target":tp3, "tp1":tp1, "tp2":tp2, "tp3":tp3,
            "tp1_done":False, "tp2_done":False, "protected":False, "realized_pnl":0.0,
            "tag":signal["tag"], "opened":datetime.now(timezone.utc).isoformat()
        }
        self.save()
        self.notify(f"OPEN {symbol} {side} | entry {price:.8g} | SL50 {stop:.8g} | TP 100/150/200 ROI | {signal['tag']}")

    def cocktail_partial_close(self, symbol, price, qty_to_close, reason):
        p = self.state["positions"][symbol]
        qty_to_close = min(qty_to_close, p["qty"])
        gross = (price-p["entry"])*qty_to_close*(1 if p["side"] == "LONG" else -1)
        fees = (p["entry"]+price)*qty_to_close*FEE_RATE
        pnl = gross-fees
        self.state["balance"] += pnl
        p["qty"] -= qty_to_close
        p["realized_pnl"] = p.get("realized_pnl", 0.0)+pnl
        self.notify(f"{reason} {symbol} | closed {qty_to_close/p['initial_qty']*100:.0f}% original | PNL {pnl:+.2f}$ | balance {self.state['balance']:.2f}$")
        if p["qty"] <= p["initial_qty"]*1e-9:
            closed = self.state["positions"].pop(symbol)
            closed.update({"exit":price, "pnl":closed["realized_pnl"], "reason":reason, "closed":datetime.now(timezone.utc).isoformat()})
            self.state["closed"] = (self.state["closed"]+[closed])[-1000:]
        self.save()

    def close(self, symbol, price, reason):
        p = self.state["positions"].pop(symbol)
        gross = (price-p["entry"])*p["qty"]*(1 if p["side"] == "LONG" else -1)
        fees = (p["entry"]*p["qty"]+price*p["qty"])*FEE_RATE
        pnl = gross-fees
        self.state["balance"] += pnl
        p.update({"exit":price,"pnl":pnl,"reason":reason,"closed":datetime.now(timezone.utc).isoformat()})
        self.state["closed"] = (self.state["closed"]+[p])[-1000:]
        self.save()
        self.notify(f"CLOSE {symbol} {reason} | PNL {pnl:+.2f}$ | balance {self.state['balance']:.2f}$")

    def manage(self):
        if not self.state["positions"]: return
        prices = self.market.prices()
        for symbol, p in list(self.state["positions"].items()):
            price = prices.get(symbol)
            if not price: continue
            if p["side"] == "LONG":
                if price <= p["stop"]: self.close(symbol, p["stop"], "STOP")
                elif price >= p["target"]: self.close(symbol, p["target"], "TARGET")
            else:
                if price >= p["stop"]: self.close(symbol, p["stop"], "STOP")
                elif price <= p["target"]: self.close(symbol, p["target"], "TARGET")

    def manage_cocktail(self, prices):
        for symbol, p in list(self.state["positions"].items()):
            price = prices.get(symbol)
            if not price: continue
            if p["side"] == "LONG" and price <= p["stop"]:
                self.cocktail_partial_close(symbol, p["stop"], p["qty"], "STOP")
                continue
            if p["side"] == "SHORT" and price >= p["stop"]:
                self.cocktail_partial_close(symbol, p["stop"], p["qty"], "STOP")
                continue
            hit1 = price >= p["tp1"]*(1-1e-12) if p["side"] == "LONG" else price <= p["tp1"]*(1+1e-12)
            hit2 = price >= p["tp2"]*(1-1e-12) if p["side"] == "LONG" else price <= p["tp2"]*(1+1e-12)
            hit3 = price >= p["tp3"]*(1-1e-12) if p["side"] == "LONG" else price <= p["tp3"]*(1+1e-12)
            if hit1 and not p["tp1_done"]:
                self.cocktail_partial_close(symbol, p["tp1"], p["initial_qty"]*0.50, "TP1_100ROI")
                if symbol not in self.state["positions"]: continue
                p = self.state["positions"][symbol]
                p["tp1_done"], p["protected"], p["stop"] = True, True, p["entry"]
                self.save()
                self.notify(f"BREAKEVEN {symbol} {p['side']} | stop moved to entry {p['entry']:.8g} | slot released")
            if symbol not in self.state["positions"]: continue
            p = self.state["positions"][symbol]
            if hit2 and not p["tp2_done"]:
                self.cocktail_partial_close(symbol, p["tp2"], p["initial_qty"]*0.25, "TP2_150ROI")
                if symbol not in self.state["positions"]: continue
                self.state["positions"][symbol]["tp2_done"] = True
                self.save()
            if symbol in self.state["positions"] and hit3:
                p = self.state["positions"][symbol]
                self.cocktail_partial_close(symbol, p["tp3"], p["qty"], "TP3_200ROI")

    def scan(self):
        if len(self.state["positions"]) >= MAX_POSITIONS:
            log.info("[%s] FULL positions=%s", BOT_NAME, len(self.state["positions"])); return
        for symbol in self.market.symbols():
            if symbol in self.state["positions"]: continue
            try:
                df = self.market.candles(symbol, limit=500)
                if len(df) < 240: continue
                signal = amd_po3_signal(df)
                if signal:
                    candle_id = str(int(df.open_time.iat[-1]))
                    key = f"{symbol}:{signal['tag']}:{signal['side']}"
                    if self.state["last_signal"].get(key) == candle_id: continue
                    self.state["last_signal"][key] = candle_id
                    self.open(symbol, float(df.close.iat[-1]), signal)
            except Exception as exc:
                log.warning("%s scan failed: %s", symbol, exc)

    def run(self):
        self.notify(f"START PAPER | balance {self.state['balance']:.2f}$ | leverage {LEVERAGE}x | notional {NOTIONAL}$ | max {MAX_POSITIONS}")
        while True:
            try:
                self.manage()
                if time.time()-self.last_scan >= SCAN_SECONDS:
                    self.scan(); self.last_scan = time.time()
            except KeyboardInterrupt: break
            except Exception: log.exception("MAIN LOOP ERROR")
            time.sleep(CHECK_SECONDS)


if __name__ == "__main__":
    if not DRY_RUN:
        raise SystemExit("This build is PAPER MODE ONLY. Set DRY_RUN=true.")
    PaperBot().run()
