import os
import requests

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"


def fetch_top_traders(limit=10, window="allTime"):
    """
    دریافت ۱۰ معامله‌گر برتر از لیدربورد عمومی Hyperliquid.
    window: "day" | "week" | "month" | "allTime"
    """
    try:
        r = requests.get(LEADERBOARD_URL, timeout=60)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"خطا در دریافت داده: {e}")
        return []

    rows = data if isinstance(data, list) else data.get("leaderboard", [])
    if not rows:
        print("داده‌ای در پاسخ وجود ندارد.")
        return []

    def get_perf(row, key):
        for perf in row.get("windowPerformances", []):
            if isinstance(perf, list) and len(perf) == 2 and perf[0] == window:
                return perf[1].get(key, 0) or 0
        return 0

    sorted_rows = sorted(rows, key=lambda r: get_perf(r, "pnl"), reverse=True)[:limit]

    result = []
    for row in sorted_rows:
        result.append({
            "address": row.get("ethAddress", "?"),
            "pnl": get_perf(row, "pnl"),
            "roi": get_perf(row, "roi"),
            "accountValue": row.get("accountValue", 0),
        })
    return result


def format_message(traders, window="allTime"):
    if not traders:
        return "⚠️ داده‌ای برای نمایش یافت نشد."

    labels = {
        "day": "۲۴ ساعت گذشته",
        "week": "هفته گذشته",
        "month": "ماه گذشته",
        "allTime": "کل دوره",
    }
    title = labels.get(window, window)

    msg = f"🏆 ۱۰ معامله‌گر برتر Hyperliquid ({title})\n\n"
    for i, t in enumerate(traders, 1):
        addr = t["address"]
        short = f"{addr[:6]}...{addr[-4:]}" if len(addr) > 12 else addr
        pnl = t["pnl"]
        roi = t["roi"]
        roi_pct = roi * 100 if abs(roi) < 10 else roi
        msg += (
            f"{i}. {short}\n"
            f"   💰 سود: ${pnl:,.0f}\n"
            f"   📈 بازدهی: {roi_pct:,.1f}%\n"
            f"   🏦 موجودی: ${t['accountValue']:,.0f}\n\n"
        )
    return msg


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("خطا: توکن یا Chat ID تنظیم نشده است.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
    }
    try:
        res = requests.post(url, json=payload, timeout=30)
        res.raise_for_status()
        print("✅ پیام با موفقیت ارسال شد.")
    except Exception as e:
        print(f"خطا در ارسال پیام: {e}")


def main():
    window = "allTime"  # می‌توانی به "day" | "week" | "month" تغییر بدهی
    traders = fetch_top_traders(limit=10, window=window)
    message = format_message(traders, window=window)
    send_telegram(message)


if __name__ == "__main__":
    main()
