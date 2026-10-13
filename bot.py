import os
import time
import requests

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"

LIMIT = 100  # تعداد تریدرهای برتر


# ============================================================
#                         دریافت داده
# ============================================================
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


# ============================================================
#                        ابزار کمکی
# ============================================================
def get_perf(row, window, key):
    """استخراج یک متریک خاص (roi یا pnl) در یک بازه زمانی مشخص"""
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


def roi_str(roi):
    """فرمت‌دهی ROI به صورت درصد"""
    pct = roi * 100 if abs(roi) < 10 else roi
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:,.1f}%"


def pnl_str(pnl):
    """فرمت‌دهی PnL به صورت دلاری"""
    sign = "+" if pnl >= 0 else ""
    return f"{sign}${pnl:,.0f}"


# ============================================================
#                       انتخاب برترین‌ها
# ============================================================
def top_by_alltime(rows, limit=LIMIT):
    """N کیف پول برتر بر اساس ROI کل دوره"""
    candidates = []
    for row in rows:
        addr = row.get("ethAddress") or row.get("address") or "?"
        if not addr or addr == "?":
            continue

        candidates.append({
            "address": addr,
            "display_name": row.get("displayName") or "",
            "account_value": safe_float(row.get("accountValue", 0)),
            "day_roi": get_perf(row, "day", "roi"),
            "week_roi": get_perf(row, "week", "roi"),
            "month_roi": get_perf(row, "month", "roi"),
            "all_roi": get_perf(row, "allTime", "roi"),
            "day_pnl": get_perf(row, "day", "pnl"),
            "week_pnl": get_perf(row, "week", "pnl"),
            "month_pnl": get_perf(row, "month", "pnl"),
            "all_pnl": get_perf(row, "allTime", "pnl"),
        })

    candidates.sort(key=lambda x: x["all_roi"], reverse=True)
    return candidates[:limit]


# ============================================================
#                        ساخت پیام
# ============================================================
def build_message(records):
    header = (
        f"🏆 گزارش {len(records)} تریدر برتر Hyperliquid\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 معیار رتبه‌بندی: ROI کل دوره\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    lines = [header]

    for i, r in enumerate(records, 1):
        name_part = f" ({r['display_name']})" if r["display_name"] else ""
        lines.append(
            f"┏━━━━━━━━━━━━━━━━━━━━━\n"
            f"┃ #{i}{name_part}\n"
            f"┃ 🔗 {r['address']}\n"
            f"┃ 💰 موجودی: ${r['account_value']:,.0f}\n"
            f"┃\n"
            f"┃ 📅 روزانه : {roi_str(r['day_roi']):>10}  ·  {pnl_str(r['day_pnl']):>12}\n"
            f"┃ 📆 هفتگی : {roi_str(r['week_roi']):>10}  ·  {pnl_str(r['week_pnl']):>12}\n"
            f"┃ 🗓️ ماهانه : {roi_str(r['month_roi']):>10}  ·  {pnl_str(r['month_pnl']):>12}\n"
            f"┃ 🏆 کل دوره: {roi_str(r['all_roi']):>10}  ·  {pnl_str(r['all_pnl']):>12}\n"
            f"┗━━━━━━━━━━━━━━━━━━━━━\n\n"
        )
    return "".join(lines)


# ============================================================
#                       ارسال به تلگرام
# ============================================================
def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("خطا: توکن یا Chat ID تنظیم نشده است.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    # به دلیل حجم کم پیام، نیازی به تقسیم نیست
    # اما اگر پیام از 4096 کاراکتر بیشتر شد، تقسیم می‌کنیم
    if len(message) <= 4096:
        chunks = [message]
    else:
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


# ============================================================
#                          اجرای اصلی
# ============================================================
def main():
    rows = fetch_rows()
    if not rows:
        send_telegram("⚠️ دریافت داده از Hyperliquid ناموفق بود.")
        return

    top = top_by_alltime(rows)
    print(f"تعداد تریدرهای برتر انتخاب شده: {len(top)}")

    if not top:
        send_telegram("⚠️ هیچ تریدری با آدرس معتبر یافت نشد.")
        return

    message = build_message(top)
    send_telegram(message)


if __name__ == "__main__":
    main()
