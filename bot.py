import os
import time
import requests

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
INFO_URL = "https://api.hyperliquid.xyz/info"

LIMIT = 200
MIN_ACCOUNT_VALUE = 100  # حداقل موجودی ۱۰۰ دلار
FILLS_HOURS = 1  # بازه معاملات اخیر (۱ ساعت)
SLEEP_BETWEEN_REQUESTS = 0.3  # مکث بین درخواست‌ها برای جلوگیری از rate limit


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
    """دریافت پوزیشن‌ها و سفارش‌های باز یک کیف پول"""
    try:
        res = requests.post(INFO_URL, json={
            "type": "clearinghouseState",
            "user": address,
        }, timeout=15)
        res.raise_for_status()
        data = res.json()

        # دریافت سفارش‌های باز (شامل TP/SL)
        try:
            orders_res = requests.post(INFO_URL, json={
                "type": "openOrders",
                "user": address,
            }, timeout=15)
            orders_res.raise_for_status()
            data["openOrders"] = orders_res.json()
        except Exception as e:
            print(f"خطا در دریافت سفارش‌های باز {address}: {e}")
            data["openOrders"] = []

        return data
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
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:,.1f}%"


def pnl_str(pnl):
    sign = "+" if pnl >= 0 else ""
    return f"{sign}${pnl:,.0f}"


def short_addr(addr, n=8):
    """کوتاه کردن آدرس برای نمایش تمیزتر"""
    if not addr or len(addr) < 14:
        return addr
    return f"{addr[:n]}...{addr[-4:]}"


def fmt_price(px):
    """فرمت قیمت بر اساس بزرگی عدد"""
    if px == 0:
        return "$0"
    if px >= 1000:
        return f"${px:,.2f}"
    if px >= 1:
        return f"${px:,.4f}"
    return f"${px:,.6f}"


def fmt_time_ago(ms_time):
    """نمایش زمان به صورت خوانا"""
    secs_ago = int((time.time() * 1000 - ms_time) / 1000)
    if secs_ago < 60:
        return f"{secs_ago} ثانیه پیش"
    if secs_ago < 3600:
        return f"{secs_ago // 60} دقیقه پیش"
    return f"{secs_ago // 3600} ساعت پیش"


def extract_new_coins(recent_fills):
    """استخراج کوین‌هایی که در بازه مشخص شده پوزیشن جدید باز شده"""
    new_coins = {}
    for fill in recent_fills:
        dir_ = fill.get("dir", "")
        coin = fill.get("coin", "")

        if dir_.startswith("Open"):
            if coin not in new_coins or fill.get("time", 0) > new_coins[coin]["time"]:
                new_coins[coin] = {
                    "dir": dir_,
                    "time": fill.get("time", 0),
                    "px": safe_float(fill.get("px", 0)),
                    "sz": safe_float(fill.get("sz", 0)),
                    "fee": safe_float(fill.get("fee", 0)),
                }
    return new_coins


def has_new_open_positions(recent_fills):
    """بررسی اینکه آیا در بازه اخیر پوزیشن جدیدی باز شده یا نه"""
    for fill in recent_fills:
        if fill.get("dir", "").startswith("Open"):
            return True
    return False


def extract_take_profits(open_orders, coin):
    """استخراج حد سودهای مربوط به یک کوین خاص از سفارش‌های باز"""
    tps = []
    if not isinstance(open_orders, list):
        return tps

    for order in open_orders:
        if not isinstance(order, dict):
            continue
        if order.get("coin") != coin:
            continue

        order_type = str(order.get("orderType", ""))
        is_tp = (
            "Take Profit" in order_type
            or order.get("triggerCondition") == "tp"
            or "tp" in order_type.lower()
        )
        if not is_tp:
            continue

        trigger_px = order.get("triggerPx") or order.get("triggerPrice")
        if not trigger_px:
            continue

        tps.append({
            "px": safe_float(trigger_px),
            "sz": safe_float(order.get("sz", 0)),
            "is_market": bool(order.get("isMarket", False)),
        })

    tps.sort(key=lambda x: x["px"])
    return tps


