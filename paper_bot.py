
def get_10m_klines(symbol, limit=260):
    """Build 10-minute candles from Binance 5-minute candles."""
    raw = get_klines(symbol, "5m", limit=limit * 2 + 10)

    candles = []
    buckets = {}

    for c in raw:
        # Align candles to 10-minute UTC boundaries.
        bucket_time = (c["time"] // 600000) * 600000
        buckets.setdefault(bucket_time, []).append(c)

    for bucket_time in sorted(buckets):
        group = buckets[bucket_time]

        # A complete 10-minute candle must contain both 5m candles.
        if len(group) != 2:
            continue

        group.sort(key=lambda x: x["time"])

        if group[1]["time"] - group[0]["time"] != 300000:
            continue

        first, second = group

        candles.append({
            "time": bucket_time,
            "open": first["open"],
            "high": max(first["high"], second["high"]),
            "low": min(first["low"], second["low"]),
            "close": second["close"],
            "quote_volume": (
                first["quote_volume"] + second["quote_volume"]
            ),
            "taker_buy_quote_volume": (
                first["taker_buy_quote_volume"]
                + second["taker_buy_quote_volume"]
            ),
        })

    # The final 10-minute candle may still be forming.
    if candles:
        now_ms = int(__import__("time").time() * 1000)
        if candles[-1]["time"] + 600000 > now_ms:
            candles.pop()

    if len(candles) < 205:
        raise RuntimeError(
            f"بيانات 10m غير كافية لـ {symbol}: {len(candles)} شمعة"
        )

    return candles[-limit:]
