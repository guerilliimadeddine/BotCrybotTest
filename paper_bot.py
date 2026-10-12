
#!/usr/bin/env python3
# تداول ورقي فقط: لا ينفذ أي أوامر شراء أو بيع حقيقية.

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_URLS = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
]

STATE_FILE = Path("paper_state.json")

INITIAL_CASH = 100.0
FEE_RATE = 0.001
SLIPPAGE_RATE = 0.0005
RISK_PER_TRADE = 0.005
MAX_DAILY_LOSS = 0.015
MAX_OPEN_POSITIONS = 5
MAX_PAIRS = 100
MIN_QUOTE_VOLUME_USDT = 1_000_000
HTTP_TIMEOUT = 12

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("paper-bot")

session = requests.Session()
session.headers.update({"User-Agent": "PaperTradingBot/1.0"})


def utc_now():
    return datetime.now(timezone.utc)


def utc_day():
    return utc_now().strftime("%Y-%m-%d")


def request_json(path, params=None):
    errors = []

    for base in BASE_URLS:
        try:
            response = session.get(
                base + path,
                params=params,
                timeout=HTTP_TIMEOUT,
            )

            if response.status_code == 451:
                errors.append(
                    f"{base}: HTTP 451 - الخدمة غير متاحة"
                )
                continue

            response.raise_for_status()
            return response.json()

        except Exception as exc:
            errors.append(f"{base}: {exc}")

    raise RuntimeError(
        "تعذّر جلب بيانات السوق: " + " | ".join(errors)
    )


def get_klines(symbol, interval, limit=260):
    """جلب الشموع المغلقة فقط."""
    raw = request_json(
        "/api/v3/klines",
        {
            "symbol": symbol,
            "interval": interval,
            "limit": min(int(limit), 1000),
        },
    )

    now_ms = int(time.time() * 1000)
    candles = []

    for k in raw:
        open_time = int(k[0])
        close_time = int(k[6])

        if close_time >= now_ms:
            continue

        candles.append({
            "time": open_time,
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
            "quote_volume": float(k[7]),
            "taker_buy_quote_volume": float(k[10]),
        })

    return candles


def get_10m_klines(symbol, limit=260):
    """
    بناء شموع 10 دقائق من شمعتين متتاليتين مدة كل منهما 5 دقائق.
    """
    raw = get_klines(
        symbol,
        "5m",
        limit=min(limit * 2 + 10, 1000),
    )

    buckets = {}

    for candle in raw:
        bucket_time = (
            candle["time"] // 600_000
        ) * 600_000

        buckets.setdefault(bucket_time, []).append(candle)

    result = []

    for bucket_time in sorted(buckets):
        group = sorted(
            buckets[bucket_time],
            key=lambda c: c["time"],
        )

        if len(group) != 2:
            continue

        if group[1]["time"] - group[0]["time"] != 300_000:
            continue

        first, second = group

        result.append({
            "time": bucket_time,
            "open": first["open"],
            "high": max(first["high"], second["high"]),
            "low": min(first["low"], second["low"]),
            "close": second["close"],
            "volume": first["volume"] + second["volume"],
            "quote_volume": (
                first["quote_volume"]
                + second["quote_volume"]
            ),
            "taker_buy_quote_volume": (
                first["taker_buy_quote_volume"]
                + second["taker_buy_quote_volume"]
            ),
        })

    return result[-limit:]


def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2.0 / (period + 1)
    current = sum(values[:period]) / period

    for value in values[period:]:
        current = (
            value * multiplier
            + current * (1 - multiplier)
        )

    return current


def rsi(values, period=14):
    if len(values) < period + 1:
        return None

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(changes)):
        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None

    true_ranges = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]

        true_ranges.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"]),
        ))

    if len(true_ranges) < period:
        return None

    value = sum(true_ranges[:period]) / period

    for tr in true_ranges[period:]:
        value = (
            value * (period - 1) + tr
        ) / period

    return value


