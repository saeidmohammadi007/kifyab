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
LIMIT = 100                      # تعداد نهایی تریدرها
MIN_ACCOUNT_VALUE = 1000         # حداقل موجودی دلاری
MIN_WEEK_VLM = 1000              # حداقل حجم معاملات هفته اخیر
MIN_AGE_DAYS = 365               # حداقل سابقه (یک سال)
MONTHLY_ROI_THRESHOLD = 0.05     # حداقل ROI هر ماه (۵٪)
MONTHS_TO_CHECK = 12             # تعداد ماه‌های اخیر برای بررسی

# ------------------ تنظیمات عملکرد ------------------
TOP_BY_ALLTIME = 300             # تعداد کاندیدای اولیه برای بررسی عمیق
SLEEP_BETWEEN_REQUESTS = 0.4     # مکث بین درخواست‌ها (ضد rate limit)


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
    """تاریخچه PnL برای محاسبه سن حساب"""
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
    """دریافت معاملات یک بازه زمانی مشخص"""
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


def vlm_str(vlm):
    if vlm >= 1_000_000:
        return f"${vlm / 1_000_000:,.2f}M"
    if vlm >= 1_000:
        return f"${vlm / 1_000:,.1f}K"
    return f"${vlm:,.0f}"


# ============================================================
#                    فیلترهای سبک (بدون API اضافه)
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
#                    فیلترهای سنگین (نیازمند API)
# ============================================================
def get_account_age_days(address, portfolio_data=None):
    """تعداد روزهای از اولین فعالیت حساب"""
    if portfolio_data is None:
        portfolio_data = fetch_portfolio(address)

    if not portfolio_data:
        return 0

    pnl_history = []

    # حالت ۱: پاسخ به صورت list از جفت‌های [window, data]
    if isinstance(portfolio_data, list):
        # چک می‌کنیم که آیا خودش pnlHistory خام است یا آرایه‌ی windowها
        if portfolio_data and isinstance(portfolio_data[0], list):
            first = portfolio_data[0]
            # فرمت خام [[t, v], [t, v], ...]
            if (len(first) == 2
                    and isinstance(first[0], (int, float))
                    and isinstance(first[1], (int, float))):
                pnl_history = portfolio_data
            else:
                # فرمت windowها: [["day", {...}], ["allTime", {...}], ...]
                for item in portfolio_data:
                    if (isinstance(item, list) and len(item) == 2
                            and item[0] == "allTime"
                            and isinstance(item[1], dict)):
                        pnl_history = item[1].get("pnlHistory") or []
                        break

    # حالت ۲: پاسخ به صورت dict
    elif isinstance(portfolio_data, dict):
        pnl_history = portfolio_data.get("pnlHistory") or []

    if not pnl_history or not isinstance(pnl_history[0], list):
        return 0

    try:
        first_activity_ms = pnl_history[0][0]
    except (IndexError, TypeError):
        return 0

    age_ms = time.time() * 1000 - first_activity_ms
    return int(age_ms / (1000 * 60 * 60 * 24))


def check_monthly_roi(address, current_account_value):
    """
    بررسی می‌کند که آیا ROI هر یک از N ماه اخیر بالای MONTHLY_ROI_THRESHOLD بوده است.
    خروجی: (نتیجه bool، لیست ROIهای ماهانه)
    """
    now = datetime.utcnow()
    monthly_rois = []

    if current_account_value <= 0:
        return False, monthly_rois

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
            return False, monthly_rois

        monthly_pnl = 0.0
        for f in fills:
            if isinstance(f, dict):
                monthly_pnl += safe_float(f.get("closedPnl", 0))

        monthly_roi = monthly_pnl / current_account_value
        monthly_rois.append(monthly_roi)

        if monthly_roi < MONTHLY_ROI_THRESHOLD:
            return False, monthly_rois

    return True, monthly_rois


# ============================================================
#                       انتخاب برترین‌ها
# ============================================================
def build_candidates(rows):
    """ساخت لیست کاندیداهای اولیه (مرتب‌شده بر اساس ROI کل)"""
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
            "day_vlm": get_perf(row, "day", "vlm"),
            "week_vlm": get_perf(row, "week", "vlm"),
            "month_vlm": get_perf(row, "month", "vlm"),
        })

    candidates = [c for c in candidates if c["all_roi"] > 0]
    candidates.sort(key=lambda x: x["all_roi"], reverse=True)
    return candidates


