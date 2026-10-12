
"""بوت تداول وهمي فقط — لا ينفذ أي أوامر شراء أو بيع حقيقية."""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

# ===== الإعدادات =====
BASE_URL = "https://api.binance.com"
STATE_FILE = Path("paper_state.json")
SYMBOLS = ("BTCUSDT", "ETHUSDT")

START_BALANCE = 100.0
FEE_RATE = 0.001          # رسوم 0.1% لكل عملية
SLIPPAGE_RATE = 0.0005    # انزلاق سعري 0.05%
RISK_FRACTION = 0.005     # مخاطرة مستهدفة 0.5% من قيمة المحفظة
DAILY_LOSS_LIMIT = 0.015  # إيقاف فتح صفقات جديدة عند خسارة يومية 1.5%
ATR_MULTIPLIER = 1.5
REWARD_RISK = 2.0

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
session = requests.Session()
session.headers.update({"User-Agent": "crybtoimad-paper-bot/1.0"})


def ema(values, period):
    if period < 1 or len(values) < period:
        return []
    alpha = 2.0 / (period + 1)
    result = [sum(values[:period]) / period]
    for value in values[period:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return [None] * (period - 1) + result


def rsi(values, period=14):
    if len(values) <= period:
        return None
    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]
    gains = [max(x, 0.0) for x in changes]
    losses = [max(-x, 0.0) for x in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period
        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0

    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def atr(candles, period=14):
    if len(candles) <= period:
        return None

    ranges = []
    for i in range(1, len(candles)):
        candle = candles[i]
        previous_close = candles[i - 1]["close"]
        ranges.append(max(
            candle["high"] - candle["low"],
            abs(candle["high"] - previous_close),
            abs(candle["low"] - previous_close),
        ))

    value = sum(ranges[:period]) / period
    for item in ranges[period:]:
        value = (value * (period - 1) + item) / period

    return value


def get_klines(symbol, interval, limit=260):
    response = session.get(
        f"{BASE_URL}/api/v3/klines",
        params={
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        },
        timeout=15,
    )
    response.raise_for_status()
    rows = response.json()

    if not isinstance(rows, list):
        raise RuntimeError(f"بيانات غير متوقعة للعملة {symbol}")

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    # تجاهل الشمعة التي لم تغلق بعد.
    rows = [row for row in rows if int(row[6]) < now_ms]

    if len(rows) < 205:
        raise RuntimeError(
            f"عدد الشموع المغلقة غير كافٍ: {symbol} {interval}"
        )

    return [
        {
            "time": int(row[0]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
        }
        for row in rows
    ]


def new_state():
    return {
        "version": 1,
        "cash": START_BALANCE,
        "positions": {},
        "last_candle": {},
        "trades": [],
        "utc_day": datetime.now(timezone.utc).date().isoformat(),
        "day_start_equity": START_BALANCE,
    }


def load_state():
    if not STATE_FILE.exists():
        return new_state()

    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))

        if not isinstance(state, dict) or state.get("version") != 1:
            raise ValueError("إصدار ملف المحفظة غير مدعوم")

        if not isinstance(state.get("cash"), (int, float)):
            raise ValueError("الرصيد النقدي غير صالح")
        if state["cash"] < -1e-8:
            raise ValueError("الرصيد النقدي سالب")

        for key, default in (
            ("positions", {}),
            ("last_candle", {}),
            ("trades", []),
        ):
            state.setdefault(key, default)

        return state

    except (OSError, ValueError, json.JSONDecodeError) as exc:
        # لا تصفّر المحفظة تلقائيًا عند تلف الملف.
        raise RuntimeError(
            f"تعذّر قراءة {STATE_FILE} بأمان: {exc}"
        ) from exc


def save_state(state):
    temp_file = STATE_FILE.with_suffix(".tmp")
    temp_file.write_text(
        json.dumps(state, indent=2),
        encoding="utf-8",
    )
    temp_file.replace(STATE_FILE)


def equity(state, prices):
    total = float(state["cash"])
    for symbol, position in state["positions"].items():
        price = prices.get(symbol, position["entry"])
        total += float(position["qty"]) * float(price)
    return total


def record_trade(
    state, symbol, side, price, qty, fee, reason, pnl=None
):
    state["trades"].append({
        "time": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "side": side,
        "price": round(price, 8),
        "qty": round(qty, 8),
        "fee": round(fee, 8),
        "reason": reason,
        "realized_pnl": (
            None if pnl is None else round(pnl, 8)
        ),
    })
    state["trades"] = state["trades"][-500:]


def buy(state, symbol, candle, atr_value, account_equity):
    cash = float(state["cash"])
    entry = candle["close"] * (1 + SLIPPAGE_RATE)
    stop = entry - ATR_MULTIPLIER * atr_value
    risk_per_unit = entry - stop

    if entry <= 0 or atr_value <= 0 or risk_per_unit <= 0:
        return None

    # حجم الصفقة محدود بالمخاطرة وبالنقد المتاح بعد الرسوم.
    qty_by_risk = (
        account_equity * RISK_FRACTION / risk_per_unit
    )
    qty_by_cash = cash / (entry * (1 + FEE_RATE))
    qty = min(qty_by_risk, qty_by_cash)

    if qty <= 0:
        return None

    cost = qty * entry
    fee = cost * FEE_RATE
    state["cash"] = cash - cost - fee

    target = entry + REWARD_RISK * risk_per_unit
    state["positions"][symbol] = {
        "qty": qty,
        "entry": entry,
        "stop": stop,
        "target": target,
        "entry_fee": fee,
    }

    record_trade(
        state, symbol, "BUY", entry, qty, fee, "strategy_entry"
    )

    return (
        f"🟢 PAPER BUY {symbol} @ {entry:.2f}\n"
        f"الكمية: {qty:.6f}\n"
        f"وقف الخسارة: {stop:.2f}\n"
        f"الهدف: {target:.2f}"
    )


