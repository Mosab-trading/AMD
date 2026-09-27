import os,time,hmac,hashlib,json,logging
from decimal import Decimal,ROUND_DOWN
from urllib.parse import urlencode
import requests
import pandas as pd
import numpy as np

KEY=os.getenv("BINANCE_API_KEY",""); SECRET=os.getenv("BINANCE_API_SECRET","")
BASE=os.getenv("EXCHANGE_BASE_URL","https://fapi.binance.com").rstrip("/")
TG=os.getenv("TELEGRAM_BOT_TOKEN",""); CHAT=os.getenv("TELEGRAM_CHAT_ID","")
BOT_VERSION="V2.1.2-BREAKEVEN-SLOTS-MAXQTY-FIX-LIVE-NO-BASKET-50REPORT"
TF="15m"; NOTIONAL=100.0; TARGET_LEV=20; MAX_POS=30
MIN_VOL=float(os.getenv("MIN_QUOTE_VOLUME","5000000"))
EXCLUDED={"BNBUSDT","DOGEUSDT","BCHUSDT"}
BASKET=50.0; LOSS_LIMIT=100.0
ALLOCATED_CAPITAL=float(os.getenv("ALLOCATED_CAPITAL","500"))
TAKER_FEE_RATE=float(os.getenv("TAKER_FEE_RATE","0.0005"))
S=requests.Session(); S.headers.update({"X-MBX-APIKEY":KEY})
logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
meta={}; mine={}; btc_mode="WAIT"; pause_until=0; loss_window=0; losing_cycles=0; cycle_realized=0; bot_realized=0; basket_lock_candle=0; entry_candle=0; entries_this_candle=0; basket_rearm_dir=""; basket_rearm_touched=False
STATE="state.json"
REPORT_STATE="hundred_trade_report.json"
REPORT_EVERY_TRADES=100

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
    # Respect Binance MARKET_LOT_SIZE maxQty as well as step/precision.
    # This is intentionally only an execution-safety fix; signal/risk logic is unchanged.
    max_q=float(meta[s].get("max",0) or 0)
    if max_q>0:
        x=min(float(x),max_q)
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
        meta[s["symbol"]]={"step":float(lot.get("stepSize",".001")),"min":float(lot.get("minQty","0")),"max":float(lot.get("maxQty","0") or 0),"tick":float(pf.get("tickSize",".0001")),"qtyPrecision":int(s.get("quantityPrecision",8))}
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

MONITOR_STATE="portfolio_monitor_state.json"
MONITOR_LOG="portfolio_monitor.jsonl"
MONITOR_INTERVAL=int(os.getenv("MONITOR_INTERVAL_SECONDS","30"))
FLOW_RADAR_STATE_URL=os.getenv("FLOW_RADAR_STATE_URL","").strip()
FLOW_RADAR_MAX_AGE=float(os.getenv("FLOW_RADAR_MAX_AGE","15"))

# This program intentionally has NO entry/open-position function.
# Its only trading action is reduce-only CLOSE ALL after a confirmed RED signal.

def roi(p):
    amt=abs(float(p["positionAmt"])); ep=float(p["entryPrice"]); lev=float(p.get("leverage",20)); pnl=float(p["unRealizedProfit"])
    margin=amt*ep/max(lev,1); return 100*pnl/margin if margin else 0.0

def load_monitor_state():
    try:
        d=json.load(open(MONITOR_STATE))
        if not isinstance(d,dict): return {}
        return d
    except Exception:
        return {}

def save_monitor_state(d):
    tmp=MONITOR_STATE+".tmp"
    with open(tmp,"w") as f: json.dump(d,f,indent=2)
    os.replace(tmp,MONITOR_STATE)

def append_snapshot(row):
    with open(MONITOR_LOG,"a") as f:
        f.write(json.dumps(row,separators=(",",":"))+"\n")

def flow_radar_red():
    """Consume Flow Radar's own RED result exactly as the prior live RED Guard did.
    Missing/stale/unavailable radar state is fail-safe: no forced close.
    """
    if not FLOW_RADAR_STATE_URL:
        return False
    try:
        r=requests.get(FLOW_RADAR_STATE_URL,timeout=5)
        r.raise_for_status()
        d=r.json()
        ts=float(d.get("ts",0) or 0)
        if not ts or time.time()-ts>FLOW_RADAR_MAX_AGE:
            logging.warning("FLOW RADAR RED GUARD stale state; ignoring")
            return False
        return str(d.get("regime","")).upper()=="RED"
    except Exception as e:
        logging.warning("FLOW RADAR RED GUARD unavailable: %s",e)
        return False

