import os,time,hmac,hashlib,json,logging
from decimal import Decimal,ROUND_DOWN
from urllib.parse import urlencode
import requests
import pandas as pd
import numpy as np

KEY=os.getenv("BINANCE_DEMO_API_KEY",""); SECRET=os.getenv("BINANCE_DEMO_API_SECRET","")
BASE=os.getenv("EXCHANGE_BASE_URL","https://demo-fapi.binance.com").rstrip("/")
TG=os.getenv("TELEGRAM_BOT_TOKEN",""); CHAT=os.getenv("TELEGRAM_CHAT_ID","")
BOT_VERSION="V1.6-2-PER-CANDLE-BASKET50"
TF="15m"; NOTIONAL=300.0; TARGET_LEV=20; MAX_POS=20
MIN_VOL=float(os.getenv("MIN_QUOTE_VOLUME","5000000"))
EXCLUDED={"BNBUSDT","DOGEUSDT","BCHUSDT"}
BASKET=50.0; LOSS_LIMIT=100.0
ALLOCATED_CAPITAL=float(os.getenv("ALLOCATED_CAPITAL","500"))
TAKER_FEE_RATE=float(os.getenv("TAKER_FEE_RATE","0.0005"))
S=requests.Session(); S.headers.update({"X-MBX-APIKEY":KEY})
logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
meta={}; mine={}; btc_mode="WAIT"; pause_until=0; loss_window=0; losing_cycles=0; cycle_realized=0; bot_realized=0; basket_lock_candle=0; last_entry_candle=0
STATE="state.json"

def pub(path,p=None):
    r=S.get(BASE+path,params=p or {},timeout=15); r.raise_for_status(); return r.json()
def signed(method,path,p=None):
    q=dict(p or {}); q["timestamp"]=int(time.time()*1000); q["recvWindow"]=10000
    qs=urlencode(q); sig=hmac.new(SECRET.encode(),qs.encode(),hashlib.sha256).hexdigest()
    r=S.request(method,BASE+path+"?"+qs+"&signature="+sig,timeout=15)
    if not r.ok: raise RuntimeError(f"{method} {path}: {r.text}")
    return r.json()
def balance():
    try:
        for x in signed("GET","/fapi/v2/balance"):
            if x["asset"]=="USDT": return float(x["balance"])
    except: pass
    return 0
def bot_balance():
    return ALLOCATED_CAPITAL + bot_realized

def msg(t,bal=True):
    if bal:t+=f"\nBot Balance: ${bot_balance():.2f}"
    logging.info(t.replace("\n"," | "))
    if TG and CHAT:
        try: requests.post(f"https://api.telegram.org/bot{TG}/sendMessage",data={"chat_id":CHAT,"text":t},timeout=8)
        except: pass
def floor(x,step):
    return float((Decimal(str(x))/Decimal(str(step))).to_integral_value(rounding=ROUND_DOWN)*Decimal(str(step)))
def fmt(x): return f"{x:.12f}".rstrip("0").rstrip(".")
def qty_ok(s,x):
    q=floor(x,meta[s]["step"])
    prec=meta[s].get("qtyPrecision",8)
    q=float(f"{q:.{prec}f}")
    return q
def save():
    with open(STATE,"w") as f: json.dump({"mine":mine,"pause":pause_until,"loss":loss_window,"losing":losing_cycles,"cycle":cycle_realized,"bot_realized":bot_realized,"basket_lock_candle":basket_lock_candle,"btc_mode":btc_mode,"last_entry_candle":last_entry_candle},f)
def load():
    global mine,pause_until,loss_window,losing_cycles,cycle_realized,bot_realized,basket_lock_candle,btc_mode,last_entry_candle
    try:
        d=json.load(open(STATE)); mine=d.get("mine",{}); pause_until=d.get("pause",0); loss_window=d.get("loss",0); losing_cycles=d.get("losing",0); cycle_realized=d.get("cycle",0); bot_realized=d.get("bot_realized",0); basket_lock_candle=d.get("basket_lock_candle",0); btc_mode=d.get("btc_mode","WAIT"); last_entry_candle=d.get("last_entry_candle",0)
    except: pass

