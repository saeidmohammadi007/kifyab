import os
import requests

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"

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


def short_addr(addr):
    return f"{addr[:6]}...{addr[-4:]}" if len(addr) > 12 else addr


def roi_str(roi):
    pct = roi * 100 if abs(roi) < 10 else roi
    return f"{pct:,.1f}%"


def top_by_alltime(rows, limit=LIMIT, min_account=MIN_ACCOUNT_VALUE):
    """۱۰۰ کیف پول برتر بر اساس ROI کل دوره"""
    candidates = []
    for row in rows:
        acc = safe_float(row.get("accountValue", 0))
        if acc < min_account:
            continue
        addr = row.get("ethAddress") or row.get("address") or "?"
        candidates.append({
            "address": addr,
            "short": short_addr(addr),
            "month_roi": get_perf(row, "month", "roi"),
            "month_pnl": get_perf(row, "month", "pnl"),
            "all_roi": get_perf(row, "allTime", "roi"),
            "all_pnl": get_perf(row, "allTime", "pnl"),
        })

    candidates.sort(key=lambda x: x["all_roi"], reverse=True)
    return candidates[:limit]


def build_message(records):
    header = (
        "🏆 ۱۰۰ کیف پول برتر Hyperliquid (کل دوره)\n"
        "📅 ماهانه | 🏆 کل دوره\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    lines = [header]
    for i, r in enumerate(records, 1):
        lines.append(
            f"{i}. {r['short']}\n"
            f"   📅 {roi_str(r['month_roi'])} (${r['month_pnl']:,.0f})\n"
            f"   🏆 {roi_str(r['all_roi'])} (${r['all_pnl']:,.0f})\n"
        )
    return "".join(lines)


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
    top = top_by_alltime(rows)
    print(f"تعداد کیف پول در گزارش: {len(top)}")
    message = build_message(top)
    send_telegram(message)


if __name__ == "__main__":
    main()
