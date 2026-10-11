import os
import requests

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"

WINDOW_LABELS = {
    "day": "۲۴ ساعت گذشته",
    "week": "۷ روز گذشته",
    "month": "۳۰ روز گذشته",
    "allTime": "کل دوره",
}

LIMIT = 100
MIN_ACCOUNT_VALUE = 100  # حداقل موجودی ۱۰۰ دلار


def fetch_rows():
    try:
        r = requests.get(LEADERBOARD_URL, timeout=60)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"خطا در دریافت داده: {e}")
        return []

    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return (
            data.get("leaderboardRows")
            or data.get("leaderboard")
            or data.get("rows")
            or []
        )
    return []


def get_perf(row, window, key):
    perfs = row.get("windowPerformances") or []
    for perf in perfs:
        if isinstance(perf, list) and len(perf) == 2 and perf[0] == window:
            try:
                return float(perf[1].get(key, 0) or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def safe_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def top_traders_by_roi(rows, window, limit=LIMIT, min_account=MIN_ACCOUNT_VALUE):
    """۱۰۰ تریدر برتر بر اساس درصد سود (ROI)"""
    candidates = []
    for row in rows:
        acc = safe_float(row.get("accountValue", 0))
        if acc < min_account:
            continue
        roi = get_perf(row, window, "roi")
        pnl = get_perf(row, window, "pnl")
        candidates.append({
            "address": row.get("ethAddress") or row.get("address") or "?",
            "roi": roi,
            "pnl": pnl,
            "accountValue": acc,
        })

    candidates.sort(key=lambda x: x["roi"], reverse=True)
    return candidates[:limit]


def format_section(title, traders):
    if not traders:
        return f"📊 *{title}*\n⚠️ داده‌ای موجود نیست.\n\n"

    msg = f"📊 *{title}* — بر اساس درصد سود\n"
    for i, t in enumerate(traders, 1):
        addr = t["address"]
        short = f"{addr[:6]}...{addr[-4:]}" if len(addr) > 12 else addr
        roi = t["roi"]
        roi_pct = roi * 100 if abs(roi) < 10 else roi
        pnl = t["pnl"]
        msg += (
            f"{i}. {short} | "
            f"📈 {roi_pct:,.1f}% | "
            f"💰 ${pnl:,.0f}\n"
        )
    return msg + "\n"


def build_message(rows):
    parts = [f"🏆 {LIMIT} معامله‌گر برتر Hyperliquid (بر اساس درصد سود)\n\n"]
    for window in ["month", "allTime"]:
        title = WINDOW_LABELS[window]
        traders = top_traders_by_roi(rows, window, limit=LIMIT)
        parts.append(format_section(title, traders))
    return "".join(parts)


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("خطا: توکن یا Chat ID تنظیم نشده است.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    chunks = [message[i:i + 4000] for i in range(0, len(message), 4000)]
    total = len(chunks)
    for idx, chunk in enumerate(chunks, 1):
        try:
            res = requests.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk,
                "disable_web_page_preview": True,
            }, timeout=30)
            res.raise_for_status()
            print(f"✅ بخش {idx}/{total} ارسال شد.")
        except Exception as e:
            print(f"خطا در ارسال بخش {idx}: {e}")


def main():
    rows = fetch_rows()
    if not rows:
        send_telegram("⚠️ دریافت داده از Hyperliquid ناموفق بود.")
        return
    message = build_message(rows)
    send_telegram(message)


if __name__ == "__main__":
    main()