def exchange_info():
    global meta
    for s in pub("/fapi/v1/exchangeInfo")["symbols"]:
        if s.get("quoteAsset")!="USDT" or s.get("contractType")!="PERPETUAL" or s.get("status")!="TRADING": continue
        fs={x["filterType"]:x for x in s["filters"]}; lot=fs.get("MARKET_LOT_SIZE",fs.get("LOT_SIZE",{})); pf=fs.get("PRICE_FILTER",{})
        meta[s["symbol"]]={"step":float(lot.get("stepSize",".001")),"min":float(lot.get("minQty","0")),"tick":float(pf.get("tickSize",".0001")),"qtyPrecision":int(s.get("quantityPrecision",8))}
def positions():
    return {p["symbol"]:p for p in signed("GET","/fapi/v2/positionRisk") if abs(float(p["positionAmt"]))>0}
def pos(s):
    for p in signed("GET","/fapi/v2/positionRisk",{"symbol":s}):
        if abs(float(p["positionAmt"]))>0:return p
def market(s,side,qty,reduce=False):
    qty=qty_ok(s,qty)
    if qty<=0: raise RuntimeError(f"{s}: quantity rounded to zero")
    p={"symbol":s,"side":side,"type":"MARKET","quantity":fmt(qty),"newOrderRespType":"RESULT"}
    if reduce:p["reduceOnly"]="true"
    return signed("POST","/fapi/v1/order",p)
def cancel_algo(s):
    try:signed("DELETE","/fapi/v1/algoOpenOrders",{"symbol":s})
    except:pass
def algo_close(s,direction,order_type,px,qty=None,close_position=False):
    side="SELL" if direction=="LONG" else "BUY"; px=floor(px,meta[s]["tick"])
    q={"algoType":"CONDITIONAL","symbol":s,"side":side,"type":order_type,
       "triggerPrice":fmt(px),"workingType":"MARK_PRICE","reduceOnly":"true"}
    if close_position:
        q.pop("reduceOnly",None); q["closePosition"]="true"
    elif qty is not None:
        qty=qty_ok(s,qty)
        if qty<=0: raise RuntimeError(f"{s}: algo quantity rounded to zero")
        q["quantity"]=fmt(qty)
    return signed("POST","/fapi/v1/algoOrder",q)

def stop(s,direction,px):
    return algo_close(s,direction,"STOP_MARKET",px,close_position=True)

def place_targets(s,direction,entry_px,lev,full_qty):
    q1=qty_ok(s,full_qty*0.50)
    q2=qty_ok(s,full_qty*0.25)
    q3=qty_ok(s,max(0.0,full_qty-q1-q2))
    tp1_move=1.00/lev
    tp2_move=1.50/lev
    tp3_move=2.00/lev
    tp1=entry_px*(1+tp1_move) if direction=="LONG" else entry_px*(1-tp1_move)
    tp2=entry_px*(1+tp2_move) if direction=="LONG" else entry_px*(1-tp2_move)
    tp3=entry_px*(1+tp3_move) if direction=="LONG" else entry_px*(1-tp3_move)
    if q1>0: algo_close(s,direction,"TAKE_PROFIT_MARKET",tp1,qty=q1)
    if q2>0: algo_close(s,direction,"TAKE_PROFIT_MARKET",tp2,qty=q2)
    if q3>0: algo_close(s,direction,"TAKE_PROFIT_MARKET",tp3,qty=q3)
    return tp1,tp2,tp3
def leverage(s):
    for l in range(TARGET_LEV,0,-1):
        try:
            return int(signed("POST","/fapi/v1/leverage",{"symbol":s,"leverage":l})["leverage"])
        except Exception:
            continue
    raise RuntimeError("No leverage available")
def klines(s):
    k=pub("/fapi/v1/klines",{"symbol":s,"interval":TF,"limit":110})
    if k and int(k[-1][6])>=int(time.time()*1000):k=k[:-1]
    return k
def sma(values, period):
    if not values or len(values) < period:
        return 0.0
    return sum(values[-period:]) / period

def sig(s):
    """BTC 15m master direction from the last CLOSED candle."""
    k=klines(s)
    if not k or len(k)<99:return "WAIT"
    closes=[float(x[4]) for x in k]
    last=closes[-1]
    m25=sum(closes[-25:])/25
    m99=sum(closes[-99:])/99
    if last>m99 and last>m25:return "LONG"
    if last<m99 and last<m25:return "SHORT"
    return "WAIT"

