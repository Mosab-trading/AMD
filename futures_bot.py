import os,time,hmac,hashlib,json,logging
from decimal import Decimal,ROUND_DOWN
from urllib.parse import urlencode
import requests

KEY=os.getenv("BINANCE_DEMO_API_KEY",""); SECRET=os.getenv("BINANCE_DEMO_API_SECRET","")
BASE=os.getenv("EXCHANGE_BASE_URL","https://demo-fapi.binance.com").rstrip("/")
TG=os.getenv("TELEGRAM_BOT_TOKEN",""); CHAT=os.getenv("TELEGRAM_CHAT_ID","")
TF="15m"; NOTIONAL=300.0; TARGET_LEV=20; MAX_POS=20
MIN_VOL=float(os.getenv("MIN_QUOTE_VOLUME","5000000"))
EXCLUDED={"BNBUSDT","DOGEUSDT","BCHUSDT"}
BASKET=20.0; LOSS_LIMIT=100.0
S=requests.Session(); S.headers.update({"X-MBX-APIKEY":KEY})
logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
meta={}; mine={}; btc_mode="WAIT"; pause_until=0; loss_window=0; losing_cycles=0; cycle_realized=0
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
def msg(t,bal=True):
    if bal:t+=f"\nBalance: ${balance():.2f}"
    logging.info(t.replace("\n"," | "))
    if TG and CHAT:
        try: requests.post(f"https://api.telegram.org/bot{TG}/sendMessage",data={"chat_id":CHAT,"text":t},timeout=8)
        except: pass
def floor(x,step):
    return float((Decimal(str(x))/Decimal(str(step))).to_integral_value(rounding=ROUND_DOWN)*Decimal(str(step)))
def fmt(x): return f"{x:.12f}".rstrip("0").rstrip(".")
def save():
    with open(STATE,"w") as f: json.dump({"mine":mine,"pause":pause_until,"loss":loss_window,"losing":losing_cycles,"cycle":cycle_realized},f)
def load():
    global mine,pause_until,loss_window,losing_cycles,cycle_realized
    try:
        d=json.load(open(STATE)); mine=d.get("mine",{}); pause_until=d.get("pause",0); loss_window=d.get("loss",0); losing_cycles=d.get("losing",0); cycle_realized=d.get("cycle",0)
    except: pass

def exchange_info():
    global meta
    for s in pub("/fapi/v1/exchangeInfo")["symbols"]:
        if s.get("quoteAsset")!="USDT" or s.get("contractType")!="PERPETUAL" or s.get("status")!="TRADING": continue
        fs={x["filterType"]:x for x in s["filters"]}; lot=fs.get("MARKET_LOT_SIZE",fs.get("LOT_SIZE",{})); pf=fs.get("PRICE_FILTER",{})
        meta[s["symbol"]]={"step":float(lot.get("stepSize",".001")),"min":float(lot.get("minQty","0")),"tick":float(pf.get("tickSize",".0001"))}
def positions():
    return {p["symbol"]:p for p in signed("GET","/fapi/v2/positionRisk") if abs(float(p["positionAmt"]))>0}
def pos(s):
    for p in signed("GET","/fapi/v2/positionRisk",{"symbol":s}):
        if abs(float(p["positionAmt"]))>0:return p
def market(s,side,qty,reduce=False):
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
        q["quantity"]=fmt(qty)
    return signed("POST","/fapi/v1/algoOrder",q)

def stop(s,direction,px):
    return algo_close(s,direction,"STOP_MARKET",px,close_position=True)

def place_targets(s,direction,entry_px,lev,full_qty):
    # ROI targets converted to price moves using the actual leverage.
    step=meta[s]["step"]
    half=floor(full_qty*0.5,step)
    rest=floor(full_qty-half,step)
    tp1_move=1.00/lev
    tp2_move=1.50/lev
    tp1=entry_px*(1+tp1_move) if direction=="LONG" else entry_px*(1-tp1_move)
    tp2=entry_px*(1+tp2_move) if direction=="LONG" else entry_px*(1-tp2_move)
    if half>0: algo_close(s,direction,"TAKE_PROFIT_MARKET",tp1,qty=half)
    if rest>0: algo_close(s,direction,"TAKE_PROFIT_MARKET",tp2,qty=rest)
    return tp1,tp2
def leverage(s):
    for l in [20,15,10,8,5,4,3,2,1]:
        try:return int(signed("POST","/fapi/v1/leverage",{"symbol":s,"leverage":l})["leverage"])
        except:continue
    raise RuntimeError("No leverage available")
def klines(s):
    k=pub("/fapi/v1/klines",{"symbol":s,"interval":TF,"limit":110})
    if k and int(k[-1][6])>=int(time.time()*1000):k=k[:-1]
    return k
def sig(s):
    k=klines(s)
    if len(k)<99:return "WAIT"
    c=[float(x[4]) for x in k]; last=c[-1]; m25=sum(c[-25:])/25; m99=sum(c[-99:])/99
    if last>m99 and last>m25:return "LONG"
    if last<m99 and last<m25:return "SHORT"
    return "WAIT"
def universe():
    a=[]
    for t in pub("/fapi/v1/ticker/24hr"):
        s=t["symbol"]
        if s in meta and s!="BTCUSDT" and s not in EXCLUDED and float(t.get("quoteVolume",0))>=MIN_VOL:a.append((s,float(t["quoteVolume"])))
    return [s for s,_ in sorted(a,key=lambda x:x[1],reverse=True)]
def roi(p):
    amt=abs(float(p["positionAmt"])); ep=float(p["entryPrice"]); lev=float(p.get("leverage",20)); pnl=float(p["unRealizedProfit"])
    margin=amt*ep/max(lev,1); return 100*pnl/margin if margin else 0
