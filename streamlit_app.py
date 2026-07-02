
# app.py - NIFTY OI Dashboard (Upstox v2)

import streamlit as st
import pandas as pd
import requests
import plotly.express as px
import os
from datetime import datetime
from streamlit_autorefresh import st_autorefresh
from datetime import datetime, time
from zoneinfo import ZoneInfo

# =========================
# MARKET HOURS CHECK
# =========================

IST = ZoneInfo("Asia/Kolkata")

def is_market_hours():

    now = datetime.now(IST)

    # Monday=0 ... Friday=4
    if now.weekday() > 4:
        return False

    market_open = time(9, 15)
    market_close = time(15, 30)

    return market_open <= now.time() <= market_close

st_autorefresh(
    interval=60000,   # 1 minutes
    key="refresh"
)

ACCESS_TOKEN = "eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ.eyJzdWIiOiIxNzk4NzkiLCJqdGkiOiI2YTJmZWQ4NWUxMTdlZTc1MmYxNjU1ZGMiLCJpc011bHRpQ2xpZW50IjpmYWxzZSwiaXNQbHVzUGxhbiI6ZmFsc2UsImlzRXh0ZW5kZWQiOnRydWUsImlhdCI6MTc4MTUyNTg5MywiaXNzIjoidWRhcGktZ2F0ZXdheS1zZXJ2aWNlIiwiZXhwIjoxODEzMDk2ODAwfQ.2TuEYMdV9j0a5q2dmKTVL0TNbsyqn9pLdOXvMDvnmD0"
INSTRUMENT_KEY = "NSE_INDEX|Nifty 50"

URL = "https://api.upstox.com/v2/option/chain"
INTRADAY_URL = "https://api.upstox.com/v3/historical-candle/intraday"

CSV_FILE = "snapshots.csv"
DETAIL_FILE = "oi_snapshots_detail.csv"

st.set_page_config(page_title="NIFTY OI Dashboard", layout="wide")

HEADERS = {
    "Accept": "application/json",
    "Api-Version": "2.0",
    "Authorization": f"Bearer {ACCESS_TOKEN}"
}

def get_current_expiry(access_token):

    url = "https://api.upstox.com/v2/option/contracts"

    headers = {
        "Accept": "application/json",
        "Api-Version": "2.0",
        "Authorization": f"Bearer {access_token}"
    }

    params = {
        "instrument_key": "NSE_INDEX|Nifty 50"
    }

    response = requests.get(
        url,
        headers=headers,
        params=params,
        timeout=30
    )

    response.raise_for_status()

    data = response.json()["data"]

    expiries = sorted(
        list(
            set(
                x["expiry"] for x in data
            )
        )
    )

    return expiries[0]

expiry = st.sidebar.text_input("Expiry (YYYY-MM-DD)", value="2026-07-07")

#expiry = get_current_expiry(ACCESS_TOKEN)


