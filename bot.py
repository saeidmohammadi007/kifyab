#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
اسکنر لیدربورد Hyperliquid (بدون پیش‌فیلتر)
شرط‌ها:
  1) حداقل 90٪ ارزهایی که تریدر معامله می‌کند در فیوچرز البانک (LBank) باشد
  2) حداقل 6 ماه سابقه
  3) همه ماه‌های کامل حداقل +5٪ (واریز/برداشت وارد سود نمی‌شود؛ فقط PnL محاسبه می‌شود)
  4) وین ریت حداقل 40٪ روی 100 معامله بسته‌شده اخیر
  5) رتبه‌بندی بر اساس میانگین سود 6 ماه اخیر
  6) ارسال 100 آدرس برتر به تلگرام
"""
import os
import sys
import time
import bisect
import datetime as dt
import requests

# ---------------- تنظیمات ----------------
MIN_MONTH_ROI = 0.05        # حداقل سود هر ماه (5٪)
MIN_MONTHS = 6              # حداقل تعداد ماه کامل سابقه
MIN_LBANK_SHARE = 0.90      # حداقل سهم ارزهای موجود در البانک
AVG_MONTHS = 6              # میانگین سود چند ماه اخیر برای رتبه‌بندی
MIN_ACCOUNT_VALUE = 50      # حداقل ارزش حساب (دلار) برای حذف حساب‌های ریز/نویزی
MIN_BASE = 50               # حداقل سرمایه پایه ماه برای محاسبه ROI (هم‌راستا با MIN_ACCOUNT_VALUE)
MAX_CANDIDATES = None       # None = بدون سقف (همه بررسی می‌شوند)
TOP_N = 100                 # تعداد نتایج ارسالی
DELAY = 1.0                 # فاصله بین درخواست‌ها (محدودیت نرخ API)

# ---- شرط وین ریت ----
MIN_WIN_RATE = 0.40         # حداقل وین ریت (40٪)
WIN_RATE_TRADES = 100       # روی چند معامله بسته‌شده اخیر محاسبه شود

INFO_URL = "https://api.hyperliquid.xyz/info"
LB_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
LBANK_URL = "https://lbkperp.lbank.com/cfd/openApi/v1/pub/instrument?productGroup=Swap&_=1"

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

S = requests.Session()
S.headers.update({"User-Agent": "hl-scanner/1.0"})


def log(*a):
    print(*a, flush=True)


# ---------------- شبکه ----------------
def hl_post(payload, retries=6):
    for i in range(retries):
        try:
            r = S.post(INFO_URL, json=payload, timeout=30)
            if r.status_code == 429:
                time.sleep(5 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception:
            time.sleep(2 * (i + 1))
    return None


def tg_send(text):
    if not TG_TOKEN or not TG_CHAT:
        log("[TG] توکن یا چت‌آیدی تنظیم نشده:\n" + text)
        return
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    total = len(chunks)
    for idx, c in enumerate(chunks, 1):
        try:
            S.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": c, "parse_mode": "HTML",
                      "disable_web_page_preview": True},
                timeout=30,
            )
        except Exception as e:
            log("TG error:", e)
        time.sleep(0.5)


# ---------------- ارزهای البانک ----------------
def load_lbank_coins():
    r = S.get(LBANK_URL, timeout=30)
    r.raise_for_status()
    data = r.json().get("data") or []
    coins = set()
    for it in data:
        base = it.get("baseCurrency")
        if not base:
            sym = str(it.get("symbol", ""))
            base = sym[:-4] if sym.endswith("USDT") else sym
        if base:
            coins.add(base.upper())
    if not coins:
        raise RuntimeError("لیست ارزهای البانک خالی بود")
    return coins


def coin_on_lbank(c, lbank):
    # HIP-3 (دارای ':') و اسپات (شروع با '@') در البانک نیست
    if ":" in c or c.startswith("@"):
        return False
    c = c.upper()
    if c in lbank:
        return True
    # نام‌های kPEPE / kBONK در هایپرلیکوید = 1000 برابر
    if c.startswith("K") and len(c) > 1:
        base = c[1:]
        if base in lbank or ("1000" + base) in lbank:
            return True
    return False


# ---------------- سود ماهانه (بدون واریز/برداشت) ----------------
def val_at(series, ts):
    """آخرین مقدار سری در یا قبل از ts؛ اگر قبل از شروع باشد None"""
    times = [p[0] for p in series]
    i = bisect.bisect_right(times, ts) - 1
    return series[i][1] if i >= 0 else None


def month_starts(first_ts_ms, now):
    d0 = dt.datetime.fromtimestamp(first_ts_ms / 1000, dt.timezone.utc)
    if d0.day == 1 and d0.hour == 0:
        start = dt.datetime(d0.year, d0.month, 1, tzinfo=dt.timezone.utc)
    else:
        y, m = (d0.year + 1, 1) if d0.month == 12 else (d0.year, d0.month + 1)
        start = dt.datetime(y, m, 1, tzinfo=dt.timezone.utc)
    end_limit = dt.datetime(now.year, now.month, 1, tzinfo=dt.timezone.utc)
    out = []
    cur = start
    while cur < end_limit:
        y, m = (cur.year + 1, 1) if cur.month == 12 else (cur.year, cur.month + 1)
        nxt = dt.datetime(y, m, 1, tzinfo=dt.timezone.utc)
        out.append((cur, nxt))
        cur = nxt
    return out


def monthly_rois(addr, now):
    data = hl_post({"type": "portfolio", "user": addr})
    time.sleep(DELAY)
    if not data:
        return None
    d = dict(data)
    p = d.get("perpAllTime") or d.get("allTime")
    if not p:
        return None
    pnl = [(int(t), float(v)) for t, v in p.get("pnlHistory", [])]
    av = [(int(t), float(v)) for t, v in p.get("accountValueHistory", [])]
    if len(pnl) < 2 or len(av) < 2:
        return None
    pnl.sort()
    av.sort()
    months = month_starts(pnl[0][0], now)
    res = []
    for s, e in months:
        s_ms, e_ms = int(s.timestamp() * 1000), int(e.timestamp() * 1000)
        p_s = val_at(pnl, s_ms) or 0.0          # قبل از شروع حساب: صفر
        p_e = val_at(pnl, e_ms)
        if p_e is None:
            return None
        month_pnl = p_e - p_s                    # فقط سود/زیان؛ واریز و برداشت در PnL نیست
        a_s = val_at(av, s_ms)
        inside = [v for t, v in av if s_ms <= t <= e_ms]
        cands = ([a_s] if a_s else []) + inside
        if not cands:
            res.append((s.strftime("%Y-%m"), float("-inf")))
            continue
        # پایه محافظه‌کارانه: بیشتر از سرمایه ابتدای ماه و میانگین ماه
        base = max(a_s or 0.0, sum(inside) / len(inside) if inside else 0.0)
        if base < MIN_BASE:
            res.append((s.strftime("%Y-%m"), float("-inf")))
            continue
        res.append((s.strftime("%Y-%m"), month_pnl / base))
    return res


# ---------------- ارزهای معامله‌شده ----------------
def get_fills(addr, start_ms):
    out, t = [], start_ms
    for _ in range(6):
        d = hl_post({"type": "userFillsByTime", "user": addr,
                     "startTime": t, "aggregateByTime": True})
        time.sleep(DELAY)
        if not d:
            break
        out += d
        if len(d) < 2000:
            break
        t = max(int(f["time"]) for f in d) + 1
    return out


def lbank_share(addr, lbank, now):
    start = int((now - dt.timedelta(days=183)).timestamp() * 1000)
    fills = get_fills(addr, start)
    coins = {f["coin"] for f in fills if "coin" in f}
    if not coins:
        return 0.0, 0, 0
    ok = sum(1 for c in coins if coin_on_lbank(c, lbank))
    return ok / len(coins), ok, len(coins)


# ---------------- وین ریت ----------------
def get_win_rate(addr, n=WIN_RATE_TRADES):
    """وین ریت آخرین n معامله بسته‌شده (بر اساس closedPnl != 0)"""
    fills = hl_post({"type": "userFills", "user": addr})
    time.sleep(DELAY)
    if not fills:
        return None, 0
    closed = [f for f in fills if abs(float(f.get("closedPnl", 0) or 0)) > 0]
    if len(closed) < n:
        return None, len(closed)   # معامله بسته‌شده کافی ندارد
    closed.sort(key=lambda f: int(f["time"]))
    closed = closed[-n:]
    wins = sum(1 for f in closed if float(f["closedPnl"]) > 0)
    return wins / n, n


# ---------------- اجرای اصلی ----------------
def main():
    now = dt.datetime.now(dt.timezone.utc)
    lbank = load_lbank_coins()
    log(f"ارزهای البانک: {len(lbank)}")

    rows = S.get(LB_URL, timeout=120).json()["leaderboardRows"]
    log(f"تعداد کل تریدرها در لیدربورد: {len(rows)}")

    # ---- بدون پیش‌فیلتر: همه تریدرها (فقط فیلتر حداقلی ارزش حساب) ----
    cands = []
    for r in rows:
        try:
            av = float(r["accountValue"])
        except Exception:
            continue
        if av >= MIN_ACCOUNT_VALUE:
            cands.append(r["ethAddress"])

    if MAX_CANDIDATES:
        cands = cands[:MAX_CANDIDATES]
    log(f"کاندیدا برای بررسی عمیق (بدون پیش‌فیلتر): {len(cands)}")

    passed = []
    for i, addr in enumerate(cands, 1):
        if i % 50 == 0:
            log(f"  بررسی {i}/{len(cands)} | عبور کرده: {len(passed)}")

        # ---- شرط وین ریت ۱۰۰ معامله اخیر (سریع، اول چک می‌شود) ----
        wr, wr_n = get_win_rate(addr)
        if wr is None or wr < MIN_WIN_RATE:
            continue

        rois = monthly_rois(addr, now)
        if not rois or len(rois) < MIN_MONTHS:
            continue
        if any(x < MIN_MONTH_ROI for _, x in rois):
            continue

        share, ok, total = lbank_share(addr, lbank, now)
        if share < MIN_LBANK_SHARE:
            continue

        last = [x for _, x in rois[-AVG_MONTHS:]]
        passed.append({
            "addr": addr,
            "avg": sum(last) / len(last),
            "months": len(rois),
            "min": min(x for _, x in rois),
            "share": share, "ok": ok, "total": total,
            "win_rate": wr, "win_n": wr_n,
            "last": rois[-AVG_MONTHS:],
        })

    passed.sort(key=lambda x: x["avg"], reverse=True)
    log(f"تعداد نهایی: {len(passed)}")

    if not passed:
        tg_send("🔎 اسکن Hyperliquid تمام شد؛ هیچ تریدری با همه شرط‌ها پیدا نشد.")
        return

    head = (f"🏆 نتایج اسکن Hyperliquid ({now:%Y-%m-%d})\n"
            f"تعداد تریدر پیدا شده: {len(passed)}\n"
            f"نمایش {min(TOP_N, len(passed))} نتیجه برتر\n")
    lines = [head]
    for n, t in enumerate(passed[:TOP_N], 1):
        months_txt = " | ".join(f"{m[5:]}: {x*100:.0f}%" for m, x in t["last"])
        lines.append(
            f"{n}) میانگین سود ۶ ماه: <b>{t['avg']*100:.1f}%</b>\n"
            f"<code>{t['addr']}</code>\n"
            f"سابقه: {t['months']} ماه | کمترین ماه: {t['min']*100:.1f}%\n"
            f"وین ریت {t['win_n']} معامله اخیر: <b>{t['win_rate']*100:.1f}%</b>\n"
            f"ارز البانک: {t['ok']}/{t['total']} ({t['share']*100:.0f}%)\n"
            f"ماه‌ها: {months_txt}\n"
        )
    tg_send("\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("ERROR:", e)
        tg_send(f"❌ خطا در اسکنر: {e}")
        sys.exit(1)
