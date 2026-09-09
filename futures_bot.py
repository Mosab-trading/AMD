import os,time,hmac,hashlib,json,logging
from decimal import Decimal,ROUND_DOWN
from urllib.parse import urlencode
import requests
import pandas as pd
import numpy as np

KEY=os.getenv("BINANCE_DEMO_API_KEY",""); SECRET=os.getenv("BINANCE_DEMO_API_SECRET","")
BASE=os.getenv("EXCHANGE_BASE_URL","https://demo-fapi.binance.com").rstrip("/")
TG=os.getenv("TELEGRAM_BOT_TOKEN",""); CHAT=os.getenv("TELEGRAM_CHAT_ID","")
BOT_VERSION="V2.1.1-TELEGRAM-BTC-REGIME-DEMO"
TF="15m"; NOTIONAL=300.0; TARGET_LEV=20; MAX_POS=20
MIN_VOL=float(os.getenv("MIN_QUOTE_VOLUME","5000000"))
EXCLUDED={"BNBUSDT","DOGEUSDT","BCHUSDT"}
BASKET=50.0; LOSS_LIMIT=100.0
ALLOCATED_CAPITAL=float(os.getenv("ALLOCATED_CAPITAL","500"))
TAKER_FEE_RATE=float(os.getenv("TAKER_FEE_RATE","0.0005"))
S=requests.Session(); S.headers.update({"X-MBX-APIKEY":KEY})
logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
meta={}; mine={}; btc_mode="WAIT"; pause_until=0; loss_window=0; losing_cycles=0; cycle_realized=0; bot_realized=0; basket_lock_candle=0; entry_candle=0; entries_this_candle=0; basket_rearm_dir=""; basket_rearm_touched=False
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
def trade_rows(s,start_ms=0):
    p={"symbol":s,"limit":1000}
    if start_ms:p["startTime"]=int(start_ms)
    return signed("GET","/fapi/v1/userTrades",p)

def sync_realized():
    """Book ACTUAL Binance realizedPnl and commissions for this bot's trades."""
    global bot_realized,cycle_realized,loss_window
    changed=False
    for s,st in list(mine.items()):
        try:
            seen=set(str(x) for x in st.get("accounted_trade_ids",[]))
            rows=trade_rows(s,st.get("entry_time",0))
            for tr in rows:
                tid=str(tr.get("id"))
                if tid in seen:continue
                # Binance userTrades: realizedPnl is exact realized profit/loss;
                # commission is an actual cost and must be deducted.
                delta=float(tr.get("realizedPnl",0))-float(tr.get("commission",0))
                bot_realized+=delta
                cycle_realized+=delta
                loss_window+=delta
                seen.add(tid); changed=True
            st["accounted_trade_ids"]=list(seen)[-2000:]
        except Exception as e:
            logging.warning("%s PnL sync failed: %s",s,e)
    if changed:save()

def live_unrealized():
    try:
        ps=positions()
        return sum(float(p.get("unRealizedProfit",0)) for s,p in ps.items() if s in mine)
    except:
        return 0.0

def bot_balance():
    # Virtual $500 allocation + ACTUAL realized bot PnL + current open PnL.
    return ALLOCATED_CAPITAL + bot_realized + live_unrealized()

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
    with open(STATE,"w") as f: json.dump({"mine":mine,"pause":pause_until,"loss":loss_window,"losing":losing_cycles,"cycle":cycle_realized,"bot_realized":bot_realized,"basket_lock_candle":basket_lock_candle,"btc_mode":btc_mode,"entry_candle":entry_candle,"entries_this_candle":entries_this_candle,"basket_rearm_dir":basket_rearm_dir,"basket_rearm_touched":basket_rearm_touched},f)