def select_top_traders(rows, limit=LIMIT):
    """انتخاب تریدرهای نهایی با اعمال همه فیلترها"""
    candidates = build_candidates(rows)
    print(f"تعداد کل کاندیداها (ROI مثبت): {len(candidates)}")

    # مرحله ۱: فیلترهای سبک روی N تریدر برتر
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

    # مرحله ۲: فیلترهای سنگین (سن حساب + ROI ماهانه)
    final = []
    checked = 0
    for c in pre_filtered:
        if len(final) >= limit:
            break

        addr = c["address"]
        checked += 1
        print(f"[{checked}/{len(pre_filtered)}] بررسی عمیق: {addr}")

        try:
            portfolio = fetch_portfolio(addr)
            time.sleep(SLEEP_BETWEEN_REQUESTS)

            age_days = get_account_age_days(addr, portfolio)
            if age_days < MIN_AGE_DAYS:
                print(f"   ✗ سن حساب کم: {age_days} روز")
                continue

            ok, monthly_rois = check_monthly_roi(addr, c["account_value"])
            if not ok:
                rois_txt = ", ".join(f"{r*100:+.1f}%" for r in monthly_rois)
                print(f"   ✗ ROI ماهانه رد شد: [{rois_txt}]")
                continue

            c["age_days"] = age_days
            c["monthly_rois"] = monthly_rois
            c["min_monthly_roi"] = min(monthly_rois)
            final.append(c)
            print(
                f"   ✓ قبول | سن: {age_days} روز | "
                f"کمترین ROI ماهانه: {min(monthly_rois)*100:.1f}%"
            )

        except Exception as e:
            print(f"   ✗ خطای نامشخص برای {addr}: {e}")
            continue

    return final


# ============================================================
#                        ساخت پیام
# ============================================================
def build_message(records):
    header = (
        f"🏆 گزارش {len(records)} تریدر برتر Hyperliquid\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ فیلترها:\n"
        f"   • موجودی ≥ ${MIN_ACCOUNT_VALUE:,}\n"
        f"   • حجم معاملات هفته ≥ ${MIN_WEEK_VLM:,}\n"
        f"   • سابقه ≥ {MIN_AGE_DAYS} روز (یک سال)\n"
        f"   • ROI هر ماه از {MONTHS_TO_CHECK} ماه اخیر ≥ {MONTHLY_ROI_THRESHOLD*100:.0f}%\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    lines = [header]

    for i, r in enumerate(records, 1):
        name_part = f" ({r['display_name']})" if r["display_name"] else ""
        monthly_str = " · ".join(f"{x*100:+.1f}%" for x in r["monthly_rois"])
        lines.append(
            f"┏━━━━━━━━━━━━━━━━━━━━━\n"
            f"┃ #{i}{name_part}\n"
            f"┃ 🔗 {r['address']}\n"
            f"┃ 💰 موجودی: ${r['account_value']:,.0f}  |  🎂 {r['age_days']} روز\n"
            f"┃\n"
            f"┃ 📅 روزانه : {roi_str(r['day_roi']):>10} · {pnl_str(r['day_pnl']):>12}\n"
            f"┃ 📆 هفتگی : {roi_str(r['week_roi']):>10} · {pnl_str(r['week_pnl']):>12}\n"
            f"┃ 🗓️ ماهانه : {roi_str(r['month_roi']):>10} · {pnl_str(r['month_pnl']):>12}\n"
            f"┃ 🏆 کل دوره: {roi_str(r['all_roi']):>10} · {pnl_str(r['all_pnl']):>12}\n"
            f"┃\n"
            f"┃ 📊 ROI ماهانه {MONTHS_TO_CHECK} ماه اخیر (جدید→قدیم):\n"
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
            f"⚠️ با فیلترهای فعلی (ROI ماهانه ≥ {MONTHLY_ROI_THRESHOLD*100:.0f}% "
            f"برای {MONTHS_TO_CHECK} ماه متوالی) هیچ تریدری یافت نشد.\n"
            f"پیشنهاد: آستانه یا تعداد ماه‌ها را کاهش دهید."
        )
        return

    message = build_message(final)
    send_telegram(message)


if __name__ == "__main__":
    main()
