import os
import time
import calendar
from datetime import datetime, timedelta
import requests

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
INFO_URL = "https://api.hyperliquid.xyz/info"

# ------------------ تنظیمات اصلی ------------------
LIMIT = 20                       # تعداد تریدر برتر نمایش داده‌شده
MIN_ACCOUNT_VALUE = 1000         # حداقل موجودی دلاری
MIN_WEEK_VLM = 1000              # حداقل حجم معاملات هفته اخیر
MIN_AGE_DAYS = 365               # حداقل سابقه (یک سال)
MIN_PASSING_TO_SHOW = 5          # حداقل ماه موفق برای نمایش در گزارش

MONTHLY_ROI_THRESHOLD = 0.05     # آستانه ROI هر ماه (۵٪)
MONTHS_TO_CHECK = 12             # تعداد ماه‌های اخیر برای بررسی

# ------------------ تنظیمات عملکرد ------------------
TOP_BY_ALLTIME = 500             # تعداد کاندیدای اولیه برای بررسی عمیق
SLEEP_BETWEEN_REQUESTS = 0.4     # مکث بین درخواست‌ها


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


def fetch_portfolio(address):
    try:
        res = requests.post(INFO_URL, json={
            "type": "portfolio",
            "user": address,
        }, timeout=15)
        res.raise_for_status()
        return res.json()
    except Exception as e:
        print(f"   خطا در portfolio {address}: {e}")
        return None


def fetch_fills_by_time(address, start_ms, end_ms):
    try:
        res = requests.post(INFO_URL, json={
            "type": "userFillsByTime",
            "user": address,
            "startTime": start_ms,
            "endTime": end_ms,
            "aggregateByTime": False,
        }, timeout=20)
        res.raise_for_status()
        data = res.json()
        if not isinstance(data, list):
            return None
        return data
    except Exception as e:
        print(f"   خطا در fills {address}: {e}")
        return None


# ============================================================
#                        ابزار کمکی
# ============================================================
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


def roi_str(roi):
    pct = roi * 100 if abs(roi) < 10 else roi
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:,.1f}%"


def pnl_str(pnl):
    sign = "+" if pnl >= 0 else ""
    return f"{sign}${pnl:,.0f}"


# ============================================================
#                    فیلترهای سبک
# ============================================================
def passes_light_filters(row):
    acc = safe_float(row.get("accountValue", 0))
    if acc < MIN_ACCOUNT_VALUE:
        return False

    week_vlm = get_perf(row, "week", "vlm")
    month_vlm = get_perf(row, "month", "vlm")
    if week_vlm < MIN_WEEK_VLM and month_vlm < MIN_WEEK_VLM:
        return False

    return True


# ============================================================
#                    فیلترهای سنگین
# ============================================================
def get_account_age_days(address, portfolio_data=None):
    if portfolio_data is None:
        portfolio_data = fetch_portfolio(address)

    if not portfolio_data:
        return 0

    pnl_history = None

    if isinstance(portfolio_data, dict):
        pnl_history = portfolio_data.get("pnlHistory")

    elif isinstance(portfolio_data, list):
        if (portfolio_data
                and isinstance(portfolio_data[0], list)
                and len(portfolio_data[0]) == 2
                and isinstance(portfolio_data[0][0], (int, float))):
            pnl_history = portfolio_data
        else:
            for item in portfolio_data:
                if not isinstance(item, list) or len(item) != 2:
                    continue
                inner = item[1]
                if isinstance(inner, dict):
                    candidate = inner.get("pnlHistory")
                    if candidate:
                        pnl_history = candidate
                        break

    if not pnl_history:
        return 0

    first_activity_ms = None
    try:
        first = pnl_history[0]
        if isinstance(first, list) and len(first) >= 1:
            first_activity_ms = first[0]
        elif isinstance(first, (int, float)):
            first_activity_ms = first
    except (IndexError, TypeError):
        return 0

    if not isinstance(first_activity_ms, (int, float)):
        return 0

    age_ms = time.time() * 1000 - first_activity_ms
    return int(age_ms / (1000 * 60 * 60 * 24))


def compute_monthly_rois(address, current_account_value):
    """
    محاسبه ROI هر یک از MONTHS_TO_CHECK ماه اخیر.
    خروجی: (لیست ROIها، تعداد ماه‌های ≥ آستانه)
    """
    now = datetime.utcnow()
    monthly_rois = []

    if current_account_value <= 0:
        return [], 0

    for i in range(MONTHS_TO_CHECK):
        target = now - timedelta(days=30 * i)
        start = target.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        last_day = calendar.monthrange(target.year, target.month)[1]
        end = target.replace(
            day=last_day, hour=23, minute=59, second=59, microsecond=0
        )

        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)

        fills = fetch_fills_by_time(address, start_ms, end_ms)
        time.sleep(SLEEP_BETWEEN_REQUESTS)

        if fills is None:
            monthly_rois.append(0.0)
            continue

        monthly_pnl = 0.0
        for f in fills:
            if isinstance(f, dict):
                monthly_pnl += safe_float(f.get("closedPnl", 0))

        monthly_rois.append(monthly_pnl / current_account_value)

    passing = sum(1 for r in monthly_rois if r >= MONTHLY_ROI_THRESHOLD)
    return monthly_rois, passing


# ============================================================
#                       انتخاب برترین‌ها
# ============================================================
def build_candidates(rows):
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
            "week_vlm": get_perf(row, "week", "vlm"),
            "month_vlm": get_perf(row, "month", "vlm"),
        })

    candidates = [c for c in candidates if c["all_roi"] > 0]
    candidates.sort(key=lambda x: x["all_roi"], reverse=True)
    return candidates


