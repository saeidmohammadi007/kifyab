import os
import time
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timezone

import requests

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
INFO_URL = "https://api.hyperliquid.xyz/info"

# ------------------ تنظیمات ------------------
LIMIT = 100                      # تعداد تریدر نمایش داده‌شده
TOP_BY_ALLTIME = 500             # تعداد کاندیدای اولیه برای بررسی عمیق
MONTHS_TO_CHECK = 12             # تعداد ماه‌های کامل اخیر برای بررسی
MONTHLY_ROI_THRESHOLD = 0.05     # آستانه‌ی ROI مثبت/موفق هر ماه (۵٪)
MIN_ACCOUNT_VALUE = 0
MIN_WEEK_VLM = 1000              # حداقل حجم معاملات هفته/ماه
MIN_MONTH_START_EQUITY = 20      # حداقل موجودی ابتدای ماه
MIN_CLOSED_TRADES = 20           # حداقل معامله‌ی بسته برای وین‌ریت
MAX_COINS_FOR_LEVERAGE = 5       # اهرم فقط برای N کوین پرمعامله گرفته می‌شود
MAX_GAP_DAYS = 15                # حداکثر فاصله‌ی نقطه‌ی history تا مرز ماه

SLEEP_BETWEEN_REQUESTS = 0.3
MAX_RETRIES = 4

# کوین‌های فیوچرز البانک
LBANK_COINS_FILE = os.getenv("LBANK_COINS_FILE", "lbank_coins.txt")
LBANK_COINS = set()

SESSION = requests.Session()
MS_DAY = 24 * 3600 * 1000


# ============================================================
#                     ابزار کمکی
# ============================================================
def safe_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def redact(text):
    text = str(text)
    if TELEGRAM_TOKEN:
        text = text.replace(TELEGRAM_TOKEN, "***")
    return text


def request_with_retry(method, url, **kwargs):
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
    pct = roi * 100
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:,.1f}%"


def pnl_str(pnl):
    sign = "+" if pnl >= 0 else ""
    return f"{sign}${pnl:,.0f}"


# ============================================================
#              قابل‌کپی بودن کوین‌ها در البانک
# ============================================================
def load_lbank_coins():
    coins = set()
    try:
        import ccxt
        ex = ccxt.lbank({"options": {"defaultType": "swap"}})
        ex.load_markets()
        for m in ex.markets.values():
            if m.get("swap") and m.get("quote") == "USDT" and m.get("base"):
                coins.add(str(m["base"]).upper())
        print(f"ccxt: {len(coins)} کوین فیوچرز از البانک")
    except Exception as e:
        print(f"ccxt ناموفق: {e}")

    if not coins and os.path.exists(LBANK_COINS_FILE):
        with open(LBANK_COINS_FILE, encoding="utf-8") as fh:
            raw = fh.read().replace("\n", ",")
        coins = {x.strip().upper() for x in raw.split(",") if x.strip()}
        print(f"فایل: {len(coins)} کوین")

    if not coins:
        print("⚠️ فهرست کوین‌های البانک در دسترس نیست.")
    return coins


def is_plain_perp(coin):
    c = str(coin or "")
    return bool(c) and ":" not in c and not c.startswith("@") and "/" not in c


def coin_ok(coin):
    if not is_plain_perp(coin):
        return False
    if not LBANK_COINS:
        return True
    c = str(coin)
    candidates = {c.upper()}
    if len(c) > 1 and c[0] == "k" and c[1].isupper():
        candidates.add("1000" + c[1:].upper())
    return bool(candidates & LBANK_COINS)


def tradable_share(fills):
    total = ok = 0.0
    for f in fills:
        notional = safe_float(f.get("px")) * safe_float(f.get("sz"))
        total += notional
        if coin_ok(f.get("coin")):
            ok += notional
    return (ok / total) if total > 0 else 0.0


# ============================================================
#                    دریافت داده
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


def fetch_fills(address):
    try:
        res = request_with_retry(
            "POST", INFO_URL,
            json={"type": "userFills", "user": address},
            timeout=20,
        )
        data = res.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"   خطا در fills {address}: {e}")
        return []


def fetch_leverage_map(address, fills):
    counts = {}
    for f in fills:
        if str(f.get("dir", "")).startswith("Open"):
            coin = f.get("coin")
            counts[coin] = counts.get(coin, 0) + 1
    top = sorted(counts, key=counts.get, reverse=True)[:MAX_COINS_FOR_LEVERAGE]

    result = {}
    for coin in top:
        try:
            res = request_with_retry(
                "POST", INFO_URL,
                json={"type": "activeAssetData", "user": address, "coin": coin},
                timeout=15,
            )
            lev = safe_float(((res.json() or {}).get("leverage") or {}).get("value"))
            if lev > 0:
                result[coin] = lev
        except Exception as e:
            print(f"   خطا در leverage {coin}: {e}")
        time.sleep(SLEEP_BETWEEN_REQUESTS)
    return result