def _format_single_position(pos, new_coins, open_orders, is_last=False, show_time=False):
    """فرمت‌دهی یک پوزیشن به صورت درخت‌نما"""
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

    side_icon = "🟢" if szi > 0 else "🔴"
    side_txt = "LONG" if szi > 0 else "SHORT"

    roe = p.get("returnOnEquity")
    roe_val = safe_float(roe) * 100 if roe else 0
    pnl_icon = "📈" if unrealized_pnl >= 0 else "📉"

    prefix = "   └ " if is_last else "   ├ "
    cont = "      " if is_last else "   │  "

    lines = []
    lines.append(f"{prefix}{side_icon} {coin} · {side_txt} · {lev_value}x {lev_type}\n")
    lines.append(f"{cont}💰 ارزش: ${pos_value:,.0f}  |  📊 حجم: {abs(szi):,.4f}\n")
    lines.append(f"{cont}📈 ورود: {fmt_price(entry_px)}\n")

    pnl_sign = "+" if unrealized_pnl >= 0 else ""
    lines.append(
        f"{cont}{pnl_icon} سود/زیان: {pnl_sign}${unrealized_pnl:,.0f} ({roe_val:+.1f}%)\n"
    )

    liq_txt = f"  |  ⚠️ لیکویید: {fmt_price(safe_float(liq_px))}" if liq_px else ""
    lines.append(f"{cont}🛡️ مارجین: ${margin_used:,.0f}{liq_txt}\n")

    # حد سود
    take_profits = extract_take_profits(open_orders, coin)
    if take_profits:
        tp_parts = []
        for tp in take_profits:
            tp_type = "بازار" if tp["is_market"] else "لیمیت"
            tp_parts.append(f"{fmt_price(tp['px'])} ({tp_type})")
        lines.append(f"{cont}🎯 حد سود: {' | '.join(tp_parts)}\n")

    # اطلاعات زمان باز شدن (فقط برای پوزیشن‌های جدید)
    if show_time and coin in new_coins:
        nc = new_coins[coin]
        lines.append(
            f"{cont}⏱️ باز شده: {fmt_time_ago(nc['time'])}\n"
            f"{cont}📝 {nc['dir']} · {fmt_price(nc['px'])} · {nc['sz']:,.4f}\n"
        )

    return "".join(lines)


def format_positions(positions_data, recent_fills):
    """فرمت‌دهی تمیز پوزیشن‌ها با تفکیک جدید/قبلی"""
    asset_positions = positions_data.get("assetPositions", [])
    if not asset_positions:
        return "   └ هیچ پوزیشن بازی ندارد\n"

    new_coins = extract_new_coins(recent_fills)
    open_orders = positions_data.get("openOrders", []) or []

    # جداسازی پوزیشن‌های جدید از قدیمی
    new_positions = []
    old_positions = []
    for pos in asset_positions:
        coin = pos.get("position", {}).get("coin", "")
        if coin in new_coins:
            new_positions.append(pos)
        else:
            old_positions.append(pos)

    lines = []

    # پوزیشن‌های جدید
    if new_positions:
        lines.append("   🆕 پوزیشن‌های جدید:\n")
        for i, pos in enumerate(new_positions):
            is_last = (i == len(new_positions) - 1) and not old_positions
            lines.append(_format_single_position(
                pos, new_coins, open_orders, is_last=is_last, show_time=True
            ))

    # پوزیشن‌های قدیمی
    if old_positions:
        lines.append("   📌 پوزیشن‌های قبلی:\n")
        for i, pos in enumerate(old_positions):
            is_last = (i == len(old_positions) - 1)
            lines.append(_format_single_position(
                pos, new_coins, open_orders, is_last=is_last, show_time=False
            ))

    return "".join(lines)


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
    total_positions = 0
    for addr in positions_map:
        total_positions += len(positions_map[addr].get("assetPositions", []))

    header = (
        f"🚀 گزارش کیف پول‌های برتر Hyperliquid\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱️ بازه: {FILLS_HOURS} ساعت گذشته\n"
        f"👥 تعداد کیف پول فعال: {len(records)}\n"
        f"📊 مجموع پوزیشن‌ها: {total_positions}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    lines = [header]

    for i, r in enumerate(records, 1):
        addr = r["address"]
        positions = positions_map.get(addr, {})
        fills = fills_map.get(addr, [])

        month_sign = "🟢" if r["month_roi"] >= 0 else "🔴"
        all_sign = "🏆" if r["all_roi"] >= 0 else "⚠️"

        lines.append(
            f"┏━━━━━━━━━━━━━━━━━━━━━\n"
            f"┃ #{i} · {short_addr(addr, 8)}\n"
            f"┃ 🔗 {addr}\n"
            f"┃\n"
            f"┃ {month_sign} ماهانه: {roi_str(r['month_roi'])}  ·  {pnl_str(r['month_pnl'])}\n"
            f"┃ {all_sign} کل دوره: {roi_str(r['all_roi'])}  ·  {pnl_str(r['all_pnl'])}\n"
            f"┃\n"
            f"{format_positions(positions, fills)}"
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

        # ابتدا fills را چک کن (سبک‌تر و سریع‌تر)
        fills = fetch_recent_fills(addr, hours=FILLS_HOURS)
        time.sleep(SLEEP_BETWEEN_REQUESTS)

        # فیلتر: فقط کسانی که در بازه اخیر پوزیشن جدید باز کرده‌اند
        if not has_new_open_positions(fills):
            continue

        # حالا پوزیشن‌های باز + سفارش‌های باز (TP/SL) را بگیر
        positions = fetch_positions(addr)
        time.sleep(SLEEP_BETWEEN_REQUESTS)

        positions_map[addr] = positions
        fills_map[addr] = fills
        filtered.append(r)

    print(f"تعداد کیف پول با معامله باز در {FILLS_HOURS} ساعت گذشته: {len(filtered)}")

    if not filtered:
        send_telegram(f"⚠️ هیچ کیف پولی در {FILLS_HOURS} ساعت گذشته معامله باز نکرده است.")
        return

    message = build_message(filtered, positions_map, fills_map)
    send_telegram(message)


if __name__ == "__main__":
    main()