def close(s,p,pct,reason):
    global loss_window,cycle_realized
    amt=abs(float(p["positionAmt"])); qty=floor(amt*pct/100,meta[s]["step"])
    if qty<=0:return
    est=float(p["unRealizedProfit"])*(qty/amt)
    market(s,"SELL" if float(p["positionAmt"])>0 else "BUY",qty,True)
    cycle_realized+=est; loss_window+=est
    msg(f"{s} {reason}\nPnL approx: ${est:.2f}")
def enter(s,d):
    if s in mine:return
    ps=positions()
    if len(ps)>=MAX_POS or s in ps:return
    px=float(pub("/fapi/v1/ticker/price",{"symbol":s})["price"]); lev=leverage(s)
    qty=floor(NOTIONAL/px,meta[s]["step"])
    if qty<meta[s]["min"] or qty<=0:return
    market(s,"BUY" if d=="LONG" else "SELL",qty); time.sleep(.25); p=pos(s)
    if not p:return
    ep=float(p["entryPrice"]); adverse=.40/lev
    sp=ep*(1-adverse) if d=="LONG" else ep*(1+adverse)
    cancel_algo(s)
    stop(s,d,sp)
    tp1_px,tp2_px=place_targets(s,d,ep,lev,abs(float(p["positionAmt"])))
    mine[s]={"dir":d,"tp1":False,"tp1_px":tp1_px,"tp2_px":tp2_px,"initial_qty":abs(float(p["positionAmt"]))}; save()
    msg(f"OPEN {d} {s}\nNotional: $300 | Leverage: {lev}x\nEntry: {ep}\nTP1: {tp1_px} (50%) | TP2: {tp2_px} (50%)")
def close_all(reason):
    global cycle_realized,losing_cycles,pause_until
    ps=positions(); targets=[(s,p) for s,p in ps.items() if s in mine]
    cycle_total=cycle_realized+sum(float(p["unRealizedProfit"]) for _,p in targets)
    for s,p in targets:
        try: cancel_algo(s); close(s,p,100,reason)
        except Exception as e: logging.error("%s close: %s",s,e)
    if cycle_total<0:losing_cycles+=1
    else:losing_cycles=0
    if losing_cycles>=3:
        pause_until=max(pause_until,time.time()+3600); losing_cycles=0; msg("3 losing cycles -> PAUSE 1 HOUR")
    mine.clear(); cycle_realized=0; save()
def manage():
    global pause_until,loss_window
    ps=positions()
    for s in list(mine):
        if s not in ps:
            mine.pop(s,None); msg(f"{s} CLOSED ON EXCHANGE"); save()
    total=cycle_realized+sum(float(p["unRealizedProfit"]) for s,p in ps.items() if s in mine)
    if mine and total>=BASKET:
        msg(f"BASKET TARGET +${total:.2f} -> CLOSE ALL"); close_all("BASKET +$20"); return
    for s in list(mine):
        p=ps.get(s)
        if not p:continue
        d="LONG" if float(p["positionAmt"])>0 else "SHORT"
        if sig(s)!=d:
            cancel_algo(s); close(s,p,100,"MA EXIT"); mine.pop(s,None); save(); continue
        # TP1/TP2 are exchange-side. Detect TP1 by the position shrinking to about half.
        current_qty=abs(float(p["positionAmt"]))
        initial_qty=float(mine[s].get("initial_qty",current_qty))
        if not mine[s]["tp1"] and initial_qty>0 and current_qty <= initial_qty*0.55:
            # TP1 executed on Binance. Rebuild protection for the remaining half:
            # SL at breakeven + TP2 for remaining quantity.
            cancel_algo(s)
            ep=float(p["entryPrice"])
            stop(s,d,ep)
            lev=float(p.get("leverage",20))
            tp2_move=1.50/max(lev,1)
            tp2=ep*(1+tp2_move) if d=="LONG" else ep*(1-tp2_move)
            algo_close(s,d,"TAKE_PROFIT_MARKET",tp2,qty=current_qty)
            mine[s]["tp1"]=True; save()
            msg(f"{s} TP1 EXECUTED (50%)\nSL moved to BREAKEVEN\nTP2 remains at +150% ROI")
    if loss_window<=-LOSS_LIMIT and time.time()>=pause_until:
        pause_until=time.time()+10800; loss_window=0; save(); msg("Loss window reached -$100 -> PAUSE 3 HOURS")

def scan():
    global btc_mode
    new=sig("BTCUSDT")
    if new!=btc_mode: msg(f"BTC MODE: {btc_mode} -> {new}"); btc_mode=new
    if time.time()<pause_until or btc_mode=="WAIT":return
    slots=MAX_POS-len(positions())
    if slots<=0:return
    opened=0
    for s in universe():
        if opened>=slots:break
        try:
            if sig(s)==btc_mode: enter(s,btc_mode); opened+=1
        except Exception as e: logging.warning("%s: %s",s,e)

def main():
    if not KEY or not SECRET:raise RuntimeError("Missing Binance demo API keys")
    exchange_info(); load()
    # Never adopt unknown positions: safe for other bots on same account.
    ps=positions()
    for s in list(mine):
        if s not in ps:mine.pop(s,None)
    msg(f"MA BTC Sync Bot STARTED\n15m SMA 7/25/99 | $300 | max 20 | Basket +$20\nLiquidity floor: ${MIN_VOL:,.0f}/24h")
    last=0
    while True:
        try:
            manage()
            if time.time()-last>=20:scan();last=time.time()
            time.sleep(5)
        except Exception as e:
            logging.exception(e);time.sleep(5)
if __name__=="__main__":main()
