#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
اسکنر لیدربورد Hyperliquid — نسخه نهایی (v3.5)
- گیت سابقه (حداقل ۴ ماه)
- گیت معاملات بسته (حداقل ۳۰ سفارش در ۶ ماه)
- گیت بلواپ (حذف ماه بدتر از -۲۰٪)
- میانگین حسابی ROI (جمع / تعداد)
- شرط حداقل تنوع ارز برای معیار البانک
- ضریب سابقه نهایی
- نمایش Max Drawdown و پایداری در خروجی
- وین‌ریت بر اساس oid — فقط نمایشی، بدون اثر در امتیاز
- امتیاز نهایی نرمال‌سازی‌شده به مقیاس ۱۰۰
- aggregateByTime=False
"""
import os
import sys
import time
import bisect
import json
import math
import datetime as dt
from collections import defaultdict
import requests
import ccxt

# ---------------- وزن‌های پایه (جمع = ۸۰، سپس به ۱۰۰ نرمال می‌شود) ----------------
W_MIN_MO   = 20
W_AVG_ROI  = 20
W_LBANK    = 20
W_MONTHS   = 20

# ضریب نرمال‌سازی به مقیاس ۱۰۰ (۱۰۰ / ۸۰ = ۱.۲۵)
SCALE_FACTOR = 100.0 / (W_MIN_MO + W_AVG_ROI + W_LBANK + W_MONTHS)

# ---------------- گیت‌ها و آستانه‌ها ----------------
MIN_ACCOUNT_VALUE   = 50
AVG_MONTHS          = 6
WIN_RATE_TRADES     = 100   # فقط برای نمایش وین‌ریت

MIN_MONTHS_REQUIRED = 4
MIN_CLOSED_TRADES   = 30
MIN_COINS_FOR_LBANK = 3
BLOWUP_THRESHOLD    = -0.20

MIN_BASE            = 50
MAX_CANDIDATES      = None
TOP_N               = 100
DELAY               = 0.25
TOP_PER_CHUNK       = 800

HISTORY_FULL_MONTHS = 6

INFO_URL = "https://api.hyperliquid.xyz/info"
LB_URL   = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "")

CHUNK_INDEX = int(os.environ.get("CHUNK_INDEX", "0"))
CHUNK_TOTAL = int(os.environ.get("CHUNK_TOTAL", "1"))

if os.environ.get("TOP_N"):
    TOP_N = int(os.environ["TOP_N"])

S = requests.Session()
S.headers.update({"User-Agent": "hl-scanner/3.5"})


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
    av  = [(int(t), float(v)) for t, v in p.get("accountValueHistory", [])]
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
    """aggregateByTime=False برای دریافت fillهای خام با oid."""
    start_ms = int((now - dt.timedelta(days=183)).timestamp() * 1000)
    out, t = [], start_ms
    for _ in range(6):
        d = hl_post({"type": "userFillsByTime", "user": addr,
                     "startTime": t, "aggregateByTime": False})
        time.sleep(DELAY)
        if not d:
            break
        out += d
        if len(d) < 2000:
            break
        t = max(int(f["time"]) for f in d) + 1
    return out


def win_rate_by_oid(fills):
    """
    وین‌ریت بر اساس گروه‌بندی fillها روی oid (شناسه سفارش).
    هر سفارش = یک معامله، حتی اگر در چند fill پر شده باشد.
    خروجی فقط برای نمایش استفاده می‌شود.
    """
    orders = defaultdict(lambda: {
        "pnl": 0.0,
        "has_closed": False,
        "time": 0,
    })

    for f in fills:
        oid = f.get("oid")
        if oid is None:
            oid = f.get("hash") or f.get("time")
            if oid is None:
                continue

        pnl = float(f.get("closedPnl", 0) or 0)
        o = orders[oid]
        o["time"] = max(o["time"], int(f.get("time", 0)))
        if abs(pnl) > 0:
            o["pnl"] += pnl
            o["has_closed"] = True

    closed = [o for o in orders.values() if o["has_closed"]]
    if not closed:
        return None, 0, 0

    closed.sort(key=lambda x: x["time"])
    total_closed = len(closed)
    used = closed[-WIN_RATE_TRADES:] if len(closed) > WIN_RATE_TRADES else closed
    n = len(used)
    wins = sum(1 for o in used if o["pnl"] > 0)
    return wins / n, n, total_closed


def lbank_share_from_fills(fills, lbank):
    coins = {f["coin"] for f in fills if "coin" in f}
    if not coins:
        return 0.0, 0, 0
    ok = sum(1 for c in coins if coin_on_lbank(c, lbank))
    return ok / len(coins), ok, len(coins)


# ---------------- متریک‌های اضافی ----------------
def arith_mean_roi(rois_pairs):
    """میانگین حسابی: جمع همه ROIها تقسیم بر تعداد."""
    vals = [r for _, r in rois_pairs]
    if not vals:
        return 0.0
    return sum(vals) / len(vals)


def compute_drawdown(rois_pairs):
    if not rois_pairs:
        return 0.0
    equity, peak, max_dd = 1.0, 1.0, 0.0
    for _, r in rois_pairs:
        equity *= (1 + r)
        if equity > peak:
            peak = equity
        if peak > 0:
            dd = (peak - equity) / peak
            if dd > max_dd:
                max_dd = dd
    return max_dd


def compute_stability(rois_pairs):
    vals = [r for _, r in rois_pairs]
    if len(vals) < 2:
        return 0.0
    mean = sum(vals) / len(vals)
    var  = sum((v - mean) ** 2 for v in vals) / len(vals)
    std  = var ** 0.5
    if std < 1e-9:
        return 0.0
    return mean / std


# ---------------- امتیازدهی (بدون وین‌ریت، مقیاس ۱۰۰) ----------------
def score_min_month(r):
    if r <= -0.20:  return -25.0
    if r <= -0.10:  return -15 + (r + 0.20) * 150
    if r <  0:      return (r + 0.10) / 0.10 * 8
    if r <  0.05:   return 8 + (r / 0.05) * 9
    if r <  0.10:   return 17 + ((r - 0.05) / 0.05) * 3
    return float(W_MIN_MO)


def score_avg_roi(r):
    if r is None or r <= 0: return 0.0
    if r >= 0.20:           return float(W_AVG_ROI)
    return (r / 0.20) * W_AVG_ROI


def score_lbank(share, n_coins):
    if n_coins < MIN_COINS_FOR_LBANK:
        return 0.0
    if share >= 0.90: return float(W_LBANK)
    if share <= 0.50: return 0.0
    return (share - 0.50) / 0.40 * W_LBANK


def score_months(m):
    if m >= 6: return float(W_MONTHS)
    if m <= 4: return 0.0
    return (m - 4) / 2.0 * W_MONTHS


def history_factor(months):
    return min(months / HISTORY_FULL_MONTHS, 1.0)


def compute_score(avg_roi, min_roi, months, share, n_coins):
    """
    امتیاز نهایی — بدون وین‌ریت، مقیاس ۱۰۰.
    - جمع خام اجزا: ۴×۲۰ = ۸۰
    - ضرب در SCALE_FACTOR (۱.۲۵) → مقیاس ۱۰۰
    - ضرب در ضریب سابقه (۰..۱)
    """
    components = (
        score_min_month(min_roi) +
        score_avg_roi(avg_roi) +
        score_lbank(share, n_coins) +
        score_months(months)
    )
    hf = history_factor(months)
    scaled = components * SCALE_FACTOR
    if scaled > 0:
        return scaled * hf
    return scaled


# ---------------- ارسال نتایج ----------------
def send_results(passed, stats, now):
    if not passed:
        tg_send(
            "🔎 اسکن Hyperliquid تمام شد.\n"
            f"کاندید: {stats['cands']}\n"
            f"رد در گیت: {stats['rejected']}\n"
            f"امتیازگرفته: 0\n"
        )
        return

    head = (
        f"🏆 نتایج اسکن Hyperliquid ({now:%Y-%m-%d})\n"
        f"کاندید: {stats['cands']} | رد در گیت: {stats['rejected']} | "
        f"امتیازگرفته: {stats['scored']}\n"
        f"نمایش {min(TOP_N, len(passed))} نتیجه برتر (حداکثر امتیاز: ۱۰۰)\n"
    )
    lines = [head]
    for n, t in enumerate(passed[:TOP_N], 1):
        months_txt = " | ".join(f"{m[5:]}: {x*100:.0f}%" for m, x in t["last"]) or "—"
        diversity_warn = " ⚠️ کم‌تنوع" if t["total"] < MIN_COINS_FOR_LBANK else ""
        lines.append(
            f"{n}) <b>امتیاز: {t['score']:.1f}</b> | "
            f"میانگین ۶ ماه (حسابی): {t['avg']*100:.1f}%\n"
            f"<code>{t['addr']}</code>\n"
            f"سابقه: {t['months']} ماه (ضریب {t['hist_factor']:.2f}) | "
            f"کمترین ماه: {t['min']*100:.1f}%\n"
            f"وین ریت {t['win_n']}/{t['win_total']} سفارش: {t['win_rate']*100:.1f}% (فقط نمایشی)\n"
            f"ارز البانک: {t['ok']}/{t['total']} ({t['share']*100:.0f}%){diversity_warn}\n"
            f"حداکثر افت: {t['drawdown']*100:.1f}% | پایداری: {t['stability']:.2f}\n"
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
    rejected = 0
    reject_reasons = {
        "no_fills": 0,
        "few_trades": 0,
        "no_rois": 0,
        "few_months": 0,
        "blowup": 0,
    }

    for i, addr in enumerate(cands, 1):
        if i % 100 == 0:
            log(f"  بررسی {i}/{len(cands)} | "
                f"امتیازگرفته: {len(scored)} | رد: {rejected}")

        fills = get_fills_6mo(addr, now)
        if not fills:
            rejected += 1
            reject_reasons["no_fills"] += 1
            continue

        # وین‌ریت — فقط برای نمایش
        wr, wr_used, wr_closed = win_rate_by_oid(fills)
        share, ok, total_coins = lbank_share_from_fills(fills, lbank)

        # گیت معاملات
        if wr_closed < MIN_CLOSED_TRADES:
            rejected += 1
            reject_reasons["few_trades"] += 1
            continue

        rois = monthly_rois(addr, now)
        if not rois:
            rejected += 1
            reject_reasons["no_rois"] += 1
            continue

        months = len(rois)

        if months < MIN_MONTHS_REQUIRED:
            rejected += 1
            reject_reasons["few_months"] += 1
            continue

        min_roi = min(x for _, x in rois)

        if min_roi < BLOWUP_THRESHOLD:
            rejected += 1
            reject_reasons["blowup"] += 1
            continue

        last_months = rois[-AVG_MONTHS:]
        avg_roi   = arith_mean_roi(last_months)   # ← میانگین حسابی
        drawdown  = compute_drawdown(rois)
        stability = compute_stability(rois)

        # ⚠️ وین‌ریت ورودی امتیاز نیست
        score = compute_score(
            avg_roi, min_roi, months, share, total_coins
        )

        scored.append({
            "addr": addr,
            "score": round(score, 2),
            "win_rate": wr if wr is not None else 0.0,
            "win_n": wr_used,
            "win_total": wr_closed,
            "avg": avg_roi,
            "min": min_roi,
            "months": months,
            "hist_factor": history_factor(months),
            "share": share,
            "ok": ok,
            "total": total_coins,
            "drawdown": drawdown,
            "stability": stability,
            "last": last_months,
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    top = scored[:TOP_PER_CHUNK]
    log(f"امتیاز گرفته: {len(scored)} | ذخیره top {len(top)} | رد در گیت: {rejected}")
    log(f"دلایل رد: {reject_reasons}")

    out_file = f"results_{CHUNK_INDEX}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(top, f, ensure_ascii=False)
    log(f"ذخیره شد: {out_file}")

    if CHUNK_TOTAL == 1:
        stats = {
            "cands": len(cands),
            "rejected": rejected,
            "scored": len(scored),
        }
        send_results(scored, stats, now)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("ERROR:", e)
        if CHUNK_TOTAL == 1:
            tg_send(f"❌ خطا در اسکنر: {e}")
        sys.exit(1)
