"""
Crypto Shooting Star Scanner — LBank + CCXT
--------------------------------------------
- دریافت ۱۰ ارز برتر USDT از LBank با CCXT
- تحلیل کندل‌های ۴ ساعته
- شناسایی الگوی Shooting Star
- بک‌تست با TP=+2% و SL=-1%
- ارسال گزارش به تلگرام
"""

import os
import time
import ccxt
import requests
import pandas as pd
import numpy as np

# ---------- تنظیمات ----------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

TOP_N       = 10
INTERVAL    = "4h"
LIMIT       = 500        # LBank حداکثر ۵۰۰ کندل
TP_PERCENT  = 2.0
SL_PERCENT  = 1.0
MAX_HOLD    = 6

# ---------- اتصال به LBank ----------
exchange = ccxt.lbank({
    "enableRateLimit": True,
    "options": {"defaultType": "spot"},
})


# ---------- ۱. دریافت ۱۰ ارز برتر ----------
def get_top_symbols():
    """دریافت ۱۰ ارز برتر USDT بر اساس حجم معاملات ۲۴ ساعته از LBank."""
    try:
        exchange.load_markets()
        tickers = exchange.fetch_tickers()
    except Exception as e:
        print(f"❌ Error fetching tickers from LBank: {e}")
        return []

    usdt_pairs = []
    for sym, t in tickers.items():
        if not sym.endswith("/USDT"):
            continue
        # حذف توکن‌های اهرمی
        if any(x in sym for x in ["UP/", "DOWN/", "BULL/", "BEAR/"]):
            continue
        vol = t.get("quoteVolume")
        if vol is None or vol <= 0:
            continue
        usdt_pairs.append((sym, vol))

    usdt_pairs.sort(key=lambda x: x[1], reverse=True)
    symbols = [s for s, _ in usdt_pairs[:TOP_N]]
    print(f"✅ Top {TOP_N} symbols from LBank: {symbols}")
    return symbols


# ---------- ۲. دریافت کندل‌های ۴ ساعته ----------
def get_klines(symbol, interval="4h", limit=500):
    """دریافت کندل‌های OHLCV از LBank."""
    ohlcv = exchange.fetch_ohlcv(
        symbol,
        timeframe=interval,
        limit=min(limit, 500),   # سقف LBank
    )
    if not ohlcv:
        raise ValueError(f"No data for {symbol}")

    df = pd.DataFrame(
        ohlcv,
        columns=["open_time", "open", "high", "low", "close", "volume"]
    )
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df


# ---------- ۳. شناسایی الگوی Shooting Star ----------
def detect_shooting_star(df):
    """
    الگوی Shooting Star (نزولی):
      - سایه بالایی ≥ ۲ برابر بدنه
      - سایه پایینی ≤ ۰.۵ برابر بدنه
      - بدنه ≤ ۳۵٪ کل دامنه
      - در روند صعودی قبلی ظاهر شود
    """
    body         = (df["close"] - df["open"]).abs()
    full_range   = df["high"] - df["low"]
    upper_shadow = df["high"] - df[["open", "close"]].max(axis=1)
    lower_shadow = df[["open", "close"]].min(axis=1) - df["low"]

    safe_body  = body.replace(0, np.nan)
    safe_range = full_range.replace(0, np.nan)

    body_ratio  = body / safe_range
    upper_ratio = upper_shadow / safe_body
    lower_ratio = lower_shadow / safe_body

    # روند صعودی: close کندل قبلی بالاتر از close ۵ کندل قبل‌تر
    uptrend = df["close"].shift(1) > df["close"].shift(6)

    pattern = (
        (upper_ratio >= 2.0) &
        (lower_ratio <= 0.5) &
        (body_ratio  <= 0.35) &
        uptrend
    )
    return pattern.fillna(False)


# ---------- ۴. بک‌تست با TP/SL ----------
def backtest(df, signal_col):
    """بررسی می‌کند هر سیگنال اول به TP می‌رسد یا SL."""
    wins, losses, open_trades = 0, 0, 0
    signal_indices = df.index[df[signal_col]].tolist()

    for idx in signal_indices:
        if idx + 1 >= len(df):
            continue

        entry_price = df.loc[idx, "close"]
        tp_price = entry_price * (1 + TP_PERCENT / 100)
        sl_price = entry_price * (1 - SL_PERCENT / 100)

        hit_tp, hit_sl = False, False
        for j in range(idx + 1, min(idx + 1 + MAX_HOLD, len(df))):
            high = df.loc[j, "high"]
            low  = df.loc[j, "low"]

            # چک محافظه‌کارانه: اول SL
            if low <= sl_price:
                hit_sl = True
                break
            if high >= tp_price:
                hit_tp = True
                break

        if hit_tp:
            wins += 1
        elif hit_sl:
            losses += 1
        else:
            open_trades += 1

    total = wins + losses
    win_rate = (wins / total * 100) if total > 0 else 0.0
    return wins, losses, open_trades, total, win_rate


# ---------- ۵. ارسال به تلگرام ----------
def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram credentials missing. Skipping notification.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    max_len = 4000
    chunks = [message[i:i + max_len] for i in range(0, len(message), max_len)]

    for chunk in chunks:
        try:
            r = requests.post(
                url,
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": chunk,
                    "parse_mode": "Markdown",
                },
                timeout=30,
            )
            r.raise_for_status()
            print("✅ Telegram message sent.")
        except Exception as e:
            print(f"❌ Telegram send failed: {e}")
        time.sleep(1)


# ---------- ۶. اجرای اصلی ----------
def main():
    print("🚀 Starting LBank scanner...")
    symbols = get_top_symbols()
    if not symbols:
        send_telegram("⚠️ دریافت لیست ارزها از LBank ناموفق بود.")
        return

    report_lines = ["📊 *گزارش اسکن Shooting Star — LBank ۴ ساعته*\n"]
    all_wins = all_losses = all_total = 0

    for sym in symbols:
        try:
            df = get_klines(sym, INTERVAL, LIMIT)
            df["signal"] = detect_shooting_star(df)

            wins, losses, opens, total, win_rate = backtest(df, "signal")
            all_wins   += wins
            all_losses += losses
            all_total  += total

            signals_count = int(df["signal"].sum())
            line = (
                f"`{sym}`\n"
                f"  سیگنال: {signals_count} | "
                f"برد: {wins} | باخت: {losses} | "
                f"باز: {opens} | نرخ برد: {win_rate:.1f}%\n"
            )
            report_lines.append(line)
            print(line.replace("\n", " "))

            time.sleep(0.5)

        except Exception as e:
            err = str(e)[:80]
            report_lines.append(f"`{sym}` → ❌ خطا: {err}\n")
            print(f"❌ {sym}: {e}")

    overall_rate = (all_wins / all_total * 100) if all_total > 0 else 0.0
    summary = (
        f"\n📈 *خلاصه کل:*\n"
        f"• معاملات بسته‌شده: {all_total}\n"
        f"• برد: {all_wins} | باخت: {all_losses}\n"
        f"• *نرخ برد کلی: {overall_rate:.1f}%*\n"
        f"\n⚙️ TP=+{TP_PERCENT}% | SL=-{SL_PERCENT}% | نگه‌داری: {MAX_HOLD} کندل"
    )
    report_lines.append(summary)

    full_report = "\n".join(report_lines)
    print("\n" + "=" * 50)
    print(full_report)
    send_telegram(full_report)


if __name__ == "__main__":
    main()