def load():
    global mine,pause_until,loss_window,losing_cycles,cycle_realized,bot_realized,basket_lock_candle,btc_mode,entry_candle,entries_this_candle,basket_rearm_dir,basket_rearm_touched
    try:
        d=json.load(open(STATE)); mine=d.get("mine",{}); pause_until=d.get("pause",0); loss_window=d.get("loss",0); losing_cycles=d.get("losing",0); cycle_realized=d.get("cycle",0); bot_realized=d.get("bot_realized",0); basket_lock_candle=d.get("basket_lock_candle",0); btc_mode=d.get("btc_mode","WAIT"); entry_candle=d.get("entry_candle",0); entries_this_candle=d.get("entries_this_candle",0); basket_rearm_dir=d.get("basket_rearm_dir",""); basket_rearm_touched=d.get("basket_rearm_touched",False)
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

def btc_ma_snapshot():
    """Last CLOSED BTC 15m candle + SMA25/SMA99."""
    k=klines("BTCUSDT")
    if not k or len(k)<99:return None
    closes=[float(x[4]) for x in k]
    row=k[-1]
    return {
        "candle":int(row[0]),
        "high":float(row[2]),
        "low":float(row[3]),
        "close":float(row[4]),
        "ma25":sum(closes[-25:])/25,
        "ma99":sum(closes[-99:])/99,
    }

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
    market(s,"SELL" if float(p["positionAmt"])>0 else "BUY",qty,True)
    time.sleep(.35)
    sync_realized()
    msg(f"{s} {reason}\nPnL booked from Binance trade history")
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
    # V2.1: keep ONE exchange-side protective STOP only. Profit targets are managed
    # by manage() from live leveraged ROI. This prevents -4045 max algo/stop-order saturation.
    mine[s]={"dir":d,"tp1":False,"tp2":False,"lock_stage":0,
             "initial_qty":abs(float(p["positionAmt"])),"entry_time":int(time.time()*1000)-10000,
             "accounted_trade_ids":[]}; save()
    sync_realized()
    msg(f"OPEN {d} {s}\nNotional: $300 | Leverage: {lev}x\nEntry: {ep}\nProfit Lock: +30->SL -25 | +50->BE | +75->SL +25 | TP1 +100% (50%, SL +50) | TP2 +150% (25%, SL +100) | TP3 +200% final")
def close_all(reason):
    global cycle_realized,losing_cycles,pause_until,basket_lock_candle,basket_rearm_dir,basket_rearm_touched
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

    # V2: BTC is context only, never a hard direction gate.
    # Keep the existing one-closed-candle basket lock; disable MA25 directional re-arm.
    if reason=="BASKET NET +$50":
        basket_rearm_dir=""
        basket_rearm_touched=False
        msg("BASKET +$50 CLOSED | next cycle waits for the next closed BTC 15m candle")
    save()
    return True

def protected_stop_for_roi(s,p,d,target_roi):
    """Replace the single exchange-side STOP so a retrace locks target leveraged ROI."""
    ep=float(p["entryPrice"]); lev=float(p.get("leverage",20))
    move=(float(target_roi)/100.0)/max(lev,1.0)
    sp=ep*(1+move) if d=="LONG" else ep*(1-move)
    cancel_algo(s)
    stop(s,d,sp)
    return sp

