import hashlib, hmac, logging, math, os, time
from urllib.parse import urlencode
import numpy as np
import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log=logging.getLogger("amd-demo")

def env(n,d,c=str):
    v=os.getenv(n,d)
    return (str(v).lower() in {"1","true","yes","on"}) if c is bool else c(v)

KEY=os.getenv("BINANCE_DEMO_API_KEY","").strip().strip("\'\\\"")
SECRET=os.getenv("BINANCE_DEMO_API_SECRET","").strip().strip("\'\\\"")
BASE=env("EXCHANGE_BASE_URL","https://demo-fapi.binance.com").strip().rstrip("/")
LEV=env("LEVERAGE",20,int)
NOTIONAL=env("POSITION_NOTIONAL",300.0,float)
MAX_POS=env("MAX_POSITIONS",20,int)
BASKET=env("BASKET_PROFIT_TARGET_USD",10.0,float)
DAILY_STOP=env("DAILY_STOP_USD",50.0,float)
TF=env("TIMEFRAME","5m")
SCAN=env("SCAN_SECONDS",300,int)
CHECK=env("CHECK_SECONDS",5,int)
TOP_N=env("TOP_N",40,int)
MIN_VOL=env("MIN_QUOTE_VOLUME",20_000_000,float)
TG_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","").strip()
TG_CHAT=os.getenv("TELEGRAM_CHAT_ID","").strip()

def telegram(msg):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        r=requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id":TG_CHAT,"text":msg},
            timeout=10
        )
        if not r.ok:
            log.error("Telegram %s: %s",r.status_code,r.text[:300])
    except Exception as e:
        log.error("Telegram failed: %s",e)

class Binance:
    def __init__(self):
        if not KEY or not SECRET: raise SystemExit("Missing Binance Demo API keys")
        self.s=requests.Session(); self.s.headers.update({"X-MBX-APIKEY":KEY,"User-Agent":"AMDPO3Demo/1.1"})
        self.time_offset=0
        self.sync_time()
        self.info=self.get("/fapi/v1/exchangeInfo")
        self.meta={x["symbol"]:x for x in self.info["symbols"]}
    def get(self,p,params=None):
        r=self.s.get(BASE+p,params=params,timeout=20); r.raise_for_status(); return r.json()
    def signed(self,m,p,params=None):
        q=dict(params or {}); q["timestamp"]=int(time.time()*1000)+self.time_offset; q.setdefault("recvWindow",10000)
        raw=urlencode(q,doseq=True); sig=hmac.new(SECRET.encode(),raw.encode(),hashlib.sha256).hexdigest()
        r=self.s.request(m,BASE+p+"?"+raw+"&signature="+sig,timeout=25)
        if not r.ok: raise RuntimeError(f"Binance {r.status_code}: {r.text[:500]}")
        return r.json()
    def sync_time(self):
        server=self.get("/fapi/v1/time")["serverTime"]
        self.time_offset=int(server)-int(time.time()*1000)
    def positions(self):
        return [x for x in self.signed("GET","/fapi/v2/positionRisk") if abs(float(x["positionAmt"]))>0]
    def wallet(self):
        return float(self.signed("GET","/fapi/v2/account")["totalWalletBalance"])
    def symbols(self):
        ok={s for s,x in self.meta.items() if x.get("status")=="TRADING" and x.get("quoteAsset")=="USDT" and x.get("contractType")=="PERPETUAL"}
        rows=self.get("/fapi/v1/ticker/24hr")
        rows=sorted((x for x in rows if x["symbol"] in ok and float(x.get("quoteVolume",0))>=MIN_VOL),key=lambda x:float(x["quoteVolume"]),reverse=True)
        return [x["symbol"] for x in rows[:TOP_N]]
    def candles(self,s):
        rows=self.get("/fapi/v1/klines",{"symbol":s,"interval":TF,"limit":500})
        cols=["open_time","open","high","low","close","volume","close_time","qv","n","tb","tq","ignore"]
        d=pd.DataFrame(rows,columns=cols)
        for c in ["open","high","low","close","volume"]: d[c]=pd.to_numeric(d[c],errors="coerce")
        return d.iloc[:-1].reset_index(drop=True)
    def leverage(self,s):
        for x in range(LEV,0,-1):
            try: self.signed("POST","/fapi/v1/leverage",{"symbol":s,"leverage":x}); return x
            except Exception:
                if x==1: raise
    def qty(self,s,q):
        f={x["filterType"]:x for x in self.meta[s]["filters"]}
        lot=f.get("MARKET_LOT_SIZE") or f["LOT_SIZE"]; step=float(lot["stepSize"]); mn=float(lot["minQty"])
        q=max(mn,math.floor(q/step+1e-12)*step)
        dec=max(0,len(f"{step:.12f}".rstrip("0").split(".")[1]) if "." in f"{step:.12f}".rstrip("0") else 0)
        return f"{q:.{dec}f}"
    def order(self,s,side,q,reduce=False):
        p={"symbol":s,"side":side,"type":"MARKET","quantity":q}
        if reduce:p["reduceOnly"]="true"
        return self.signed("POST","/fapi/v1/order",p)

    def price(self,s,p):
        fs={x["filterType"]:x for x in self.meta[s]["filters"]}
        tick=float(fs["PRICE_FILTER"]["tickSize"])
        v=round(float(p)/tick)*tick
        t=f"{tick:.12f}".rstrip("0")
        dec=len(t.split(".")[1]) if "." in t else 0
        return f"{v:.{dec}f}"

    def algo_order(self,p):
        return self.signed("POST","/fapi/v1/algoOrder",p)

    def protect(self,s,pos_side,stop,target):
        close_side="SELL" if pos_side=="LONG" else "BUY"
        common={"algoType":"CONDITIONAL","symbol":s,"side":close_side,
                "closePosition":"true","workingType":"MARK_PRICE"}
        sl=dict(common); sl.update({"type":"STOP_MARKET","triggerPrice":self.price(s,stop)})
        tp=dict(common); tp.update({"type":"TAKE_PROFIT_MARKET","triggerPrice":self.price(s,target)})
        slr=self.algo_order(sl)
        try:
            tpr=self.algo_order(tp)
        except Exception:
            try:self.cancel_orders(s)
            except Exception:pass
            raise
        return slr,tpr

    def cancel_orders(self,s):
        # Standard orders + migrated USD-M conditional/algo orders.
        for path in ("/fapi/v1/allOpenOrders","/fapi/v1/algoOpenOrders"):
            try:self.signed("DELETE",path,{"symbol":s})
            except Exception as e:log.warning("cancel %s %s: %s",s,path,e)

    def close_all(self,attempts=5):
        for attempt in range(1,attempts+1):
            ps=self.positions()
            if not ps:return True
            for p in ps:
                s=p["symbol"]; a=float(p["positionAmt"])
                self.cancel_orders(s)
                try:self.order(s,"SELL" if a>0 else "BUY",self.qty(s,abs(a)),True)
                except Exception as e:log.error("close %s attempt %d: %s",s,attempt,e)
            time.sleep(1)
        return not self.positions()