def default_state():
    return {
        "cash": INITIAL_CASH,
        "positions": {},
        "trades": [],
        "day": utc_day(),
        "day_start_equity": INITIAL_CASH,
        "last_scan": None,
    }


def load_state():
    if not STATE_FILE.exists():
        return default_state()

    try:
        state = json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )

        state.setdefault("cash", INITIAL_CASH)
        state.setdefault("positions", {})
        state.setdefault("trades", [])
        state.setdefault("day", utc_day())
        state.setdefault(
            "day_start_equity",
            state["cash"],
        )
        state.setdefault("last_scan", None)

        if state["day"] != utc_day():
            state["day"] = utc_day()
            state["day_start_equity"] = (
                state["cash"]
                + sum(
                    p["qty"] * p.get(
                        "last_price",
                        p["entry_price"],
                    )
                    for p in state["positions"].values()
                )
            )

        return state

    except Exception as exc:
        log.exception("تعذّرت قراءة ملف الحالة: %s", exc)
        raise RuntimeError(
            "ملف paper_state.json تالف. خذ نسخة احتياطية منه."
        ) from exc


def save_state(state):
    temp = STATE_FILE.with_suffix(".tmp")

    temp.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    temp.replace(STATE_FILE)


def get_top_usdt_pairs():
    tickers = request_json("/api/v3/ticker/24hr")
    exchange = request_json("/api/v3/exchangeInfo")

    allowed = {
        item["symbol"]
        for item in exchange.get("symbols", [])
        if item.get("status") == "TRADING"
        and item.get("quoteAsset") == "USDT"
        and item.get("isSpotTradingAllowed", True)
    }

    filtered = []

    for ticker in tickers:
        symbol = ticker.get("symbol", "")

        if symbol not in allowed or not symbol.endswith("USDT"):
            continue

        base = symbol[:-4]

        if any(
            base.endswith(suffix)
            for suffix in ("UP", "DOWN", "BULL", "BEAR")
        ):
            continue

        try:
            quote_volume = float(ticker.get("quoteVolume", 0))
            price = float(ticker.get("lastPrice", 0))
        except (TypeError, ValueError):
            continue

        if (
            quote_volume >= MIN_QUOTE_VOLUME_USDT
            and price > 0
        ):
            filtered.append((symbol, quote_volume))

    filtered.sort(key=lambda item: item[1], reverse=True)

    return [
        symbol
        for symbol, _ in filtered[:MAX_PAIRS]
    ]


def market_signal(symbol):
    """إرجاع إشارة دخول أو None إذا لم تتحقق الشروط."""
    candles = get_10m_klines(symbol, 260)
    trend_candles = get_klines(symbol, "30m", 230)

    if len(candles) < 205 or len(trend_candles) < 205:
        return None

    closes = [c["close"] for c in candles]
    trend_closes = [c["close"] for c in trend_candles]

    trend_ema200 = ema(trend_closes, 200)
    current_rsi = rsi(closes, 14)
    current_atr = atr(candles, 14)

    if (
        trend_ema200 is None
        or current_rsi is None
        or current_atr is None
        or current_atr <= 0
    ):
        return None

    last = candles[-1]
    previous = candles[-2]

    close_1h_ago = candles[-7]["close"]
    close_4h_ago = candles[-25]["close"]

    drop_1h = last["close"] / close_1h_ago - 1
    drop_4h = last["close"] / close_4h_ago - 1

    recent_volumes = [
        candle["quote_volume"]
        for candle in candles[-21:-1]
    ]

    average_volume = (
        sum(recent_volumes) / len(recent_volumes)
        if recent_volumes else 0
    )

    buy_ratio = (
        last["taker_buy_quote_volume"]
        / last["quote_volume"]
        if last["quote_volume"] > 0 else 0
    )

    dip_detected = (
        drop_1h <= -0.02
        or drop_4h <= -0.03
    )

    bullish_reclaim = (
        last["close"] > last["open"]
        and last["close"] > previous["high"]
    )

    volume_confirmed = (
        average_volume > 0
        and last["quote_volume"] >= 1.5 * average_volume
    )

    trend_confirmed = trend_closes[-1] > trend_ema200
    rsi_ok = 40 <= current_rsi <= 68
    buy_pressure = buy_ratio >= 0.52

    if all([
        dip_detected,
        bullish_reclaim,
        volume_confirmed,
        trend_confirmed,
        rsi_ok,
        buy_pressure,
    ]):
        return {
            "symbol": symbol,
            "price": last["close"],
            "atr": current_atr,
            "rsi": current_rsi,
            "drop_1h_pct": drop_1h * 100,
            "drop_4h_pct": drop_4h * 100,
            "volume_ratio": (
                last["quote_volume"] / average_volume
            ),
            "buy_ratio_pct": buy_ratio * 100,
        }

    return None