def manage():
    global pause_until,loss_window
    sync_realized()
    ps=positions()
    for s in list(mine):
        if s not in ps:
            sync_realized()
            mine.pop(s,None); save()
            msg(f"{s} CLOSED ON EXCHANGE | Actual PnL reconciled")
    gross_total=cycle_realized+sum(float(p["unRealizedProfit"]) for s,p in ps.items() if s in mine)
    expected_close_fees=estimated_exit_fees(ps)
    net_after_close=gross_total-expected_close_fees
    if mine and net_after_close>=BASKET:
        msg(f"BASKET NET TARGET ${net_after_close:.2f} AFTER EST. CLOSE FEES -> CLOSE ALL")
        close_all("BASKET NET +$50")
        return

    for s in list(mine):
        p=ps.get(s)
        if not p: continue
        d="LONG" if float(p["positionAmt"])>0 else "SHORT"
        r=roi(p)
        st=int(mine[s].get("lock_stage",0))
        initial_qty=float(mine[s].get("initial_qty",abs(float(p["positionAmt"]))))

        try:
            # Stair-step profit protection. Stages only move forward; never loosen a stop.
            if r>=200 and st<6:
                cancel_algo(s)
                close(s,p,100,"TP3 +200% ROI FINAL")
                mine[s]["lock_stage"]=6; save()
                continue
            if r>=150 and st<5:
                # Close 25% of ORIGINAL size (normally 50% of the remaining half).
                live_qty=abs(float(p["positionAmt"]))
                q_pct=min(100.0,100.0*(initial_qty*0.25)/max(live_qty,1e-12))
                cancel_algo(s); close(s,p,q_pct,"TP2 +150% ROI (25% ORIGINAL)")
                time.sleep(.25); lp=pos(s)
                if lp: protected_stop_for_roi(s,lp,d,100)
                mine[s]["tp2"]=True; mine[s]["lock_stage"]=5; save()
                msg(f"{s} PROFIT LOCK | TP2 DONE | Remaining SL -> +100% ROI")
                continue
            if r>=100 and st<4:
                cancel_algo(s); close(s,p,50,"TP1 +100% ROI (50%)")
                time.sleep(.25); lp=pos(s)
                if lp: protected_stop_for_roi(s,lp,d,50)
                mine[s]["tp1"]=True; mine[s]["lock_stage"]=4; save()
                msg(f"{s} PROFIT LOCK | TP1 DONE | Remaining SL -> +50% ROI")
                continue
            if r>=75 and st<3:
                protected_stop_for_roi(s,p,d,25)
                mine[s]["lock_stage"]=3; save()
                msg(f"{s} PROFIT LOCK | ROI +75% -> SL +25% ROI")
                continue
            if r>=50 and st<2:
                protected_stop_for_roi(s,p,d,0)
                mine[s]["lock_stage"]=2; save()
                msg(f"{s} PROFIT LOCK | ROI +50% -> SL BREAKEVEN")
                continue
            if r>=30 and st<1:
                protected_stop_for_roi(s,p,d,-25)
                mine[s]["lock_stage"]=1; save()
                msg(f"{s} PROFIT LOCK | ROI +30% -> SL -25% ROI")
                continue
        except Exception as e:
            logging.warning("%s profit-lock management failed: %s",s,e)
            # Best effort: if stop replacement/partial close failed, do not advance stage.

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

def open_position_count():
    try:
        return sum(1 for p in signed("GET","/fapi/v2/positionRisk") if abs(float(p.get("positionAmt",0)))>0)
    except Exception as e:
        logging.warning("open position count failed: %s",e)
        return len(mine)

def rsi_last(vals, period=14):
    x=pd.Series(vals,dtype=float); d=x.diff()
    up=d.clip(lower=0).ewm(alpha=1/period,adjust=False).mean()
    dn=(-d.clip(upper=0)).ewm(alpha=1/period,adjust=False).mean()
    rs=up/dn.replace(0,np.nan); z=100-(100/(1+rs))
    return float(z.iloc[-1]) if len(z) and pd.notna(z.iloc[-1]) else 50.0

def btc_context():
    """BTC flow is a SMALL scoring input only. It cannot block either side."""
    k=klines("BTCUSDT")
    if not k or len(k)<100:return {"bias":"NEUTRAL","long_bonus":0.0,"short_bonus":0.0,"score":0.0}
    c=np.array([float(x[4]) for x in k]); h=np.array([float(x[2]) for x in k])
    l=np.array([float(x[3]) for x in k]); v=np.array([float(x[5]) for x in k])
    tb=np.array([float(x[9]) for x in k])
    e25=float(pd.Series(c).ewm(span=25,adjust=False).mean().iloc[-1])
    e99=float(pd.Series(c).ewm(span=99,adjust=False).mean().iloc[-1])
    tp=(h+l+c)/3; vv=v[-20:]
    vwap=float(np.sum(tp[-20:]*vv)/max(np.sum(vv),1e-12))
    buy=float(np.sum(tb[-3:])/max(np.sum(v[-3:]),1e-12))
    vr=float(v[-1]/max(np.mean(v[-20:]),1e-12))
    score=(2.5 if c[-1]>vwap else -2.5)+(2 if e25>e99 else -2)
    score+=max(-3,min(3,(buy-.5)*20))
    if vr>1.2:score+=1.5 if c[-1]>c[-2] else -1.5
    bias="LONG" if score>=2.5 else "SHORT" if score<=-2.5 else "NEUTRAL"
    bonus=max(-8,min(8,score))
    return {"bias":bias,"long_bonus":bonus,"short_bonus":-bonus,"score":score,
            "buy_ratio":buy,"vol_ratio":vr,"close":float(c[-1]),"vwap":vwap}