def select_top_traders(rows):
    """
    همه‌ی کاندیداهای واجد شرایط را بررسی می‌کند،
    سپس بر اساس تعداد ماه‌های موفق (ROI ≥ آستانه) رتبه‌بندی می‌کند.
    """
    candidates = build_candidates(rows)
    print(f"تعداد کل کاندیداها (ROI مثبت): {len(candidates)}")

    # فیلتر سبک
    pre_filtered = []
    for c in candidates[:TOP_BY_ALLTIME]:
        row = {
            "accountValue": c["account_value"],
            "windowPerformances": [
                ["week", {"vlm": c["week_vlm"]}],
                ["month", {"vlm": c["month_vlm"]}],
            ],
        }
        if passes_light_filters(row):
            pre_filtered.append(c)

    print(f"پس از فیلترهای سبک: {len(pre_filtered)}")

    # بررسی عمیق
    analyzed = []
    for i, c in enumerate(pre_filtered, 1):
        addr = c["address"]
        print(f"[{i}/{len(pre_filtered)}] بررسی عمیق: {addr}")

        try:
            portfolio = fetch_portfolio(addr)
            time.sleep(SLEEP_BETWEEN_REQUESTS)

            age_days = get_account_age_days(addr, portfolio)
            if age_days < MIN_AGE_DAYS:
                print(f"   ✗ سن حساب کم: {age_days} روز")
                continue

            monthly_rois, passing = compute_monthly_rois(addr, c["account_value"])

            c["age_days"] = age_days
            c["monthly_rois"] = monthly_rois
            c["passing_months"] = passing
            analyzed.append(c)
            print(f"   ✓ ماه‌های موفق: {passing}/{MONTHS_TO_CHECK}")

        except Exception as e:
            print(f"   ✗ خطای نامشخص برای {addr}: {e}")
            continue

    # مرتب‌سازی بر اساس تعداد ماه‌های موفق (نزولی)
    # در صورت تساوی، ROI کل‌دوره بالاتر اولویت دارد
    analyzed.sort(key=lambda x: (x["passing_months"], x["all_roi"]), reverse=True)

    # فقط آن‌هایی که حداقل MIN_PASSING_TO_SHOW ماه موفق داشتند
    filtered = [c for c in analyzed if c["passing_months"] >= MIN_PASSING_TO_SHOW]

    print(f"تعداد تریدرهای با ≥ {MIN_PASSING_TO_SHOW} ماه موفق: {len(filtered)}")

    return filtered[:LIMIT]


# ============================================================
#                        ساخت پیام
# ============================================================
def build_message(records):
    header = (
        f"🏆 {len(records)} تریدر برتر Hyperliquid\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 معیار رتبه‌بندی: تعداد ماه‌های با ROI ≥ "
        f"{MONTHLY_ROI_THRESHOLD*100:.0f}% (از {MONTHS_TO_CHECK} ماه اخیر)\n"
        f"✅ فیلترها:\n"
        f"   • موجودی ≥ ${MIN_ACCOUNT_VALUE:,}\n"
        f"   • حجم معاملات هفته ≥ ${MIN_WEEK_VLM:,}\n"
        f"   • سابقه ≥ {MIN_AGE_DAYS} روز\n"
        f"   • حداقل {MIN_PASSING_TO_SHOW} ماه موفق برای نمایش\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    lines = [header]

    for i, r in enumerate(records, 1):
        name_part = f" ({r['display_name']})" if r["display_name"] else ""
        monthly_str = " · ".join(f"{x*100:+.1f}%" for x in r["monthly_rois"])

        # مدال برای رتبه‌های برتر
        medal = ""
        if i == 1:
            medal = "🥇 "
        elif i == 2:
            medal = "🥈 "
        elif i == 3:
            medal = "🥉 "

        lines.append(
            f"┏━━━━━━━━━━━━━━━━━━━━━\n"
            f"┃ {medal}#{i}{name_part}\n"
            f"┃ 🔗 {r['address']}\n"
            f"┃ 💰 موجودی: ${r['account_value']:,.0f}  |  🎂 {r['age_days']} روز\n"
            f"┃\n"
            f"┃ 🎯 ماه‌های موفق: {r['passing_months']}/{MONTHS_TO_CHECK}\n"
            f"┃\n"
            f"┃ 📅 روزانه : {roi_str(r['day_roi']):>10} · {pnl_str(r['day_pnl']):>12}\n"
            f"┃ 📆 هفتگی : {roi_str(r['week_roi']):>10} · {pnl_str(r['week_pnl']):>12}\n"
            f"┃ 🗓️ ماهانه : {roi_str(r['month_roi']):>10} · {pnl_str(r['month_pnl']):>12}\n"
            f"┃ 🏆 کل دوره: {roi_str(r['all_roi']):>10} · {pnl_str(r['all_pnl']):>12}\n"
            f"┃\n"
            f"┃ 📊 ROI ماهانه (جدید→قدیم):\n"
            f"┃    {monthly_str}\n"
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

    print(f"کل تریدرهای دریافتی: {len(rows)}")
    final = select_top_traders(rows)
    print(f"تعداد تریدرهای نهایی: {len(final)}")

    if not final:
        send_telegram(
            f"⚠️ هیچ تریدری با حداقل {MIN_PASSING_TO_SHOW} ماه موفق "
            f"(ROI ≥ {MONTHLY_ROI_THRESHOLD*100:.0f}%) یافت نشد.\n"
            f"پیشنهاد: MIN_PASSING_TO_SHOW را کاهش دهید."
        )
        return

    message = build_message(final)
    send_telegram(message)


if __name__ == "__main__":
    main()
