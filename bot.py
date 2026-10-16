#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
اسکنر لیدربورد Hyperliquid — نسخه امتیازدهی (وزن‌های مساوی)
هر معیار 20 امتیاز | مجموع 100
"""
import os
import sys
import time
import bisect
import json
import datetime as dt
import requests
import ccxt

# ---------------- وزن‌های مساوی (هر کدام 20) ----------------
W_MIN_MO   = 20
W_AVG_ROI  = 20
W_LBANK    = 20
W_MONTHS   = 20
W_WIN_RATE = 20

# ---------------- تنظیمات ----------------
MIN_ACCOUNT_VALUE = 50
AVG_MONTHS = 6
WIN_RATE_TRADES = 100
MIN_BASE = 50
MAX_CANDIDATES = None
TOP_N = 100
DELAY = 0.25
TOP_PER_CHUNK = 800

INFO_URL = "https://api.hyperliquid.xyz/info"
LB_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

CHUNK_INDEX = int(os.environ.get("CHUNK_INDEX", "0"))
CHUNK_TOTAL = int(os.environ.get("CHUNK_TOTAL", "1"))

if os.environ.get("TOP_N"):
    TOP_N = int(os.environ["TOP_N"])

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
                time.sleep(3 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception:
            time.sleep(1.5 * (i + 1))
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
    for c in chunks:
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


# ---------------- ارزهای فیوچرز البانک ----------------
def load_lbank_coins():
    exchange = ccxt.lbank({
        'options': {'defaultType': 'swap'},
        'enableRateLimit': True,
        'timeout': 30000,
    })
    markets = exchange.load_markets()
    coins = set()
    for symbol, market in markets.items():
        if not market:
            continue
        if (market.get('swap') or market.get('future')) and market.get('active'):
            base = market.get('base')
            if base:
                coins.add(base.upper())
    if not coins:
        raise RuntimeError("CCXT لیست ارزهای فیوچرز البانک را برنگرداند")
    log(f"[LBank/CCXT] تعداد ارزهای فیوچرز: {len(coins)}")
    return coins


def coin_on_lbank(c, lbank):
    if ":" in c or c.startswith("@"):
        return False
    c = c.upper()
    if c in lbank:
        return True
    if c.startswith("K") and len(c) > 1:
        base = c[1:]
        if base in lbank or ("1000" + base) in lbank:
            return True
    return False


# ---------------- سود ماهانه ----------------
def val_at(series, ts):
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
        return []
    d = dict(data)
    p = d.get("perpAllTime") or d.get("allTime")
    if not p:
        return []
    pnl = [(int(t), float(v)) for t, v in p.get("pnlHistory", [])]
    av = [(int(t), float(v)) for t, v in p.get("accountValueHistory", [])]
    if len(pnl) < 2 or len(av) < 2:
        return []
    pnl.sort()
    av.sort()
    months = month_starts(pnl[0][0], now)
    res = []
    for s, e in months:
        s_ms, e_ms = int(s.timestamp() * 1000), int(e.timestamp() * 1000)
        p_s = val_at(pnl, s_ms) or 0.0
        p_e = val_at(pnl, e_ms)
        if p_e is None:
            continue
        month_pnl = p_e - p_s
        a_s = val_at(av, s_ms)
        inside = [v for t, v in av if s_ms <= t <= e_ms]
        base = max(a_s or 0.0, sum(inside) / len(inside) if inside else 0.0)
        if base < MIN_BASE:
            continue
        res.append((s.strftime("%Y-%m"), month_pnl / base))
    return res


# ---------------- fills ----------------
def get_fills_6mo(addr, now):
    start_ms = int((now - dt.timedelta(days=183)).timestamp() * 1000)
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


def win_rate_from_fills(fills):
    closed = [f for f in fills if abs(float(f.get("closedPnl", 0) or 0)) > 0]
    if not closed:
        return None, 0
    closed.sort(key=lambda f: int(f["time"]))
    if len(closed) > WIN_RATE_TRADES:
        closed = closed[-WIN_RATE_TRADES:]
    total = len(closed)
    wins = sum(1 for f in closed if float(f["closedPnl"]) > 0)
    return wins / total, total


def lbank_share_from_fills(fills, lbank):
    coins = {f["coin"] for f in fills if "coin" in f}
    if not coins:
        return 0.0, 0, 0
    ok = sum(1 for c in coins if coin_on_lbank(c, lbank))
    return ok / len(coins), ok, len(coins)


# ---------------- امتیازدهی (هر معیار 0..20) ----------------
def score_min_month(r):
    """کمترین ماه: -5%→0 | 0%→8 | +5%→17 | +10%+→20"""
    if r >= 0.10:  return W_MIN_MO
    if r <= -0.05: return 0.0
    if r < 0:      return (r + 0.05) / 0.05 * 8
    if r < 0.05:   return 8 + (r / 0.05) * (17 - 8)
    return 17 + ((r - 0.05) / 0.05) * (20 - 17)


def score_avg_roi(r):
    """میانگین ۶ ماه: 0%→0 | 20%+→20"""
    if r is None or r <= 0: return 0.0
    if r >= 0.20:           return W_AVG_ROI
    return (r / 0.20) * W_AVG_ROI


def score_lbank(share):
    """سهم البانک: <50%→0 | 90%+→20"""
    if share >= 0.90: return W_LBANK
    if share <= 0.50: return 0.0
    return (share - 0.50) / 0.40 * W_LBANK


def score_months(m):
    """سابقه: <3 ماه→0 | 6+ ماه→20"""
    if m >= 6: return W_MONTHS
    if m <= 3: return 0.0
    return (m - 3) / 3 * W_MONTHS


def score_win_rate(wr):
    """وین ریت: <30%→0 | 100%→20"""
    if wr is None or wr <= 0.30: return 0.0
    if wr >= 1.0:                return W_WIN_RATE
    return (wr - 0.30) / 0.70 * W_WIN_RATE


def compute_score(wr, avg_roi, min_roi, months, share):
    return (
        score_min_month(min_roi) +
        score_avg_roi(avg_roi) +
        score_lbank(share) +
        score_months(months) +
        score_win_rate(wr)
    )


# ---------------- ارسال نتایج ----------------
def send_results(passed, now):
    if not passed:
        tg_send("🔎 اسکن Hyperliquid تمام شد؛ هیچ تریدری امتیاز نگرفت.")
        return
    head = (f"🏆 نتایج اسکن Hyperliquid ({now:%Y-%m-%d})\n"
            f"تعداد کل امتیازگرفته: {len(passed)}\n"
            f"نمایش {min(TOP_N, len(passed))} نتیجه برتر\n")
    lines = [head]
    for n, t in enumerate(passed[:TOP_N], 1):
        months_txt = " | ".join(f"{m[5:]}: {x*100:.0f}%" for m, x in t["last"]) or "—"
        lines.append(
            f"{n}) <b>امتیاز: {t['score']:.1f}</b> | میانگین ۶ ماه: {t['avg']*100:.1f}%\n"
            f"<code>{t['addr']}</code>\n"
            f"سابقه: {t['months']} ماه | کمترین ماه: {t['min']*100:.1f}%\n"
            f"وین ریت {t['win_n']} معامله اخیر: {t['win_rate']*100:.1f}%\n"
            f"ارز البانک: {t['ok']}/{t['total']} ({t['share']*100:.0f}%)\n"
            f"ماه‌ها: {months_txt}\n"
        )
    tg_send("\n".join(lines))


# ---------------- اجرای اصلی ----------------
def main():
    now = dt.datetime.now(dt.timezone.utc)
    lbank = load_lbank_coins()
    log(f"ارزهای البانک: {len(lbank)}")

    rows = S.get(LB_URL, timeout=120).json()["leaderboardRows"]
    log(f"تعداد کل تریدرها در لیدربورد: {len(rows)}")

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

    if CHUNK_TOTAL > 1:
        cands = cands[CHUNK_INDEX::CHUNK_TOTAL]
        log(f"[Chunk {CHUNK_INDEX}/{CHUNK_TOTAL}] تعداد کاندید: {len(cands)}")
    else:
        log(f"کاندیدا برای بررسی: {len(cands)}")

    scored = []
    for i, addr in enumerate(cands, 1):
        if i % 100 == 0:
            log(f"  بررسی {i}/{len(cands)} | امتیازگرفته: {len(scored)}")

        fills = get_fills_6mo(addr, now)
        if not fills:
            continue

        wr, wr_n = win_rate_from_fills(fills)
        share, ok, total_coins = lbank_share_from_fills(fills, lbank)

        rois = monthly_rois(addr, now)
        if rois:
            last = [x for _, x in rois[-AVG_MONTHS:]]
            avg_roi = sum(last) / len(last)
            min_roi = min(x for _, x in rois)
            months = len(rois)
            last_months = rois[-AVG_MONTHS:]
        else:
            avg_roi = 0.0
            min_roi = 0.0
            months = 0
            last_months = []

        score = compute_score(wr, avg_roi, min_roi, months, share)

        scored.append({
            "addr": addr,
            "score": round(score, 2),
            "win_rate": wr if wr is not None else 0.0,
            "win_n": wr_n,
            "avg": avg_roi,
            "min": min_roi,
            "months": months,
            "share": share,
            "ok": ok,
            "total": total_coins,
            "last": last_months,
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    top = scored[:TOP_PER_CHUNK]
    log(f"امتیاز گرفته: {len(scored)} | ذخیره top {len(top)}")

    out_file = f"results_{CHUNK_INDEX}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(top, f, ensure_ascii=False)
    log(f"ذخیره شد: {out_file}")

    if CHUNK_TOTAL == 1:
        send_results(scored, now)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("ERROR:", e)
        if CHUNK_TOTAL == 1:
            tg_send(f"❌ خطا در اسکنر: {e}")
        sys.exit(1)