def long_engine(s,btc):
    """Dedicated LONG: trend/reclaim + CHOCH/retest + volume/aggression."""
    k=klines(s)
    if not k or len(k)<100:return None
    o=np.array([float(x[1]) for x in k]); h=np.array([float(x[2]) for x in k])
    l=np.array([float(x[3]) for x in k]); c=np.array([float(x[4]) for x in k])
    v=np.array([float(x[5]) for x in k]); tb=np.array([float(x[9]) for x in k])
    e21=float(pd.Series(c).ewm(span=21,adjust=False).mean().iloc[-1])
    e55=float(pd.Series(c).ewm(span=55,adjust=False).mean().iloc[-1])
    tp=(h+l+c)/3; vwap=float(np.sum(tp[-20:]*v[-20:])/max(np.sum(v[-20:]),1e-12))
    r=rsi_last(c); vr=float(v[-1]/max(np.mean(v[-20:]),1e-12))
    buy=float(np.sum(tb[-3:])/max(np.sum(v[-3:]),1e-12))
    prior_high=float(np.max(h[-12:-2]))
    choch=c[-1]>prior_high or (c[-1]>e21 and c[-2]<=e21)
    retest=l[-1]<=max(e21,vwap)*1.003 and c[-1]>max(e21,vwap)
    trend=e21>e55 and c[-1]>e21
    reclaim=c[-1]>vwap and c[-2]<=vwap
    impulse=c[-1]>o[-1] and vr>=1.05
    score=0.0
    if trend:score+=24
    if c[-1]>vwap:score+=14
    if choch:score+=18
    if retest or reclaim:score+=14
    if 48<=r<=72:score+=10
    if buy>=.52:score+=10
    if impulse:score+=10
    score+=float(btc.get("long_bonus",0))
    if not (score>=70 and (choch or retest or reclaim) and (trend or c[-1]>vwap)):return None
    return {"side":"LONG","score":round(score,2),"tag":"LONG_TREND+VWAP+CHOCH+FLOW",
            "details":f"rsi={r:.1f} vol={vr:.2f} buy={buy:.2f}"}

def short_engine(s,btc):
    """Preserve original Liquidity Reversal SHORT trigger; add quality ranking."""
    ls=liquidity_entry_signal(s)
    if not ls or ls.get("side")!="SHORT":return None
    k=klines(s)
    if not k or len(k)<100:return None
    o=np.array([float(x[1]) for x in k]); h=np.array([float(x[2]) for x in k])
    l=np.array([float(x[3]) for x in k]); c=np.array([float(x[4]) for x in k])
    v=np.array([float(x[5]) for x in k]); tb=np.array([float(x[9]) for x in k])
    e21=float(pd.Series(c).ewm(span=21,adjust=False).mean().iloc[-1])
    r=rsi_last(c); vr=float(v[-1]/max(np.mean(v[-20:]),1e-12))
    buy=float(np.sum(tb[-3:])/max(np.sum(v[-3:]),1e-12))
    breakdown=c[-1]<e21 or c[-1]<l[-2]
    rejection=h[-1]>h[-2] and c[-1]<o[-1]
    score=62.0
    if breakdown:score+=12
    if rejection:score+=8
    if r<58:score+=6
    if buy<=.48:score+=8
    if vr>=1.05 and c[-1]<o[-1]:score+=6
    score+=float(btc.get("short_bonus",0))
    if score<70:return None
    return {"side":"SHORT","score":round(score,2),"tag":"SHORT_LIQUIDITY_REVERSAL+QUALITY",
            "details":f"rsi={r:.1f} vol={vr:.2f} buy={buy:.2f}"}

