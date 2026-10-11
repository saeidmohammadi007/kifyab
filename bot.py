import os
import json
import requests

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"


def fetch_top_traders(limit=10, window="allTime", debug=False):
    try:
        r = requests.get(LEADERBOARD_URL, timeout=60)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"خطا در دریافت داده: {e}")
        return []

    # چند کلید ممکن را امتحان کن
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = (
            data.get("leaderboardRows")
            or data.get("leaderboard")
            or data.get("rows")
            or []
        )
    else:
        rows = []

    if debug:
        print("--- نوع داده:", type(data).__name__)
        if isinstance(data, dict):
            print("--- کلیدها:", list(data.keys()))
        print("--- تعداد ردیف:", len(rows))
        if rows:
            print("--- نمونه ردیف اول:")
            print(json.dumps(rows[0], indent=2, ensure_ascii=False)[:1500])

    if not rows:
        print("داده‌ای در پاسخ وجود ندارد.")
        return []

    def get_perf(row, key):
        perfs = row.get("windowPerformances") or []
        for perf in perfs:
            if isinstance(perf, list) and len(perf) == 2 and perf[0] == window:
                val = perf[1].get(key, 0)
                try:
                    return float(val)
                except (TypeError, ValueError):
                    return 0.0
        return 0.0

    def safe_float(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    sorted_rows = sorted(rows, key=lambda r: get_perf(r, "pnl"), reverse=True)[:limit]

    result = []
    for row in sorted_rows:
        result.append({
            "address": row.get("ethAddress") or row.get("address") or "?",
            "pnl": get_perf(row, "pnl"),
            "roi": get_perf(row, "roi"),
            "accountValue": safe_float(row.get("accountValue", 0)),
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
    try:
        res = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "disable_web_page_preview": True,
        }, timeout=30)
        res.raise_for_status()
        print("✅ پیام با موفقیت ارسال شد.")
    except Exception as e:
        print(f"خطا در ارسال پیام: {e}")


def main():
    window = "allTime"  # "day" | "week" | "month" | "allTime"
    traders = fetch_top_traders(limit=10, window=window, debug=True)
    message = format_message(traders, window=window)
    send_telegram(message)


if __name__ == "__main__":
    main()
