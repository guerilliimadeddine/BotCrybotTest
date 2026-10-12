
"""بوت تداول وهمي على إطار 10 دقائق — لا ينفذ أوامر حقيقية."""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ==================== الإعدادات ====================

BASE_URLS = (
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
)

STATE_FILE = Path("paper_state.json")
TOP_SYMBOLS_LIMIT = 100
MAX_WORKERS = 10

START_BALANCE = 100.0
FEE_RATE = 0.001
SLIPPAGE_RATE = 0.0005
RISK_FRACTION = 0.005
DAILY_LOSS_LIMIT = 0.015
ATR_MULTIPLIER = 1.5
REWARD_RISK = 2.0

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

EXCLUDED_BASE_ASSETS = {
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP",
    "USDE", "PYUSD", "USTC", "EUR", "EURC", "USDS",
    "USD1", "USDD", "FRAX", "UST",
}
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

session = requests.Session()
session.headers.update({"User-Agent": "paper-trading-bot/2.0"})


# ==================== المؤشرات الفنية ====================

def ema(values, period):
    if len(values) < period:
        return []

    alpha = 2.0 / (period + 1)
    current = sum(values[:period]) / period
    result = [None] * (period - 1) + [current]

    for value in values[period:]:
        current = alpha * value + (1 - alpha) * current
        result.append(current)

    return result


def rsi(values, period=14):
    if len(values) <= period:
        return None

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]

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
        current = candles[i]
        previous_close = candles[i - 1]["close"]

        ranges.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous_close),
            abs(current["low"] - previous_close),
        ))

    value = sum(ranges[:period]) / period

    for item in ranges[period:]:
        value = (value * (period - 1) + item) / period

    return value


# ==================== بيانات Binance ====================

def get_klines(symbol, interval, limit=260):
    errors = []

    for base_url in BASE_URLS:
        try:
            response = session.get(
                f"{base_url}/api/v3/klines",
                params={
                    "symbol": symbol,
                    "interval": interval,
                    "limit": limit,
                },
                timeout=12,
            )
            response.raise_for_status()
            rows = response.json()

            if not isinstance(rows, list):
                raise RuntimeError("استجابة شموع غير صالحة")

            now_ms = int(
                datetime.now(timezone.utc).timestamp() * 1000
            )

            # استعمال الشموع المغلقة فقط.
            rows = [
                row for row in rows
                if int(row[6]) < now_ms
            ]

            if len(rows) < 205:
                raise RuntimeError(
                    f"شموع مغلقة غير كافية: {symbol} {interval}"
                )

            return [
                {
                    "time": int(row[0]),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "quote_volume": float(row[7]),
                    "taker_buy_quote_volume": float(row[10]),
                }
                for row in rows
            ]

        except (
            requests.RequestException,
            ValueError,
            TypeError,
            IndexError,
            RuntimeError,
        ) as exc:
            errors.append(f"{base_url}: {exc}")

    raise RuntimeError(
        f"فشل جلب {symbol} {interval}: " + " | ".join(errors)
    )


def get_top_symbols(limit=TOP_SYMBOLS_LIMIT):
    errors = []

    for base_url in BASE_URLS:
        try:
            info_response = session.get(
                f"{base_url}/api/v3/exchangeInfo",
                timeout=15,
            )
            info_response.raise_for_status()
            info = info_response.json()

            spot_symbols = {
                item["symbol"]
                for item in info.get("symbols", [])
                if item.get("status") == "TRADING"
                and item.get("quoteAsset") == "USDT"
                and item.get("isSpotTradingAllowed", True)
            }

            response = session.get(
                f"{base_url}/api/v3/ticker/24hr",
                timeout=15,
            )
            response.raise_for_status()
            tickers = response.json()

            eligible = []

            for item in tickers:
                symbol = str(item.get("symbol", ""))

                if symbol not in spot_symbols:
                    continue

                if not symbol.endswith("USDT"):
                    continue

                base_asset = symbol[:-4]

                if base_asset in EXCLUDED_BASE_ASSETS:
                    continue

                if base_asset.endswith(LEVERAGED_SUFFIXES):
                    continue

                try:
                    volume = float(item.get("quoteVolume", 0))
                except (TypeError, ValueError):
                    continue

                if volume > 0:
                    eligible.append((symbol, volume))

            eligible.sort(key=lambda pair: pair[1], reverse=True)
            symbols = [symbol for symbol, _ in eligible[:limit]]

            if not symbols:
                raise RuntimeError("لم يتم العثور على أزواج مناسبة")

            logging.info("تم اختيار %d زوج USDT.", len(symbols))
            return symbols

        except (
            requests.RequestException,
            ValueError,
            TypeError,
            RuntimeError,
        ) as exc:
            errors.append(f"{base_url}: {exc}")

    raise RuntimeError(
        "فشل جلب قائمة العملات: " + " | ".join(errors)
    )


# ==================== المحفظة الوهمية ====================