def close_all_account_positions(reason):
    """Emergency protection: close EVERY currently open Binance Futures position.
    Uses reduceOnly market orders and retries. No new exposure can be created here.
    """
    ps=positions()
    if not ps:
        msg(f"{reason} | No open positions",bal=False); return True
    failed=[]
    for s,p in list(ps.items()):
        try: cancel_algo(s)
        except Exception: pass
    for s,p in list(ps.items()):
        ok=False
        for attempt in range(1,4):
            try:
                live=pos(s)
                if not live: ok=True; break
                amt=abs(float(live["positionAmt"]))
                side="SELL" if float(live["positionAmt"])>0 else "BUY"
                market(s,side,amt,True)
                time.sleep(.35)
                if not pos(s): ok=True; break
            except Exception as e:
                logging.error("%s emergency close attempt %d/3: %s",s,attempt,e)
                time.sleep(1)
        if not ok: failed.append(s)
    if failed:
        msg("RED CLOSE INCOMPLETE | still open: "+", ".join(failed),bal=False)
        return False
    msg(f"🔴 {reason} | ALL FUTURES POSITIONS CLOSED",bal=False)
    return True

def monitor_snapshot(state):
    ps=positions()
    now=int(time.time()*1000)
    previous=state.get("positions",{})
    current={}
    total_pnl=0.0; total_notional=0.0; weighted_margin=0.0
    winners=losers=flat=0
    weakening=0

    for s,p in ps.items():
        amt=float(p["positionAmt"]); qty=abs(amt); ep=float(p["entryPrice"])
        mark=float(p.get("markPrice") or ep); lev=float(p.get("leverage",1) or 1)
        pnl=float(p.get("unRealizedProfit",0)); r=roi(p)
        side="LONG" if amt>0 else "SHORT"
        notional=qty*mark; margin=qty*ep/max(lev,1)
        total_pnl+=pnl; total_notional+=notional; weighted_margin+=margin
        if pnl>0:winners+=1
        elif pnl<0:losers+=1
        else:flat+=1
        old=previous.get(s,{})
        old_pnl=old.get("pnl")
        if old_pnl is not None and pnl < float(old_pnl): weakening+=1
        peak=max(float(old.get("peak_pnl",pnl)),pnl)
        trough=min(float(old.get("trough_pnl",pnl)),pnl)
        current[s]={"side":side,"qty":qty,"entry":ep,"mark":mark,"leverage":lev,
                    "pnl":pnl,"roi":r,"notional":notional,"peak_pnl":peak,"trough_pnl":trough,
                    "giveback":max(0.0,peak-pnl),"seen_ms":now}

    opened=sorted(set(current)-set(previous)); closed=sorted(set(previous)-set(current))
    peak_port=max(float(state.get("peak_portfolio_pnl",total_pnl)),total_pnl)
    trough_port=min(float(state.get("trough_portfolio_pnl",total_pnl)),total_pnl)
    giveback=max(0.0,peak_port-total_pnl)
    giveback_pct=(100*giveback/peak_port) if peak_port>0 else 0.0
    breadth=(100*weakening/len(current)) if current else 0.0
    portfolio_roi=(100*total_pnl/weighted_margin) if weighted_margin else 0.0

    row={"ts":now,"open_positions":len(current),"opened":opened,"closed":closed,
         "total_unrealized_pnl":total_pnl,"portfolio_roi":portfolio_roi,
         "peak_portfolio_pnl":peak_port,"trough_portfolio_pnl":trough_port,
         "giveback_usdt":giveback,"giveback_pct":giveback_pct,
         "winners":winners,"losers":losers,"flat":flat,"weakening":weakening,
         "weakening_pct":breadth,"total_notional":total_notional,"positions":current}
    append_snapshot(row)

    if opened: msg("MONITOR OPEN DETECTED: "+", ".join(opened),bal=False)
    if closed: msg("MONITOR CLOSE DETECTED: "+", ".join(closed),bal=False)
    logging.info("PORTFOLIO | open=%d | uPnL=$%.2f | ROI=%.2f%% | peak=$%.2f | giveback=$%.2f (%.1f%%) | weakening=%d/%d (%.1f%%) | W/L=%d/%d",
                 len(current),total_pnl,portfolio_roi,peak_port,giveback,giveback_pct,weakening,len(current),breadth,winners,losers)
    state.update({"positions":current,"peak_portfolio_pnl":peak_port,"trough_portfolio_pnl":trough_port,
                  "last_snapshot_ms":now})
    save_monitor_state(state)
    return state

def main():
    if not KEY or not SECRET: raise RuntimeError("Missing Binance LIVE API keys")
    exchange_info()
    state=load_monitor_state()
    msg("LIVE PORTFOLIO MONITOR + RED PROTECTION STARTED\nNO ENTRIES | NO TP/SL MANAGEMENT | monitors ALL account positions\nRED confirmed -> CLOSE ALL | slow-bleed data = RECORD ONLY",bal=False)
    red_latched=False; last_monitor=0
    while True:
        try:
            now=time.time()
            if now-last_monitor>=MONITOR_INTERVAL:
                state=monitor_snapshot(state); last_monitor=now
            is_red=flow_radar_red()
            if is_red:
                if (not red_latched) or positions():
                    if close_all_account_positions("FLOW RADAR RED"):
                        red_latched=True
                        state=load_monitor_state(); state["peak_portfolio_pnl"]=0.0; save_monitor_state(state)
            elif red_latched:
                red_latched=False
                msg("FLOW RADAR RED CLEARED | MONITOR CONTINUES",bal=False)
            time.sleep(5)
        except Exception as e:
            logging.exception(e); time.sleep(5)

if __name__=="__main__": main()