def universe():
    a=[]
    for t in pub("/fapi/v1/ticker/24hr"):
        s=t["symbol"]
        if s in meta and s!="BTCUSDT" and s not in EXCLUDED and float(t.get("quoteVolume",0))>=MIN_VOL:a.append((s,float(t["quoteVolume"])))
    return [s for s,_ in sorted(a,key=lambda x:x[1],reverse=True)]
def closed_candle_id(s="BTCUSDT"):
    k=klines(s)
    return int(k[-1][0]) if k else 0

def estimated_exit_fees(ps):
    # Conservative market/taker estimate for closing every remaining bot position.
    fees=0.0
    for s,p in ps.items():
        if s not in mine: continue
        qty=abs(float(p["positionAmt"]))
        mark=float(p.get("markPrice") or p["entryPrice"])
        fees += qty*mark*TAKER_FEE_RATE
    return fees

def roi(p):
    amt=abs(float(p["positionAmt"])); ep=float(p["entryPrice"]); lev=float(p.get("leverage",20)); pnl=float(p["unRealizedProfit"])
    margin=amt*ep/max(lev,1); return 100*pnl/margin if margin else 0
def close(s,p,pct,reason):
    global loss_window,cycle_realized,bot_realized
    amt=abs(float(p["positionAmt"])); qty=qty_ok(s,amt*pct/100)
    if qty<=0:return
    est=float(p["unRealizedProfit"])*(qty/amt)
    mark=float(p.get("markPrice") or p["entryPrice"])
    exit_fee=qty*mark*TAKER_FEE_RATE
    net_est=est-exit_fee
    market(s,"SELL" if float(p["positionAmt"])>0 else "BUY",qty,True)
    cycle_realized+=net_est; loss_window+=net_est; bot_realized+=net_est
    msg(f"{s} {reason}\nNet PnL approx: ${net_est:.2f}")
def enter(s,d):
    if s in mine:return
    ps=positions()
    if len(ps)>=MAX_POS or s in ps:return
    px=float(pub("/fapi/v1/ticker/price",{"symbol":s})["price"]); lev=leverage(s)
    qty=qty_ok(s,NOTIONAL/px)
    if qty<meta[s]["min"] or qty<=0:return
    market(s,"BUY" if d=="LONG" else "SELL",qty); time.sleep(.25); p=pos(s)
    if not p:return
    ep=float(p["entryPrice"]); adverse=.50/lev
    sp=ep*(1-adverse) if d=="LONG" else ep*(1+adverse)
    cancel_algo(s)
    stop(s,d,sp)
    tp1_px,tp2_px,tp3_px=place_targets(s,d,ep,lev,abs(float(p["positionAmt"])))
    mine[s]={"dir":d,"tp1":False,"tp2":False,"tp1_px":tp1_px,"tp2_px":tp2_px,"tp3_px":tp3_px,"initial_qty":abs(float(p["positionAmt"]))}; save()
    msg(f"OPEN {d} {s}\nNotional: $300 | Leverage: {lev}x\nEntry: {ep}\nSL: -50% ROI | TP1: +100% (50%) | TP2: +150% (25%) | TP3: +200% (25%)")
def close_all(reason):
    global cycle_realized,losing_cycles,pause_until,basket_lock_candle
    ps=positions(); targets=[(s,p) for s,p in ps.items() if s in mine]
    cycle_total=cycle_realized+sum(float(p["unRealizedProfit"]) for _,p in targets)

    # Cancel protection first, then retry market close up to 3 times.
    for s,_ in targets:
        cancel_algo(s)

    failed=[]
    for s,_ in targets:
        ok=False
        for attempt in range(1,4):
            try:
                live=pos(s)
                if not live:
                    ok=True; break
                close(s,live,100,reason)
                time.sleep(.35)
                if not pos(s):
                    ok=True; break
            except Exception as e:
                logging.error("%s close attempt %d/3: %s",s,attempt,e)
                time.sleep(1.0)
        if not ok:
            failed.append(s)

    # Re-check exchange state. Never pretend basket is finished while a bot position remains.
    live_ps=positions()
    still_open=[s for s in mine if s in live_ps]
    if still_open:
        msg("BASKET CLOSE INCOMPLETE | still open: "+", ".join(still_open))
        # Restore a protective SL for any remaining position where possible.
        for s in still_open:
            try:
                lp=live_ps[s]
                d="LONG" if float(lp["positionAmt"])>0 else "SHORT"
                ep=float(lp["entryPrice"]); lev=float(lp.get("leverage",20))
                adverse=.50/max(lev,1)
                sp=ep*(1-adverse) if d=="LONG" else ep*(1+adverse)
                stop(s,d,sp)
            except Exception as e:
                logging.error("%s restore stop: %s",s,e)
        save()
        return False

    if cycle_total<0: losing_cycles+=1
    else: losing_cycles=0
    if losing_cycles>=3:
        pause_until=max(pause_until,time.time()+3600); losing_cycles=0; msg("3 losing cycles -> PAUSE 1 HOUR")

    mine.clear(); cycle_realized=0
    basket_lock_candle=closed_candle_id("BTCUSDT")
    save()
    return True