def new_state():
    return {
        "version": 2,
        "cash": START_BALANCE,
        "positions": {},
        "last_candle": {},
        "last_liquidity_alert": {},
        "trades": [],
        "utc_day": datetime.now(timezone.utc).date().isoformat(),
        "day_start_equity": START_BALANCE,
    }


def load_state():
    if not STATE_FILE.exists():
        return new_state()

    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))

        if not isinstance(state, dict):
            raise ValueError("ملف المحفظة غير صالح")

        # الحفاظ على الصفقات المفتوحة من الإصدار السابق.
        state["version"] = 2

        for key, default in (
            ("cash", START_BALANCE),
            ("positions", {}),
            ("last_candle", {}),
            ("last_liquidity_alert", {}),
            ("trades", []),
            ("utc_day", datetime.now(timezone.utc).date().isoformat()),
            ("day_start_equity", START_BALANCE),
        ):
            state.setdefault(key, default)

        if not isinstance(state["cash"], (int, float)):
            raise ValueError("الرصيد النقدي غير صالح")

        return state

    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"تعذر قراءة المحفظة بأمان: {exc}"
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
        "realized_pnl": None if pnl is None else round(pnl, 8),
    })
    state["trades"] = state["trades"][-500:]


def buy(state, symbol, candle, atr_value, account_equity):
    cash = float(state["cash"])
    entry = candle["close"] * (1 + SLIPPAGE_RATE)
    stop = entry - ATR_MULTIPLIER * atr_value
    risk_per_unit = entry - stop

    if entry <= 0 or atr_value <= 0 or risk_per_unit <= 0:
        return None

    qty_by_risk = account_equity * RISK_FRACTION / risk_per_unit
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
        state, symbol, "BUY", entry, qty, fee, "10m_strategy_entry"
    )

    return (
        f"🟢 PAPER BUY: {symbol}\n"
        f"الدخول: {entry:.8f}\n"
        f"الكمية: {qty:.6f}\n"
        f"وقف الخسارة: {stop:.8f}\n"
        f"الهدف: {target:.8f}"
    )


def sell(state, symbol, raw_price, reason):
    position = state["positions"].pop(symbol)

    price = float(raw_price) * (1 - SLIPPAGE_RATE)
    qty = float(position["qty"])
    proceeds = qty * price
    fee = proceeds * FEE_RATE

    state["cash"] += proceeds - fee

    pnl = (
        proceeds - fee
        - qty * float(position["entry"])
        - float(position["entry_fee"])
    )

    record_trade(
        state, symbol, "SELL", price, qty, fee, reason, pnl
    )

    return (
        f"🔴 PAPER SELL: {symbol}\n"
        f"سعر الخروج: {price:.8f}\n"
        f"النتيجة: {pnl:+.3f} USDT\n"
        f"السبب: {reason}"
    )


# ==================== استراتيجية 10 دقائق ====================

def market_signal(symbol):
    candles = get_klines(symbol, "10m")
    trend_candles = get_klines(symbol, "30m")

    closes = [c["close"] for c in candles]
    trend_closes = [c["close"] for c in trend_candles]

    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    trend_ema200 = ema(trend_closes, 200)

    rsi_value = rsi(closes, 14)
    atr_value = atr(candles, 14)

    if (
        rsi_value is None
        or atr_value is None
        or not trend_ema200
        or trend_ema200[-1] is None
    ):
        raise RuntimeError(f"المؤشرات غير جاهزة: {symbol}")

    last = candles[-1]
    previous = candles[-2]

    # الحركة من 6 شموع إلى 24 شمعة: ساعة وأربع ساعات تقريبًا.
    drop_1h_pct = (last["close"] / candles[-7]["close"] - 1) * 100
    drop_4h_pct = (last["close"] / candles[-25]["close"] - 1) * 100

    # متوسط حجم التداول لعشرين شمعة سابقة.
    previous_volumes = [
        c["quote_volume"] for c in candles[-21:-1]
    ]
    avg_volume = (
        sum(previous_volumes) / len(previous_volumes)
        if previous_volumes else 0
    )

    volume_multiple = (
        last["quote_volume"] / avg_volume if avg_volume > 0 else 0
    )

    taker_buy_ratio = (
        last["taker_buy_quote_volume"] / last["quote_volume"]
        if last["quote_volume"] > 0 else 0
    )

    # شروط ارتداد بعد هبوط.
    price_dropped = drop_1h_pct <= -2 or drop_4h_pct <= -3
    bullish_candle = last["close"] > last["open"]
    breaks_previous_high = last["close"] > previous["high"]
    volume_surge = volume_multiple >= 1.5
    buying_pressure = taker_buy_ratio >= 0.52

    # تأكيد الاتجاه على إطار 30 دقيقة.
    trend_ok = trend_closes[-1] > trend_ema200[-1]

    buy_signal = (
        price_dropped
        and bullish_candle
        and breaks_previous_high
        and volume_surge
        and buying_pressure
        and trend_ok
        and 40 <= rsi_value <= 68
    )

    exit_signal = (
        ema20[-2] >= ema50[-2]
        and ema20[-1] < ema50[-1]
    )

    liquidity_alert = (
        price_dropped
        and bullish_candle
        and volume_surge
        and buying_pressure
    )

    return {
        "candle": last,
        "atr": atr_value,
        "buy": buy_signal,
        "exit": exit_signal,
        "drop_1h_pct": drop_1h_pct,
        "drop_4h_pct": drop_4h_pct,
        "volume_multiple": volume_multiple,
        "taker_buy_ratio": taker_buy_ratio,
        "liquidity_inflow": liquidity_alert,
    }


