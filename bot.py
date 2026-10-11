import os
import time
import requests

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
INFO_URL = "https://api.hyperliquid.xyz/info"

LIMIT = 200
MIN_ACCOUNT_VALUE = 100  # حداقل موجودی ۱۰۰ دلار
FILLS_HOURS = 24  # بازه معاملات اخیر (ساعت)
SLEEP_BETWEEN_REQUESTS = 0.25  # مکث بین درخواست‌ها برای جلوگیری از rate limit


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


def fetch_positions(address):
    """دریافت پوزیشن‌های باز یک کیف پول"""
    try:
        res = requests.post(INFO_URL, json={
            "type": "clearinghouseState",
            "user": address,
        }, timeout=15)
        res.raise_for_status()
        return res.json()
    except Exception as e:
        print(f"خطا در دریافت پوزیشن‌های {address}: {e}")
        return {}


def fetch_recent_fills(address, hours=FILLS_HOURS):
    """دریافت معاملات انجام شده در N ساعت گذشته"""
    now = int(time.time() * 1000)
    start = now - (hours * 3600 * 1000)
    try:
        res = requests.post(INFO_URL, json={
            "type": "userFillsByTime",
            "user": address,
            "startTime": start,
            "endTime": now,
        }, timeout=15)
        res.raise_for_status()
        return res.json()
    except Exception as e:
        print(f"خطا در دریافت معاملات {address}: {e}")
        return []


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
    return f"{pct:,.1f}%"


def has_open_positions(positions_data):
    """بررسی اینکه آیا کیف پول پوزیشن باز دارد یا نه"""
    if not positions_data:
        return False
    return len(positions_data.get("assetPositions", [])) > 0


def format_positions(positions_data, recent_fills):
    """فرمت‌دهی پوزیشن‌های باز با علامت‌گذاری معاملات جدید"""
    asset_positions = positions_data.get("assetPositions", [])
    if not asset_positions:
        return "   🔹 پوزیشن بازی ندارد\n"

    # مجموعه کوین‌هایی که در N ساعت گذشته معامله شده‌اند
    recent_coins = set()
    for fill in recent_fills:
        recent_coins.add(fill.get("coin", ""))

    lines = []
    for pos in asset_positions:
        p = pos.get("position", {})
        coin = p.get("coin", "?")
        szi = safe_float(p.get("szi", 0))
        entry_px = safe_float(p.get("entryPx", 0))
        pos_value = safe_float(p.get("positionValue", 0))
        unrealized_pnl = safe_float(p.get("unrealizedPnl", 0))
        margin_used = safe_float(p.get("marginUsed", 0))
        liq_px = p.get("liquidationPx")
        leverage = p.get("leverage", {}) or {}
        lev_value = leverage.get("value", "?")
        lev_type = leverage.get("type", "cross")

        side = "🟢 LONG" if szi > 0 else "🔴 SHORT"
        is_new = "🆕 " if coin in recent_coins else ""

        roe = p.get("returnOnEquity")
        roe_str = f"{safe_float(roe) * 100:,.1f}%" if roe else "N/A"

        line = (
            f"   {is_new}{side} {coin}\n"
            f"      📊 حجم: {abs(szi):,.4f} | 💰 ارزش: ${pos_value:,.0f}\n"
            f"      🎯 اهرم: {lev_value}x ({lev_type})\n"
            f"      📈 ورود: ${entry_px:,.4f} | 💵 سود/زیان: ${unrealized_pnl:,.0f} ({roe_str})\n"
            f"      🛡️ مارجین: ${margin_used:,.0f}"
        )
        if liq_px:
            line += f" | ⚠️ لیکویید: ${safe_float(liq_px):,.4f}"
        lines.append(line)

    return "\n".join(lines) + "\n"


# ============================================================
#                       انتخاب برترین‌ها
# ============================================================
def top_by_alltime(rows, limit=LIMIT, min_account=MIN_ACCOUNT_VALUE):
    """N کیف پول برتر بر اساس ROI کل دوره"""
    candidates = []
    for row in rows:
        acc = safe_float(row.get("accountValue", 0))
        if acc < min_account:
            continue
        addr = row.get("ethAddress") or row.get("address") or "?"
        candidates.append({
            "address": addr,
            "month_roi": get_perf(row, "month", "roi"),
            "month_pnl": get_perf(row, "month", "pnl"),
            "all_roi": get_perf(row, "allTime", "roi"),
            "all_pnl": get_perf(row, "allTime", "pnl"),
        })

    candidates.sort(key=lambda x: x["all_roi"], reverse=True)
    return candidates[:limit]


# ============================================================
#                        ساخت پیام
# ============================================================
def build_message(records, positions_map, fills_map):
    header = (
        f"🏆 {len(records)} کیف پول برتر Hyperliquid با پوزیشن باز\n"
        f"📅 ماهانه | 🏆 کل دوره | 📌 پوزیشن‌های باز {FILLS_HOURS} ساعته\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    lines = [header]
    for i, r in enumerate(records, 1):
        addr = r["address"]
        positions = positions_map.get(addr, {})
        fills = fills_map.get(addr, [])
        lines.append(
            f"{i}. {addr}\n"
            f"   📅 {roi_str(r['month_roi'])} (${r['month_pnl']:,.0f})\n"
            f"   🏆 {roi_str(r['all_roi'])} (${r['all_pnl']:,.0f})\n"
            f"   ─── پوزیشن‌های باز ───\n"
            f"{format_positions(positions, fills)}"
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

    top = top_by_alltime(rows)
    print(f"تعداد کیف پول در لیست اولیه: {len(top)}")

    positions_map = {}
    fills_map = {}
    filtered = []

    for idx, r in enumerate(top, 1):
        addr = r["address"]
        if not addr or addr == "?":
            continue

        print(f"[{idx}/{len(top)}] بررسی {addr}")
        positions = fetch_positions(addr)
        time.sleep(SLEEP_BETWEEN_REQUESTS)

        # فیلتر: فقط کیف پول‌هایی که پوزیشن باز دارند
        if not has_open_positions(positions):
            continue

        fills = fetch_recent_fills(addr, hours=FILLS_HOURS)
        time.sleep(SLEEP_BETWEEN_REQUESTS)

        positions_map[addr] = positions
        fills_map[addr] = fills
        filtered.append(r)

    print(f"تعداد کیف پول با پوزیشن باز: {len(filtered)}")

    if not filtered:
        send_telegram("⚠️ هیچ کیف پولی با پوزیشن باز پیدا نشد.")
        return

    message = build_message(filtered, positions_map, fills_map)
    send_telegram(message)


if __name__ == "__main__":
    main()