def manage():
    global pause_until,loss_window
    ps=positions()
    for s in list(mine):
        if s not in ps:
            mine.pop(s,None); msg(f"{s} CLOSED ON EXCHANGE"); save()
    gross_total=cycle_realized+sum(float(p["unRealizedProfit"]) for s,p in ps.items() if s in mine)
    expected_close_fees=estimated_exit_fees(ps)
    net_after_close=gross_total-expected_close_fees
    if mine and net_after_close>=BASKET:
        msg(f"BASKET NET TARGET ${net_after_close:.2f} AFTER EST. CLOSE FEES -> CLOSE ALL")
        close_all("BASKET NET +$20")
        return
    for s in list(mine):
        p=ps.get(s)
        if not p:continue
        d="LONG" if float(p["positionAmt"])>0 else "SHORT"
        # Trade management belongs to Liquidity Reversal Staged.
        # BTC controls basket direction globally; individual coin MA changes do NOT close the trade.
        current_qty=abs(float(p["positionAmt"]))
        initial_qty=float(mine[s].get("initial_qty",current_qty))
        if not mine[s].get("tp1",False) and initial_qty>0 and current_qty <= initial_qty*0.55:
            # TP1 (+100% ROI) filled: protect remaining 50% at breakeven,
            # then restore TP2 (+150%, 25% original) and TP3 (+200%, final 25%).
            cancel_algo(s)
            ep=float(p["entryPrice"])
            stop(s,d,ep)
            lev=float(p.get("leverage",20))
            q2=qty_ok(s,initial_qty*0.25)
            q3=qty_ok(s,max(0.0,current_qty-q2))
            tp2=ep*(1+1.50/max(lev,1)) if d=="LONG" else ep*(1-1.50/max(lev,1))
            tp3=ep*(1+2.00/max(lev,1)) if d=="LONG" else ep*(1-2.00/max(lev,1))
            if q2>0: algo_close(s,d,"TAKE_PROFIT_MARKET",tp2,qty=q2)
            if q3>0: algo_close(s,d,"TAKE_PROFIT_MARKET",tp3,qty=q3)
            mine[s]["tp1"]=True; save()
            msg(f"{s} TP1 +100% ROI EXECUTED (50%)\nSL -> BREAKEVEN\nTP2 +150% (25%) | TP3 +200% (25%)")
        elif mine[s].get("tp1",False) and not mine[s].get("tp2",False) and initial_qty>0 and current_qty <= initial_qty*0.30:
            mine[s]["tp2"]=True; save()
            msg(f"{s} TP2 +150% ROI EXECUTED (25%)\nFinal 25% targeting +200% ROI")
    if loss_window<=-LOSS_LIMIT and time.time()>=pause_until:
        pause_until=time.time()+10800; loss_window=0; save(); msg("Loss window reached -$100 -> PAUSE 3 HOURS")



# --- Exact Liquidity Reversal Staged signal reused from prior bot ---
def liq_ema(s, n): return s.ewm(span=n, adjust=False).mean()

def liq_sma(s, n): return s.rolling(n).mean()