# ==================== Telegram ====================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.info("إعدادات Telegram غير موجودة؛ تم تخطي الرسالة.")
        return

    response = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
        },
        timeout=15,
    )
    response.raise_for_status()

    if not response.json().get("ok"):
        raise RuntimeError("فشل إرسال رسالة Telegram")


# ==================== تشغيل البوت ====================

def run():
    state = load_state()

    try:
        send_telegram(
            "🚀 بدأ فحص بوت التداول الوهمي.\n"
            "الاستراتيجية: 10 دقائق + تأكيد 30 دقيقة."
        )
    except Exception:
        logging.exception("تعذر إرسال إشعار البدء.")

    top_symbols = get_top_symbols(TOP_SYMBOLS_LIMIT)
    symbols = list(dict.fromkeys(
        top_symbols + list(state["positions"].keys())
    ))

    signals = {}

    # الفحص المتوازي لتقليل زمن فحص العملات.
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(market_signal, symbol): symbol
            for symbol in symbols
        }

        for future in as_completed(futures):
            symbol = futures[future]
            try:
                signals[symbol] = future.result()
            except Exception:
                logging.exception("تعذر فحص %s", symbol)

    if not signals:
        raise RuntimeError("فشل فحص جميع العملات.")

    prices = {
        symbol: data["candle"]["close"]
        for symbol, data in signals.items()
    }

    today = datetime.now(timezone.utc).date().isoformat()
    current_equity = equity(state, prices)

    if state.get("utc_day") != today:
        state["utc_day"] = today
        state["day_start_equity"] = current_equity

    messages = []

    for symbol in symbols:
        if symbol not in signals:
            continue

        signal = signals[symbol]
        candle = signal["candle"]
        candle_key = str(candle["time"])

        # تنبيه السيولة مرة واحدة لكل شمعة 10 دقائق.
        if (
            signal["liquidity_inflow"]
            and state["last_liquidity_alert"].get(symbol) != candle_key
        ):
            messages.append(
                f"💧 احتمال نشاط شرائي: {symbol}\n"
                f"هبوط ساعة: {signal['drop_1h_pct']:.2f}%\n"
                f"هبوط 4 ساعات: {signal['drop_4h_pct']:.2f}%\n"
                f"حجم التداول: {signal['volume_multiple']:.2f}x\n"
                f"شراء السوق: {signal['taker_buy_ratio'] * 100:.1f}%\n"
                f"السعر: {candle['close']:.8f} USDT\n"
                "⚠️ تنبيه مراقبة وليس ضمانًا للارتداد."
            )
            state["last_liquidity_alert"][symbol] = candle_key

        # لا تكرر القرارات على الشمعة نفسها.
        if state["last_candle"].get(symbol) == candle_key:
            continue

        position = state["positions"].get(symbol)

        if position:
            # إذا لمس الوقف والهدف داخل الشمعة نفسها، نحتسب الوقف أولًا.
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
                    sell(state, symbol, candle["close"], "EMA_cross_down")
                )

        else:
            # إعادة حساب حد الخسارة قبل كل دخول.
            current_equity = equity(state, prices)
            day_start = float(state.get("day_start_equity", START_BALANCE))
            loss_paused = (
                current_equity <= day_start * (1 - DAILY_LOSS_LIMIT)
            )

            if not loss_paused and signal["buy"]:
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
    day_start = float(state.get("day_start_equity", START_BALANCE))
    loss_paused = (
        current_equity <= day_start * (1 - DAILY_LOSS_LIMIT)
    )

    summary = (
        "📊 ملخص التداول الوهمي\n"
        f"النقد: {state['cash']:.2f} USDT\n"
        f"قيمة المحفظة: {current_equity:.2f} USDT\n"
        f"الصفقات المفتوحة: {len(state['positions'])}\n"
        f"العملات التي تم فحصها: {len(signals)}\n"
        f"إيقاف الدخول بسبب حد الخسارة: {'نعم' if loss_paused else 'لا'}\n"
        "تنبيه: لا توجد أوامر تداول حقيقية."
    )

    save_state(state)

    logging.info("%s", summary.replace("\n", " | "))

    try:
        if messages:
            send_telegram("\n\n".join(messages + [summary]))
        else:
            send_telegram(summary)
    except Exception:
        logging.exception("تعذر إرسال ملخص Telegram.")


if __name__ == "__main__":
    run()
