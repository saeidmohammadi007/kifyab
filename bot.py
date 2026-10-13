import os
import time
from bisect import bisect_left
from datetime import datetime, timezone

import requests

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
INFO_URL = "https://api.hyperliquid.xyz/info"

# ------------------ تنظیمات اصلی ------------------
LIMIT = 20                       # تعداد تریدر برتر نمایش داده‌شده
MIN_ACCOUNT_VALUE = 5000         # حداقل موجودی دلاری
MIN_WEEK_VLM = 1000              # حداقل حجم معاملات هفته/ماه اخیر
MIN_AGE_DAYS = 365               # حداقل سابقه (یک سال)
MIN_PASSING_TO_SHOW = 5          # حداقل ماه موفق برای نمایش

MONTHLY_ROI_THRESHOLD = 0.05     # آستانه ROI هر ماه (۵٪)
MONTHS_TO_CHECK = 12             # تعداد ماه‌های کامل اخیر
MIN_MONTH_START_EQUITY = 500     # حداقل موجودی ابتدای ماه برای معتبر بودن ROI
MAX_ALLOWED_MONTH_LOSS = -0.30   # اگر ماهی بدتر از این بود حذف شود (None = غیرفعال)
MAX_GAP_DAYS = 15                # حداکثر فاصله‌ی مجاز نقطه‌ی history تا مرز ماه

REQUIRE_ALL_MONTHS_POSITIVE = True   # فقط تریدرهایی که همه ماه‌ها ROI مثبت دارند
MIN_VALID_MONTHS = 10                # حداقل تعداد ماه با داده‌ی معتبر (ماه‌های بدون داده نادیده گرفته می‌شوند)

# ------------------ تنظیمات عملکرد ------------------
TOP_BY_ALLTIME = 500             # تعداد کاندیدای اولیه برای بررسی عمیق
SLEEP_BETWEEN_REQUESTS = 0.4     # مکث بین درخواست‌ها
MAX_RETRIES = 4

SESSION = requests.Session()
MS_DAY = 24 * 3600 * 1000


# ============================================================
#                        ابزار کمکی
# ============================================================
def safe_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def redact(text):
    """حذف توکن تلگرام از متن خطا"""
    text = str(text)
    if TELEGRAM_TOKEN:
        text = text.replace(TELEGRAM_TOKEN, "***")
    return text


def request_with_retry(method, url, **kwargs):
    """درخواست با retry و backoff برای 429 و خطاهای شبکه"""
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            res = SESSION.request(method, url, **kwargs)
            if res.status_code == 429 or res.status_code >= 500:
                time.sleep(2 ** attempt)
                last_exc = requests.HTTPError(f"HTTP {res.status_code}")
                continue
            res.raise_for_status()
            return res
        except requests.RequestException as e:
            last_exc = e
            time.sleep(2 ** attempt)
    raise last_exc


def get_perf(row, window, key):
    for perf in row.get("windowPerformances") or []:
        if isinstance(perf, list) and len(perf) == 2 and perf[0] == window:
            return safe_float((perf[1] or {}).get(key, 0))
    return 0.0


def roi_str(roi):
    """roi همیشه به‌صورت کسر (مثلاً 0.12 = 12٪) است"""
    pct = roi * 100
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:,.1f}%"


def pnl_str(pnl):
    sign = "+" if pnl >= 0 else ""
    return f"{sign}${pnl:,.0f}"


def roi_list_str(rois):
    return " · ".join(f"{x*100:+.1f}%" if x is not None else "—" for x in rois)


# ============================================================
#                         دریافت داده
# ============================================================
def fetch_rows():
    try:
        r = request_with_retry("GET", LEADERBOARD_URL, timeout=60)
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
        res = request_with_retry(
            "POST", INFO_URL,
            json={"type": "portfolio", "user": address},
            timeout=15,
        )
        return res.json()
    except Exception as e:
        print(f"   خطا در portfolio {address}: {e}")
        return None


# ============================================================
#                    تحلیل portfolio
# ============================================================
def parse_history(history):
    """تبدیل history به دو لیست مرتب‌شده (timestamps, values)"""
    pairs = []
    for point in history or []:
        if isinstance(point, list) and len(point) == 2:
            try:
                pairs.append((int(point[0]), float(point[1])))
            except (ValueError, TypeError):
                continue
    pairs.sort(key=lambda p: p[0])
    return [p[0] for p in pairs], [p[1] for p in pairs]


def extract_alltime(portfolio_data):
    """خروجی: (av_ts, av_vals, pnl_ts, pnl_vals) یا None"""
    alltime = None
    if isinstance(portfolio_data, list):
        for item in portfolio_data:
            if (isinstance(item, list) and len(item) == 2
                    and item[0] == "allTime" and isinstance(item[1], dict)):
                alltime = item[1]
                break
    elif isinstance(portfolio_data, dict):
        alltime = portfolio_data

    if not alltime:
        return None

    av_ts, av_vals = parse_history(alltime.get("accountValueHistory"))
    pnl_ts, pnl_vals = parse_history(alltime.get("pnlHistory"))
    if not av_ts or not pnl_ts:
        return None
    return av_ts, av_vals, pnl_ts, pnl_vals