def sell(state, symbol, raw_price, reason):
    position = state["positions"].pop(symbol)
    price = float(raw_price) * (1 - SLIPPAGE_RATE)
    qty = float(position["qty"])
    proceeds = qty * price
    fee = proceeds * FEE_RATE

    state["cash"] += proceeds - fee

    pnl = (
        proceeds
        - fee
        - qty * float(position["entry"])
        - float(position["entry_fee"])
    )

    record_trade(
        state, symbol, "SELL", price, qty, fee, reason, pnl
    )

    return (
        f"🔴 PAPER SELL {symbol} @ {price:.2f}\n"
        f"النتيجة: {pnl:+.3f} USDT\n"
        f"السبب: {reason}"
    )


def market_signal(symbol):
    hourly = get_klines(symbol, "1h")
    four_hour = get_klines(symbol, "4h")

    closes = [candle["close"] for candle in hourly]
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    ema200_4h = ema(
        [candle["close"] for candle in four_hour], 200
    )

    i = len(closes) - 1
    previous = i - 1
    rsi_value = rsi(closes, 14)
    atr_value = atr(hourly, 14)

    if (
        rsi_value is None
        or atr_value is None
        or ema200_4h[-1] is None
    ):
        raise RuntimeError(f"المؤشرات غير جاهزة: {symbol}")

    crossed_up = (
        ema20[previous] <= ema50[previous]
        and ema20[i] > ema50[i]
    )
    crossed_down = (
        ema20[previous] >= ema50[previous]
        and ema20[i] < ema50[i]
    )

    return {
        "candle": hourly[-1],
        "atr": atr_value,
        "buy": (
            crossed_up
            and closes[-1] > ema200_4h[-1]
            and 50 <= rsi_value <= 70
        ),
        "exit": crossed_down,
    }


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.info(
            "لم تُضبط أسرار تيليغرام؛ تم تخطي إرسال الرسالة."
        )
        return

    response = session.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
        },
        timeout=15,
    )
    response.raise_for_status()

    if not response.json().get("ok"):
        raise RuntimeError("رفضت واجهة تيليغرام الرسالة")


def run():
    state = load_state()

    # اجلب كل البيانات قبل تعديل حالة المحفظة.
    signals = {
        symbol: market_signal(symbol)
        for symbol in SYMBOLS
    }
    prices = {
        symbol: signals[symbol]["candle"]["close"]
        for symbol in SYMBOLS
    }

    current_equity = equity(state, prices)
    today = datetime.now(timezone.utc).date().isoformat()

    if state.get("utc_day") != today:
        state["utc_day"] = today
        state["day_start_equity"] = current_equity

    day_start = float(
        state.get("day_start_equity", START_BALANCE)
    )
    loss_paused = (
        current_equity <= day_start * (1 - DAILY_LOSS_LIMIT)
    )

    messages = []

    for symbol in SYMBOLS:
        signal = signals[symbol]
        candle = signal["candle"]
        candle_key = str(candle["time"])

        # لا تكرر القرار على الشمعة نفسها.
        if state["last_candle"].get(symbol) == candle_key:
            continue

        position = state["positions"].get(symbol)

        if position:
            # إذا لمس الوقف والهدف في الشمعة نفسها، افترض الوقف أولًا.
            if candle["low"] <= position["stop"]:
                messages.append(
                    sell(state, symbol, position["stop"], "stop_loss")
                )
            elif candle["high"] >= position["target"]:
                messages.append(
                    sell(state, symbol, position["target"], "take_profit")
                )
            elif signal["exit"]:
                messages.append(
                    sell(
                        state, symbol, candle["close"],
                        "EMA_cross_down",
                    )
                )

        elif not loss_paused and signal["buy"]:
            message = buy(
                state,
                symbol,
                candle,
                signal["atr"],
                current_equity,
            )
            if message:
                messages.append(message)

        state["last_candle"][symbol] = candle_key

    current_equity = equity(state, prices)
    summary = (
        "📊 PAPER TRADING ONLY — تداول وهمي فقط\n"
        f"النقد: {state['cash']:.2f} USDT\n"
        f"قيمة المحفظة: {current_equity:.2f} USDT\n"
        f"الصفقات المفتوحة: {len(state['positions'])}\n"
        f"إيقاف فتح صفقات جديدة بسبب حد الخسارة: "
        f"{'نعم' if loss_paused else 'لا'}"
    )

    save_state(state)
    logging.info("%s", summary.replace("\n", " | "))

    if messages:
        send_telegram("\n\n".join(messages + [summary]))
        for message in messages:
            logging.info("%s", message)


if __name__ == "__main__":
    run()