# ============================================================
#                تحلیل portfolio
# ============================================================
def parse_history(history):
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


def shift_month(dt, delta):
    m = dt.year * 12 + (dt.month - 1) + delta
    return datetime(m // 12, m % 12 + 1, 1, tzinfo=timezone.utc)


def compute_monthly_rois(series):
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

    positive = sum(1 for r in rois if r is not None and r > 0)
    return rois, positive


# ============================================================
#                تحلیل معاملات
# ============================================================
def compute_trade_stats(fills, av_ts=None, av_vals=None, lev_map=None):
    groups = {}
    for f in fills:
        closed = safe_float(f.get("closedPnl"))
        direction = str(f.get("dir", ""))
        if closed == 0 and not direction.startswith("Close"):
            continue
        key = (f.get("coin"), f.get("oid"))
        groups[key] = groups.get(key, 0.0) + closed - safe_float(f.get("fee"))

    nets = list(groups.values())
    n = len(nets)
    if n < MIN_CLOSED_TRADES:
        return None

    wins = [x for x in nets if x > 0]
    win_rate = len(wins) / n

    times = [f["time"] for f in fills if f.get("time")]
    days = (max(times) - min(times)) / MS_DAY if times else 0
    per_day = n / max(days, 1)

    # حجم هر ورود (notional) نسبت به موجودی
    opens = {}
    for f in fills:
        if not str(f.get("dir", "")).startswith("Open"):
            continue
        key = (f.get("coin"), f.get("oid"))
        notional = safe_float(f.get("px")) * safe_float(f.get("sz"))
        t = f.get("time") or 0
        prev = opens.get(key)
        opens[key] = (
            min(prev[0], t) if prev else t,
            (prev[1] if prev else 0.0) + notional,
        )

    sizes = []
    for (coin, _oid), (t, notional) in opens.items():
        lev = (lev_map or {}).get(coin)
        if not lev:
            continue
        equity = value_near(av_ts, av_vals, t) if av_ts else None
        if equity and equity > 0 and notional > 0:
            sizes.append((notional / lev) / equity)
    sizes.sort()

    size_median = size_p90 = size_max = None
    if len(sizes) >= 5:
        size_median = sizes[len(sizes) // 2]
        size_max = sizes[-1]
        size_p90 = sizes[min(len(sizes) - 1, int(len(sizes) * 0.9))]

    return {
        "n": n,
        "win_rate": win_rate,
        "days": days,
        "per_day": per_day,
        "size_median": size_median,
        "size_p90": size_p90,
        "size_max": size_max,
    }


# ============================================================
#                انتخاب تریدرهای برتر
# ============================================================
def select_top_traders(rows):
    candidates = []
    for row in rows:
        addr = row.get("ethAddress") or row.get("address")
        if not addr:
            continue
        av = safe_float(row.get("accountValue", 0))
        if av < MIN_ACCOUNT_VALUE:
            continue
        wk = get_perf(row, "week", "vlm")
        mo = get_perf(row, "month", "vlm")
        if wk < MIN_WEEK_VLM and mo < MIN_WEEK_VLM:
            continue
        all_roi = get_perf(row, "allTime", "roi")
        if all_roi <= 0:
            continue

        candidates.append({
            "address": addr,
            "display_name": row.get("displayName") or "",
            "account_value": av,
            "day_roi": get_perf(row, "day", "roi"),
            "week_roi": get_perf(row, "week", "roi"),
            "month_roi": get_perf(row, "month", "roi"),
            "all_roi": all_roi,
            "day_pnl": get_perf(row, "day", "pnl"),
            "week_pnl": get_perf(row, "week", "pnl"),
            "month_pnl": get_perf(row, "month", "pnl"),
            "all_pnl": get_perf(row, "allTime", "pnl"),
        })

    candidates.sort(key=lambda x: x["all_roi"], reverse=True)
    candidates = candidates[:TOP_BY_ALLTIME]
    print(f"کاندیداهای اولیه: {len(candidates)}")

    analyzed = []
    for i, c in enumerate(candidates, 1):
        addr = c["address"]
        print(f"[{i}/{len(candidates)}] {addr}")

        try:
            portfolio = fetch_portfolio(addr)
            time.sleep(SLEEP_BETWEEN_REQUESTS)
            series = extract_alltime(portfolio) if portfolio else None
            if not series:
                continue

            c["av_ts"], c["av_vals"] = series[0], series[1]
            c["age_days"] = int((time.time() * 1000 - series[2][0]) / MS_DAY)

            monthly_rois, positive = compute_monthly_rois(series)
            c["monthly_rois"] = monthly_rois
            c["positive_months"] = positive
            valid = [r for r in monthly_rois if r is not None]
            c["avg_monthly_roi"] = (sum(valid) / len(valid)) if valid else None

            fills = fetch_fills(addr)
            time.sleep(SLEEP_BETWEEN_REQUESTS)

            if not fills:
                c["tradable_share"] = 0.0
                c["trade_stats"] = None
                c["max_lev"] = None
                analyzed.append(c)
                continue

            c["tradable_share"] = tradable_share(fills)
            cfills = [f for f in fills if coin_ok(f.get("coin"))]

            lev_map = fetch_leverage_map(addr, cfills) if cfills else {}
            c["max_lev"] = max(lev_map.values()) if lev_map else None

            c["trade_stats"] = compute_trade_stats(
                cfills, c.get("av_ts"), c.get("av_vals"), lev_map
            )
            analyzed.append(c)

        except Exception as e:
            print(f"   ✗ خطا: {e}")

    # مرتب‌سازی: اول تعداد ماه مثبت، بعد ROI کل
    analyzed.sort(
        key=lambda x: (x.get("positive_months", 0), x["all_roi"]),
        reverse=True,
    )
    print(f"تعداد تحلیل‌شده: {len(analyzed)}")
    return analyzed[:LIMIT]


# ============================================================
#                ساخت پیام
# ============================================================
def build_header(count):
    return (
        f"🏆 {count} تریدر برتر Hyperliquid\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 معیارها: ماه‌های مثبت (از {MONTHS_TO_CHECK} ماه اخیر)، "
        f"وین‌ریت، معاملات روزانه، مارجین هر ورود، "
        f"سود ماهانه و سهم کوین‌های البانک\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )


def build_card(i, r):
    name_part = f" ({r['display_name']})" if r["display_name"] else ""
    medal = {1: "🥇 ", 2: "🥈 ", 3: "🥉 "}.get(i, "")

    ts = r.get("trade_stats")
    if ts:
        win_line = f"📈 وین‌ریت: {ts['win_rate']*100:.0f}% ({ts['n']} معامله)"
        perday_line = f"🔁 میانگین معامله در روز: {ts['per_day']:.1f}"
        if ts.get("size_median") is not None:
            margin_line = (
                f"💼 مارجین هر ورود: میانه {ts['size_median']*100:.1f}% "
                f"| ۹۰٪ زیر {ts['size_p90']*100:.1f}% "
                f"| حداکثر {ts['size_max']*100:.1f}%"
            )
        else:
            margin_line = "💼 مارجین هر ورود: داده‌ی کافی نیست"
    else:
        win_line = "📈 وین‌ریت: داده‌ی کافی نیست"
        perday_line = "🔁 میانگین معامله در روز: —"
        margin_line = "💼 مارجین هر ورود: —"

    avg_monthly = r.get("avg_monthly_roi")
    avg_line = f"{avg_monthly*100:+.1f}%" if avg_monthly is not None else "—"

    rois = r.get("monthly_rois") or []
    rois_line = " · ".join(
        f"{x*100:+.1f}%" if x is not None else "—" for x in rois
    )

    return (
        f"┏━━━━━━━━━━━━━━━━━━━━━\n"
        f"┃ {medal}#{i}{name_part}\n"
        f"┃ 🔗 {r['address']}\n"
        f"┃ 💰 موجودی: ${r['account_value']:,.0f}  |  🎂 {r['age_days']} روز\n"
        f"┃\n"
        f"┃ ✅ ماه‌های مثبت: {r['positive_months']}/{MONTHS_TO_CHECK}\n"
        f"┃ {win_line}\n"
        f"┃ {perday_line}\n"
        f"┃ {margin_line}\n"
        f"┃ 🏦 سهم کوین‌های البانک: {r['tradable_share']*100:.0f}%\n"
        f"┃\n"
        f"┃ 📅 سود هر ماه (جدید → قدیم):\n"
        f"┃    {rois_line}\n"
        f"┃ 📊 میانگین سود ماهانه: {avg_line}\n"
        f"┃ 🏆 ROI کل: {roi_str(r['all_roi'])} | PnL: {pnl_str(r['all_pnl'])}\n"
        f"┗━━━━━━━━━━━━━━━━━━━━━\n\n"
    )


def build_chunks(records, limit=4000):
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
#                ارسال به تلگرام
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
#                اجرای اصلی
# ============================================================
def main():
    global LBANK_COINS
    LBANK_COINS = load_lbank_coins()

    rows = fetch_rows()
    if not rows:
        send_telegram("⚠️ دریافت داده از Hyperliquid ناموفق بود.")
        return

    print(f"کل تریدرهای دریافتی: {len(rows)}")
    final = select_top_traders(rows)
    print(f"تعداد نهایی: {len(final)}")

    if not final:
        send_telegram("⚠️ هیچ تریدری برای نمایش یافت نشد.")
        return

    send_telegram(build_chunks(final))


if __name__ == "__main__":
    main()