def current_slot():
    now = datetime.now(IST)
    slot_minute = (now.minute // 15) * 15

    return now.replace(
        minute=slot_minute,
        second=0,
        microsecond=0
    )
    


def fetch_intraday_data():
    url = f"{INTRADAY_URL}/{INSTRUMENT_KEY}/minutes/1"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    payload = r.json()
    candles = payload["data"]["candles"]
    df = pd.DataFrame(
        candles,
        columns=["datetime","open","high","low","close","volume","oi"]
    )
    df = df.iloc[::-1].reset_index(drop=True)
    return df

def calculate_vwap(df):
    tp = (df["high"] + df["low"] + df["close"]) / 3
    v = df["volume"].replace(0, 1)
    return round(((tp * v).cumsum() / v.cumsum()).iloc[-1], 2)

def fetch_option_chain():
    params = {
        "instrument_key": INSTRUMENT_KEY,
        "expiry_date": expiry
    }

    r = requests.get(URL, params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()

    payload = r.json()

    if payload.get("status") != "success":
        raise Exception(payload)

    return payload["data"]

def process_chain(data):

    spot = float(data[0]["underlying_spot_price"])
    atm = round(spot / 100) * 100

    ce_strikes = {atm, atm+100, atm+200, atm+300, atm+400}
    pe_strikes = {atm, atm-100, atm-200, atm-300, atm-400}

    rows = []

    total_ce_oi = total_pe_oi = 0
    total_ce_change = total_pe_change = 0

    for item in data:

        strike = int(item["strike_price"])

        ce_oi = float(item["call_options"]["market_data"]["oi"])
        ce_prev = float(item["call_options"]["market_data"]["prev_oi"])

        pe_oi = float(item["put_options"]["market_data"]["oi"])
        pe_prev = float(item["put_options"]["market_data"]["prev_oi"])

        ce_daily = ce_oi - ce_prev
        pe_daily = pe_oi - pe_prev

        if strike in ce_strikes:
            total_ce_oi += ce_oi
            total_ce_change += ce_daily

        if strike in pe_strikes:
            total_pe_oi += pe_oi
            total_pe_change += pe_daily

        if strike in ce_strikes or strike in pe_strikes:
            rows.append({
                "Strike": strike,
                "CE OI": int(ce_oi),
                "CE Daily ΔOI": int(ce_daily),
                "PE OI": int(pe_oi),
                "PE Daily ΔOI": int(pe_daily)
            })

    pcr = round(total_pe_oi / max(total_ce_oi, 1), 2)
    change_pcr = round(total_pe_change / max(abs(total_ce_change), 1), 2)
    net_oi = total_pe_change - total_ce_change

    signal = "🟡 NEUTRAL"
    if change_pcr > 1.2 and net_oi > 0:
        signal = "🟢 BULLISH"
    elif change_pcr < 0.8 and net_oi < 0:
        signal = "🔴 BEARISH"

    return spot, atm, rows, pcr, change_pcr, net_oi, signal

def save_snapshot(slot_time, spot, atm, pcr, change_pcr, net_oi, signal, rows):

    if os.path.exists(CSV_FILE):
        hist = pd.read_csv(CSV_FILE)
        if str(slot_time) in hist["timestamp"].astype(str).values:
            return

    pd.DataFrame([{
        "timestamp": slot_time,
        "spot": spot,
        "atm": atm,
        "pcr": pcr,
        "change_pcr": change_pcr,
        "net_oi": net_oi,
        "signal": signal
    }]).to_csv(
        CSV_FILE,
        mode="a",
        header=not os.path.exists(CSV_FILE),
        index=False
    )

    detail = []
    for r in rows:
        detail.append({
            "timestamp": slot_time,
            "strike": r["Strike"],
            "ce_oi": r["CE OI"],
            "pe_oi": r["PE OI"]
        })

    pd.DataFrame(detail).to_csv(
        DETAIL_FILE,
        mode="a",
        header=not os.path.exists(DETAIL_FILE),
        index=False
    )

def add_15m_delta(rows):

    if not os.path.exists(DETAIL_FILE):
        for r in rows:
            r["CE 15m ΔOI"] = 0
            r["PE 15m ΔOI"] = 0
        return rows

    hist = pd.read_csv(DETAIL_FILE)

    if hist.empty:
        return rows

    last_ts = hist["timestamp"].max()
    prev = hist[hist["timestamp"] == last_ts]

    prev_map = {
        int(x["strike"]): x
        for _, x in prev.iterrows()
    }

    for r in rows:

        strike = r["Strike"]

        if strike in prev_map:
            r["CE 15m ΔOI"] = int(r["CE OI"] - prev_map[strike]["ce_oi"])
            r["PE 15m ΔOI"] = int(r["PE OI"] - prev_map[strike]["pe_oi"])
        else:
            r["CE 15m ΔOI"] = 0
            r["PE 15m ΔOI"] = 0

    return rows

st.title("📈 NIFTY ATM ±4 OI Dashboard")

try:

    if not ACCESS_TOKEN:
        st.error("Add Upstox ACCESS_TOKEN in app.py")
        st.stop()

    if not is_market_hours():
        st.warning(
            "Market is closed. Data collection runs only between 09:15 and 15:30 IST (Mon-Fri)."
        )

        if os.path.exists(CSV_FILE):
            history = pd.read_csv(CSV_FILE)
            st.subheader("Last Available Snapshots")
            st.dataframe(history.tail(20), width="stretch")

        st.stop()

    data = fetch_option_chain()
    candles = fetch_intraday_data()
    vwap = calculate_vwap(candles)

    spot, atm, rows, pcr, change_pcr, net_oi, signal = process_chain(data)

    if signal == '🟢 BULLISH' and spot > vwap:
        signal = '🟢 STRONG BUY'
    elif signal == '🔴 BEARISH' and spot < vwap:
        signal = '🔴 STRONG SELL'

    rows = add_15m_delta(rows)

    slot = current_slot()

    save_snapshot(
        slot, spot, atm, pcr, change_pcr, net_oi, signal, rows
    )

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Spot", round(spot, 2))
    c2.metric("VWAP", round(vwap,2), delta=round(spot-vwap,2))
    c3.metric("ATM", atm)
    c4.metric("PCR", pcr)
    c5.metric("Change PCR", change_pcr)
    c6.metric("Signal", signal)

    if spot > vwap:
        st.success(f"Spot is ABOVE VWAP by {spot-vwap:.2f} points")
    elif spot < vwap:
        st.error(f"Spot is BELOW VWAP by {vwap-spot:.2f} points")
    else:
        st.info("Spot is at VWAP")

    c6, c7, c8 = st.columns(3)
    c6.metric("CE Daily ΔOI", f"{sum(x['CE Daily ΔOI'] for x in rows):,}")
    c7.metric("PE Daily ΔOI", f"{sum(x['PE Daily ΔOI'] for x in rows):,}")
    c8.metric("Net OI", f"{net_oi:,.0f}")

    st.subheader("Strike Wise OI")
    st.dataframe(pd.DataFrame(rows).sort_values("Strike", ascending=False), width='stretch')

    if os.path.exists(CSV_FILE):
        history = pd.read_csv(CSV_FILE)

        st.subheader("15 Minute Signal History")
        st.dataframe(history.sort_values("timestamp", ascending=False), width='stretch')

        if len(history) > 1:
            st.subheader("PCR Trend")
            st.plotly_chart(
                px.line(history, x="timestamp", y="pcr", markers=True),
                width='stretch'
            )

            st.subheader("Net OI Trend")
            st.plotly_chart(
                px.line(history, x="timestamp", y="net_oi", markers=True),
                width='stretch'
            )

except Exception as e:
    st.error(str(e))