def liq_atr(df, n=14):
    pc = df.close.shift(1)
    tr = pd.concat([(df.high-df.low), (df.high-pc).abs(), (df.low-pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()

def liq_rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))

def liq_liquidity_signal(df):
    basis = liq_ema(df.close, 55)
    a55 = liq_atr(df, 55)
    upper, lower = basis+4*a55, basis-4*a55
    a14 = liq_atr(df, 14)
    recent = slice(-11, -1)
    lower_trap = bool((df.close.iloc[recent] < lower.iloc[recent]).any() and df.close.iat[-1] > lower.iat[-1])
    upper_trap = bool((df.close.iloc[recent] > upper.iloc[recent]).any() and df.close.iat[-1] < upper.iat[-1])
    # PO3: a compressed 20-bar range followed by a wick sweep and close-back.
    rh, rl = df.high.iloc[-22:-2].max(), df.low.iloc[-22:-2].min()
    width = rh-rl
    compressed = width/max(float(a55.iat[-2]), 1e-12) <= 8.0
    bull_po3 = compressed and df.low.iat[-1] < rl and df.close.iat[-1] > rl
    bear_po3 = compressed and df.high.iat[-1] > rh and df.close.iat[-1] < rh
    rv = liq_rsi(df.close, 20).iat[-1]
    if (lower_trap or bull_po3) and rv < 55:
        stop = min(df.low.iloc[-2:].min(), rl)-0.5*a14.iat[-1]
        target = max(basis.iat[-1], rh)
        return {"side":"LONG", "stop":float(stop), "target":float(target), "tag":"TRAP/PO3+RSI"}
    if (upper_trap or bear_po3) and rv > 45:
        stop = max(df.high.iloc[-2:].max(), rh)+0.5*a14.iat[-1]
        target = min(basis.iat[-1], rl)
        return {"side":"SHORT", "stop":float(stop), "target":float(target), "tag":"TRAP/PO3+RSI"}
    return None

def liq_df(s):
    k=klines(s)
    if not k or len(k)<100:return None
    # klines() already excludes the forming candle in this bot.
    return pd.DataFrame({
        "open":[float(x[1]) for x in k],
        "high":[float(x[2]) for x in k],
        "low":[float(x[3]) for x in k],
        "close":[float(x[4]) for x in k],
        "volume":[float(x[5]) for x in k],
    })

def liquidity_entry_signal(s):
    df=liq_df(s)
    if df is None:return None
    try:
        return liq_liquidity_signal(df)
    except Exception as e:
        logging.warning("%s liquidity signal: %s",s,e)
        return None

def scan():
    global btc_mode,last_entry_candle
    new=sig("BTCUSDT")
    if new!=btc_mode:
        old_mode=btc_mode
        msg(f"BTC MODE: {old_mode} -> {new}")
        if mine and old_mode in ("LONG","SHORT") and new!=old_mode:
            if not close_all(f"BTC MODE {old_mode}->{new}"):
                return
        btc_mode=new
        save()
    if time.time()<pause_until or btc_mode=="WAIT":
        return

    btc_rows=klines("BTCUSDT")
    if not btc_rows:
        return
    closed_candle=int(btc_rows[-1][0])

    if basket_lock_candle and closed_candle<=basket_lock_candle:
        return
    if closed_candle==last_entry_candle:
        return

    # Exactly one entry batch for each newly CLOSED 15m BTC candle.
    last_entry_candle=closed_candle
    save()

    slots=max(0,MAX_POS-len(pos()))
    batch_limit=min(2,slots)
    if batch_limit<=0:
        return

    opened=0
    for s in universe():
        if opened>=batch_limit:
            break
        if s in mine:
            continue
        ls=liquidity_entry_signal(s)
        if ls and ls.get("side")==btc_mode:
            try:
                enter(s,btc_mode)
                opened+=1
            except Exception as e:
                logging.warning("%s entry failed: %s",s,e)

    logging.info("BTC CLOSED CANDLE %s | %s | NEW %s/%s | OPEN %s/%s",
                 closed_candle,btc_mode,opened,batch_limit,len(pos()),MAX_POS)

def main():
    if not KEY or not SECRET:raise RuntimeError("Missing Binance demo API keys")
    exchange_info(); load()
    # Never adopt unknown positions: safe for other bots on same account.
    ps=positions()
    for s in list(mine):
        if s not in ps:mine.pop(s,None)
    msg(f"MA BTC Sync Bot {BOT_VERSION} STARTED\n15m SMA 7/25/99 | Allocated: ${ALLOCATED_CAPITAL:.0f} | Notional: $300 | Max: 20 | Basket: NET +$20 AFTER CLOSE FEES | BTC MA25/99 = direction/exit gate | Liquidity Reversal Staged TRAP/PO3+RSI = coin selection/entry | SL50 TP100/150/200\nExcluded: BNB, DOGE, BCH | Liquidity floor: ${MIN_VOL:,.0f}/24h")
    last=0
    while True:
        try:
            manage()
            if time.time()-last>=20:scan();last=time.time()
            time.sleep(5)
        except Exception as e:
            logging.exception(e);time.sleep(5)
if __name__=="__main__":main()