def equity(state):
    position_value = sum(
        position["qty"] * position.get(
            "last_price",
            position["entry_price"],
        )
        for position in state["positions"].values()
    )

    return state["cash"] + position_value


def mark_positions(state):
    for symbol, position in list(state["positions"].items()):
        try:
            candles = get_klines(symbol, "5m", 3)

            if candles:
                position["last_price"] = candles[-1]["close"]

        except Exception as exc:
            log.warning(
                "%s: تعذّر تحديث السعر: %s",
                symbol,
                exc,
            )


def telegram(message):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        log.info("Telegram غير مهيأ؛ تم تخطي الإرسال.")
        return

    try:
        response = session.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={
                "chat_id": chat_id,
                "text": message,
            },
            timeout=HTTP_TIMEOUT,
        )

        response.raise_for_status()

    except Exception as exc:
        log.warning("فشل إرسال Telegram: %s", exc)


def close_position(state, symbol, price, reason):
    position = state["positions"].pop(symbol)

    exit_price = price * (1 - SLIPPAGE_RATE)
    gross = position["qty"] * exit_price
    fee = gross * FEE_RATE
    net_proceeds = gross - fee

    state["cash"] += net_proceeds
    pnl = net_proceeds - position["cost_basis"]

    state["trades"].append({
        "symbol": symbol,
        "entry_price": position["entry_price"],
        "exit_price": exit_price,
        "qty": position["qty"],
        "pnl": pnl,
        "reason": reason,
        "closed_at": utc_now().isoformat(),
    })

    message = (
        f"📤 إغلاق افتراضي: {symbol}\n"
        f"السبب: {reason}\n"
        f"الدخول: {position['entry_price']:.8g}\n"
        f"الخروج: {exit_price:.8g}\n"
        f"الربح/الخسارة بعد الرسوم: {pnl:.4f} USDT\n"
        f"الرصيد النقدي: {state['cash']:.2f} USDT"
    )

    log.info(message.replace("\n", " | "))
    telegram(message)


def manage_positions(state):
    for symbol in list(state["positions"]):
        position = state["positions"][symbol]

        try:
            candles = get_10m_klines(symbol, 60)

            if len(candles) < 50:
                continue

            price = candles[-1]["close"]
            position["last_price"] = price

            closes = [c["close"] for c in candles]
            ema20 = ema(closes, 20)
            ema50 = ema(closes, 50)

            if price <= position["stop_price"]:
                close_position(
                    state, symbol, price, "وقف الخسارة"
                )

            elif price >= position["target_price"]:
                close_position(
                    state, symbol, price, "هدف الربح"
                )

            elif (
                ema20 is not None
                and ema50 is not None
                and ema20 < ema50
            ):
                close_position(
                    state,
                    symbol,
                    price,
                    "تقاطع EMA20 تحت EMA50",
                )

        except Exception as exc:
            log.warning(
                "%s: تعذّرت إدارة الصفقة: %s",
                symbol,
                exc,
            )