def value_near(ts_list, vals, t_ms):
    """مقدار نزدیک‌ترین نقطه به t_ms؛ اگر فاصله > MAX_GAP_DAYS باشد None"""
    if not ts_list:
        return None
    idx = bisect_left(ts_list, t_ms)
    best = None
    for j in (idx - 1, idx):
        if 0 <= j < len(ts_list):
            diff = abs(ts_list[j] - t_ms)
            if best is None or diff < best[0]:
                best = (diff, vals[j])
    if best is None or best[0] > MAX_GAP_DAYS * MS_DAY:
        return None
    return best[1]


def account_age_days(series):
    pnl_ts = series[2]
    return int((time.time() * 1000 - pnl_ts[0]) / MS_DAY)


def shift_month(dt, delta):
    """جابه‌جایی ماه تقویمی؛ dt اول ماه است"""
    m = dt.year * 12 + (dt.month - 1) + delta
    return datetime(m // 12, m % 12 + 1, 1, tzinfo=timezone.utc)


def compute_monthly_rois(series):
    """
    ROI ماه تقویمی = (تغییر pnl تجمعی در ماه) / (موجودی ابتدای ماه)
    فقط ماه‌های کامل (ماه جاری حذف می‌شود). ترتیب خروجی: جدید → قدیم.
    """
    av_ts, av_vals, pnl_ts, pnl_vals = series
    now = datetime.now(timezone.utc)
    cur_month = datetime(now.year, now.month, 1, tzinfo=timezone.utc)

    rois = []
    for i in range(1, MONTHS_TO_CHECK + 1):
        start = shift_month(cur_month, -i)
        end = shift_month(cur_month, -i + 1)
        s_ms = int(start.timestamp() * 1000)
        e_ms = int(end.timestamp() * 1000)

        av_start = value_near(av_ts, av_vals, s_ms)
        pnl_start = value_near(pnl_ts, pnl_vals, s_ms)
        pnl_end = value_near(pnl_ts, pnl_vals, e_ms)

        if (av_start is None or pnl_start is None or pnl_end is None
                or av_start < MIN_MONTH_START_EQUITY):
            rois.append(None)
            continue

        rois.append((pnl_end - pnl_start) / av_start)

    passing = sum(1 for r in rois if r is not None and r >= MONTHLY_ROI_THRESHOLD)
    return rois, passing


# ============================================================
#                       انتخاب برترین‌ها
# ============================================================
def build_candidates(rows):
    candidates = []
    for row in rows:
        addr = row.get("ethAddress") or row.get("address")
        if not addr:
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
    return [c for c in candidates if c["all_roi"] > 0]


def passes_light_filters(c):
    if c["account_value"] < MIN_ACCOUNT_VALUE:
        return False
    if c["week_vlm"] < MIN_WEEK_VLM and c["month_vlm"] < MIN_WEEK_VLM:
        return False
    return True


def select_top_traders(rows):
    candidates = build_candidates(rows)
    print(f"تعداد کل کاندیداها (ROI مثبت): {len(candidates)}")

    # اول فیلتر سبک، بعد برش top N
    pre_filtered = [c for c in candidates if passes_light_filters(c)]
    pre_filtered.sort(key=lambda x: x["all_roi"], reverse=True)
    pre_filtered = pre_filtered[:TOP_BY_ALLTIME]
    print(f"پس از فیلترهای سبک: {len(pre_filtered)}")

    analyzed = []
    for i, c in enumerate(pre_filtered, 1):
        addr = c["address"]
        print(f"[{i}/{len(pre_filtered)}] بررسی: {addr}")

        try:
            portfolio = fetch_portfolio(addr)
            time.sleep(SLEEP_BETWEEN_REQUESTS)

            series = extract_alltime(portfolio) if portfolio else None
            if not series:
                print("   ✗ portfolio خالی")
                continue

            age_days = account_age_days(series)
            if age_days < MIN_AGE_DAYS:
                print(f"   ✗ سن حساب کم: {age_days} روز")
                continue

            monthly_rois, passing = compute_monthly_rois(series)

            if MAX_ALLOWED_MONTH_LOSS is not None and any(
                r is not None and r < MAX_ALLOWED_MONTH_LOSS for r in monthly_rois
            ):
                print("   ✗ ماه با ضرر شدید")
                continue

            if REQUIRE_ALL_MONTHS_POSITIVE:
                valid = [r for r in monthly_rois if r is not None]
                if len(valid) < MIN_VALID_MONTHS:
                    print(f"   ✗ ماه‌های معتبر کم: {len(valid)}")
                    continue
                if any(r <= 0 for r in valid):
                    print("   ✗ حداقل یک ماه منفی/صفر")
                    continue

            print(f"   ✓ سن: {age_days} روز | موفق: {passing}/{MONTHS_TO_CHECK}")
            print(f"      {roi_list_str(monthly_rois)}")

            c["age_days"] = age_days
            c["monthly_rois"] = monthly_rois
            c["passing_months"] = passing
            analyzed.append(c)

        except Exception as e:
            print(f"   ✗ خطا برای {addr}: {e}")

    analyzed.sort(key=lambda x: (x["passing_months"], x["all_roi"]), reverse=True)
    filtered = [c for c in analyzed if c["passing_months"] >= MIN_PASSING_TO_SHOW]
    print(f"تعداد تریدرهای با ≥ {MIN_PASSING_TO_SHOW} ماه موفق: {len(filtered)}")
    return filtered[:LIMIT]


# ============================================================
#                        ساخت پیام
# ============================================================
def build_header(count):
    return (
        f"🏆 {count} تریدر برتر Hyperliquid\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 معیار رتبه‌بندی: تعداد ماه‌های با ROI ≥ "
        f"{MONTHLY_ROI_THRESHOLD*100:.0f}% (از {MONTHS_TO_CHECK} ماه کامل اخیر)\n"
        f"✅ فیلترها:\n"
        f"   • موجودی ≥ ${MIN_ACCOUNT_VALUE:,}\n"
        f"   • حجم معاملات هفته/ماه ≥ ${MIN_WEEK_VLM:,}\n"
        f"   • سابقه ≥ {MIN_AGE_DAYS} روز\n"
        f"   • حداقل {MIN_PASSING_TO_SHOW} ماه موفق برای نمایش\n"
        + ("   • همه ماه‌ها ROI مثبت\n" if REQUIRE_ALL_MONTHS_POSITIVE else "")
        + f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )


def build_card(i, r):
    name_part = f" ({r['display_name']})" if r["display_name"] else ""
    medal = {1: "🥇 ", 2: "🥈 ", 3: "🥉 "}.get(i, "")
    return (
        f"┏━━━━━━━━━━━━━━━━━━━━━\n"
        f"┃ {medal}#{i}{name_part}\n"
        f"┃ 🔗 {r['address']}\n"
        f"┃ 💰 موجودی: ${r['account_value']:,.0f}  |  🎂 {r['age_days']} روز\n"
        f"┃\n"
        f"┃ 🎯 ماه‌های موفق: {r['passing_months']}/{MONTHS_TO_CHECK}\n"
        f"┃\n"
        f"┃ 📅 روزانه : {roi_str(r['day_roi'])} · {pnl_str(r['day_pnl'])}\n"
        f"┃ 📆 هفتگی : {roi_str(r['week_roi'])} · {pnl_str(r['week_pnl'])}\n"
        f"┃ 🗓️ ماهانه : {roi_str(r['month_roi'])} · {pnl_str(r['month_pnl'])}\n"
        f"┃ 🏆 کل دوره: {roi_str(r['all_roi'])} · {pnl_str(r['all_pnl'])}\n"
        f"┃\n"
        f"┃ 📊 ROI ماهانه (جدید→قدیم):\n"
        f"┃    {roi_list_str(r['monthly_rois'])}\n"
        f"┗━━━━━━━━━━━━━━━━━━━━━\n\n"
    )


def build_chunks(records, limit=4000):
    """پیام‌ها را کارت‌به‌کارت تقسیم می‌کند تا وسط کارت قطع نشود"""
    chunks = []
    current = build_header(len(records))
    for i, r in enumerate(records, 1):
        card = build_card(i, r)
        if len(current) + len(card) > limit:
            chunks.append(current)
            current = ""
        current += card
    if current.strip():
        chunks.append(current)
    return chunks


# ============================================================
#                       ارسال به تلگرام
# ============================================================
def send_telegram(message_or_chunks):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("خطا: توکن یا Chat ID تنظیم نشده است.")
        return

    chunks = ([message_or_chunks] if isinstance(message_or_chunks, str)
              else message_or_chunks)
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    total = len(chunks)
    for idx, chunk in enumerate(chunks, 1):
        try:
            request_with_retry("POST", url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk,
                "disable_web_page_preview": True,
            }, timeout=30)
            print(f"✅ بخش {idx}/{total} ارسال شد.")
        except Exception as e:
            print(f"خطا در ارسال بخش {idx}: {redact(e)}")
        time.sleep(1)


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
            f"پیشنهاد: MIN_PASSING_TO_SHOW یا MONTHLY_ROI_THRESHOLD را کاهش دهید."
        )
        return

    send_telegram(build_chunks(final))


if __name__ == "__main__":
    main()