def scan():
    global btc_mode,entry_candle,entries_this_candle,basket_rearm_dir,basket_rearm_touched,basket_lock_candle
    if time.time()<pause_until:return
    closed_candle=closed_candle_id("BTCUSDT")
    if not closed_candle:return
    ctx=btc_context()
    old_mode=btc_mode
    btc_mode=ctx["bias"]
    logging.info("BTC CONTEXT: %s | score %.2f | taker-buy %.3f | vol %.2f | NOT A HARD GATE",
                 ctx["bias"],ctx["score"],ctx.get("buy_ratio",.5),ctx.get("vol_ratio",1))
    # Telegram only when the BTC market state changes; never spam every scan.
    if btc_mode != old_mode:
        direction_text = ("New positions: LONG only" if btc_mode=="LONG" else
                          "New positions: SHORT only" if btc_mode=="SHORT" else
                          "New positions: LONG or SHORT by setup score")
        msg(f"BTC MARKET CHANGE: {old_mode} -> {btc_mode}\n{direction_text}\nExisting positions continue with Profit Lock / SL / TP", bal=False)
        save()
    if basket_lock_candle and closed_candle<=basket_lock_candle:return
    if closed_candle!=entry_candle:
        entry_candle=closed_candle; entries_this_candle=0; save()
    limit=min(max(0,2-entries_this_candle),max(0,MAX_POS-open_position_count()))
    if limit<=0:return
    candidates=[]
    for s in universe():
        if s in mine:continue
        try:
            # V2.1: market direction controls NEW slots only. Existing positions are never
            # force-closed on a BTC context flip; they keep their own SL/TP management.
            if ctx["bias"] in ("SHORT","NEUTRAL"):
                sh=short_engine(s,ctx)
                if sh:candidates.append((float(sh["score"]),s,sh))
            if ctx["bias"] in ("LONG","NEUTRAL"):
                lo=long_engine(s,ctx)
                if lo:candidates.append((float(lo["score"]),s,lo))
        except Exception as e:logging.warning("%s scoring failed: %s",s,e)
    candidates.sort(key=lambda x:x[0],reverse=True)
    opened=0; used=set()
    for score,s,setup in candidates:
        if opened>=limit:break
        if s in used or s in mine:continue
        try:
            enter(s,setup["side"])
            # enter() writes mine only after a successful protected entry.
            if s in mine:
                opened+=1; entries_this_candle+=1; used.add(s); save()
                logging.info("SELECTED %s %s | score %.2f | %s",setup["side"],s,score,setup["details"])
        except Exception as e:logging.warning("%s entry failed: %s",s,e)
    logging.info("BTC CANDLE %s | CONTEXT %s | OPENED %s | CANDLE TOTAL %s/2 | OPEN %s/%s",
                 closed_candle,ctx["bias"],opened,entries_this_candle,open_position_count(),MAX_POS)

def main():
    if not KEY or not SECRET:raise RuntimeError("Missing Binance demo API keys")
    exchange_info(); load()
    # Never adopt unknown positions: safe for other bots on same account.
    ps=positions()
    for s in list(mine):
        if s not in ps:mine.pop(s,None)
    msg(f"Dual Engine {BOT_VERSION} STARTED\nAllocated: ${ALLOCATED_CAPITAL:.0f} | Notional: $300 | Max: 20 | Basket: NET +$50 AFTER CLOSE FEES | BTC context controls NEW slots only; existing trades are not force-closed | Profit Lock: +30/-25, +50/BE, +75/+25, TP1 +100/50%+SL50, TP2 +150/25%+SL100, TP3 +200 final\nExcluded: BNB, DOGE, BCH | Liquidity floor: ${MIN_VOL:,.0f}/24h")
    last=0
    while True:
        try:
            manage()
            if time.time()-last>=20:scan();last=time.time()
            time.sleep(5)
        except Exception as e:
            logging.exception(e);time.sleep(5)
if __name__=="__main__":main()