def atr(df, n=14):
    pc = df.close.shift(1)
    tr = pd.concat([(df.high-df.low), (df.high-pc).abs(), (df.low-pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()


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



class Bot:
    def __init__(self):
        self.b=Binance(); self.last_scan=0; self.seen={}; self.day=None; self.day_wallet=None
    def daily_ok(self):
        d=time.strftime("%Y-%m-%d",time.gmtime())
        if d!=self.day: self.day=d; self.day_wallet=self.b.wallet()
        return self.b.wallet()-self.day_wallet > -DAILY_STOP
    def risk(self):
        ps=self.b.positions()
        pnl=sum(float(p.get("unRealizedProfit",0)) for p in ps)
        log.info("BASKET open_pnl=%+.2f target=%.2f positions=%d",pnl,BASKET,len(ps))
        if ps and pnl>=BASKET:
            log.warning("BASKET TARGET HIT %+.2f -> CLOSE ALL 100%%",pnl)
            telegram(f"AMD DEMO\nBASKET HIT ${pnl:+.2f}\nClosing all positions 100%")
            ok=self.b.close_all()
            rem=self.b.positions()
            if rem:
                log.error("CLOSE ALL incomplete: %d remain",len(rem))
                telegram(f"AMD DEMO ERROR\nClose-all incomplete: {len(rem)} remain")
            else:
                log.warning("CYCLE COMPLETE -> immediate new cycle")
                telegram("AMD DEMO\nCYCLE COMPLETE\nAll positions closed. New cycle starts now.")
                self.last_scan=0
            return False
        if not self.daily_ok():
            if ps:self.b.close_all()
            log.warning("DAILY STOP ACTIVE")
            return False
        return True
    def scan(self):
        ps=self.b.positions(); held={p["symbol"] for p in ps}
        if len(held)>=MAX_POS:return
        for s in self.b.symbols():
            if s in held:continue
            try:
                d=self.b.candles(s); sig=amd_po3_signal(d)
                if not sig:continue
                cid=str(int(d.open_time.iat[-1])); k=f"{s}:{sig['side']}:{sig['tag']}"
                if self.seen.get(k)==cid:continue
                self.seen[k]=cid; price=float(d.close.iat[-1]); lev=self.b.leverage(s)
                q=self.b.qty(s,NOTIONAL/price)
                self.b.order(s,"BUY" if sig["side"]=="LONG" else "SELL",q)
                try:
                    self.b.protect(s,sig["side"],sig["stop"],sig["target"])
                except Exception as e:
                    log.exception("PROTECTION FAILED %s -> emergency close",s)
                    telegram(f"AMD DEMO ERROR\n{s} opened but SL/TP failed. Emergency closing.\n{e}")
                    time.sleep(0.3)
                    for p in self.b.positions():
                        if p["symbol"]==s:
                            a=float(p["positionAmt"])
                            self.b.order(s,"SELL" if a>0 else "BUY",self.b.qty(s,abs(a)),True)
                    self.b.cancel_orders(s)
                    continue
                slp=self.b.price(s,sig["stop"]); tpp=self.b.price(s,sig["target"])
                log.warning("OPEN %s %s notional~%.2f leverage=%dx SL=%s TP=%s",s,sig["side"],NOTIONAL,lev,slp,tpp)
                telegram(f"AMD DEMO OPEN\n{s} {sig['side']}\nNotional: ${NOTIONAL:.2f}\nLeverage: {lev}x\nEntry approx: {price}\nSL: {slp}\nTP: {tpp}")
                held.add(s)
                if len(held)>=MAX_POS:break
            except Exception as e:log.exception("%s failed: %s",s,e)
    def run(self):
        log.warning("START AMD PO3 BINANCE DEMO | basket +$%.2f | max %d | $%.2f each",BASKET,MAX_POS,NOTIONAL)
        telegram(f"AMD PO3 DEMO STARTED\nMax positions: {MAX_POS}\nNotional: ${NOTIONAL:.2f}\nLeverage target: {LEV}x\nBasket: +${BASKET:.2f}\nDaily stop: -${DAILY_STOP:.2f}")
        while True:
            try:
                if self.risk() and time.time()-self.last_scan>=SCAN:
                    self.scan(); self.last_scan=time.time()
            except Exception:log.exception("MAIN LOOP")
            time.sleep(CHECK)

if __name__=="__main__": Bot().run()