def open_position(state, signal):
    symbol = signal["symbol"]

    if symbol in state["positions"]:
        return False

    if len(state["positions"]) >= MAX_OPEN_POSITIONS:
        return False

    current_equity = equity(state)
    maximum_loss = current_equity * RISK_PER_TRADE

    entry = signal["price"] * (1 + SLIPPAGE_RATE)
    stop = entry - 1.5 * signal["atr"]

    if stop <= 0 or stop >= entry:
        return False

    risk_per_unit = entry - stop
    quantity = maximum_loss / risk_per_unit

    max_notional = min(
        current_equity * 0.20,
        state["cash"] / (1 + FEE_RATE),
    )

    quantity = min(quantity, max_notional / entry)

    if quantity <= 0:
        return False

    notional = quantity * entry
    fee = notional * FEE_RATE
    cost_basis = notional + fee

    if cost_basis > state["cash"]:
        return False

    state["cash"] -= cost_basis

    target = entry + 2 * risk_per_unit

    state["positions"][symbol] = {
        "entry_price": entry,
        "qty": quantity,
        "stop_price": stop,
        "target_price": target,
        "cost_basis": cost_basis,
        "last_price": signal["price"],
        "opened_at": utc_now().isoformat(),
        "atr": signal["atr"],
    }

    message = (
        f"📥 شراء افتراضي: {symbol}\n"
        f"سعر الدخول: {entry:.8g}\n"
        f"الكمية: {quantity:.8g}\n"
        f"وقف الخسارة: {stop:.8g}\n"
        f"الهدف: {target:.8g}\n"
        f"RSI: {signal['rsi']:.1f}\n"
        f"ضغط الشراء: {signal['buy_ratio_pct']:.1f}%\n"
        f"الرصيد النقدي المتبقي: {state['cash']:.2f} USDT"
    )

    log.info(message.replace("\n", " | "))
    telegram(message)

    return True


def main():
    state = load_state()

    if state["day"] != utc_day():
        state["day"] = utc_day()
        state["day_start_equity"] = equity(state)

    log.info(
        "بدء الفحص: تداول ورقي فقط، دون أوامر حقيقية."
    )

    try:
        mark_positions(state)
        manage_positions(state)

        current_equity = equity(state)
        day_start = max(
            float(state.get("day_start_equity", INITIAL_CASH)),
            0.01,
        )

        daily_loss = (
            day_start - current_equity
        ) / day_start

        if daily_loss >= MAX_DAILY_LOSS:
            message = (
                "⛔ توقف فتح صفقات جديدة بسبب حد الخسارة اليومية.\n"
                f"الخسارة اليومية: {daily_loss * 100:.2f}%"
            )

            log.warning(message.replace("\n", " | "))
            telegram(message)

        else:
            symbols = get_top_usdt_pairs()

            log.info(
                "تم اختيار %d زوج USDT.",
                len(symbols),
            )

            for index, symbol in enumerate(symbols, 1):
                if len(state["positions"]) >= MAX_OPEN_POSITIONS:
                    break

                if symbol in state["positions"]:
                    continue

                try:
                    signal = market_signal(symbol)

                    if signal:
                        log.info(
                            "إشارة %s: RSI=%.1f volume=%.2fx",
                            symbol,
                            signal["rsi"],
                            signal["volume_ratio"],
                        )

                        open_position(state, signal)

                except Exception as exc:
                    log.warning(
                        "%s: فشل تحليل الزوج: %s",
                        symbol,
                        exc,
                    )

                if index % 10 == 0:
                    log.info(
                        "تم فحص %d/%d زوج.",
                        index,
                        len(symbols),
                    )

        state["last_scan"] = utc_now().isoformat()

        mark_positions(state)
        save_state(state)

        log.info(
            "انتهى الفحص | النقد=%.2f USDT | "
            "قيمة الحساب=%.2f USDT | صفقات مفتوحة=%d",
            state["cash"],
            equity(state),
            len(state["positions"]),
        )

    except Exception:
        save_state(state)
        log.exception("فشل التشغيل")
        raise


if __name__ == "__main__":
    main()
