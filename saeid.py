import os
import sys
import traceback

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from scipy.spatial import cKDTree


def log(m):
    print(m, flush=True)


# ==================== تنظیمات ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()

SYMBOLS = [
    "LUMIA-USD", "API3-USD", "FIL-USD",
    "DOGE-USD", "ONDO-USD", "SHIB-USD",
    "BLESS-USD", "ROSE-USD", "LINEA-USD", "JTO-USD",
]

SEARCH_PERIOD_HOURLY = "730d"
LOOKAHEAD     = 100      # کندل ۴ساعته‌ی بعدی که بررسی می‌شود
TARGET_PCT    = 20.0     # هدف سود (درصد)
DD_LIMIT      = 3.0      # حداکثر ریزش مجاز قبل از رسیدن به هدف (درصد)

K_NEIGHBORS   = 100      # تعداد همسایه‌های نزدیک که بررسی می‌شوند
MIN_NEIGHBORS = 50       # حداقل همسایه برای اعتبار
MIN_HIT_RATE  = 20.0     # حداقل درصد موفقیت برای گزارش
EXCLUDE_TEMPORAL = 30    # حذف همسایه‌های زمانی نزدیک (نشتی داده)
DEDUP_DIST    = 0.01     # آستانه‌ی حذف الگوهای تکراری در فضای ویژگی
TOP_PATTERNS  = 25       # چند الگوی برتر گزارش شود


# ==================== توابع ====================
def fetch_4h(symbol):
    log(f"📥 دریافت {symbol} (1h→4h)")
    try:
        df = yf.download(symbol, interval="1h", period=SEARCH_PERIOD_HOURLY,
                         progress=False, auto_adjust=True, timeout=60)
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df[['Open', 'High', 'Low', 'Close']].dropna()
        if df.empty:
            return None
        df_4h = df.resample('4h').agg({
            'Open':  'first',
            'High':  'max',
            'Low':   'min',
            'Close': 'last',
        }).dropna()
        return df_4h if not df_4h.empty else None
    except Exception as e:
        log(f"❌ fetch_4h {symbol}: {e}")
        return None


def candle_features(o, h, l, c, i):
    op, hi, lo, cl = o[i], h[i], l[i], c[i]
    if op <= 0:
        return None
    body        = (cl - op) / op
    upper_wick  = (hi - max(op, cl)) / op
    lower_wick  = (min(op, cl) - lo) / op
    total_range = (hi - lo) / op
    return np.array([body, upper_wick, lower_wick, total_range], dtype=float)


def forward_outcome(o, h, l, c, i, lookahead, target_pct, dd_limit):
    """
    1 اگر هدف با ریزش کمتر از dd_limit خورد،
    0 اگر هدف نخورد یا با ریزش بیشتر خورد،
    None اگر دیتای کافی برای آینده نبود.
    """
    n = len(c)
    if i + 1 + lookahead > n:
        return None
    entry = float(c[i])
    if entry <= 0:
        return None
    end = i + 1 + lookahead
    fut_h = h[i + 1:end]
    fut_l = l[i + 1:end]
    if len(fut_h) == 0:
        return None

    target_price = entry * (1.0 + target_pct / 100.0)
    hits = np.where(fut_h >= target_price)[0]
    if len(hits) == 0:
        return 0
    k = int(hits[0])
    min_low = float(np.min(fut_l[:k + 1]))
    dd = (min_low - entry) / entry * 100.0     # منفی
    return 1 if dd >= -dd_limit else 0


# ==================== تلگرام ====================
def send_message(text, chunk_size=3500):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        log("⚠️ توکن یا chat_id موجود نیست.")
        return
    lines = text.split("\n")
    chunks, cur = [], ""
    for ln in lines:
        if len(cur) + len(ln) + 1 > chunk_size and cur:
            chunks.append(cur)
            cur = ln
        else:
            cur = (cur + "\n" + ln) if cur else ln
    if cur:
        chunks.append(cur)

    for i, chunk in enumerate(chunks, 1):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk,
                      "parse_mode": "HTML"},
                timeout=30,
            )
            log(f"📤 telegram part {i}/{len(chunks)}: {r.status_code}")
            if r.status_code != 200:
                log(f"   err: {r.text[:200]}")
        except Exception as e:
            log(f"❌ send part {i}: {e}")


# ==================== گزارش ====================
def build_report(results, base_rate):
    lines = []
    lines.append("🔍 <b>اسکن الگوهای کندلی ۴ساعته</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"🎯 هدف: <b>+{TARGET_PCT:.0f}٪</b> با ریزش کمتر از "
                 f"<b>{DD_LIMIT:.0f}٪</b>")
    lines.append(f"📊 نرخ پایه‌ی کل (baseline): <code>{base_rate:.1f}٪</code>")
    lines.append(f"✅ آستانه‌ی گزارش: <code>{MIN_HIT_RATE:.0f}٪</code>")
    lines.append(f"🧮 تعداد الگوهای برتر: <code>{len(results)}</code>")
    lines.append("")

    if not results:
        lines.append("❌ هیچ الگویی با نرخ موفقیت مورد نیاز پیدا نشد.")
        return "\n".join(lines)

    for i, r in enumerate(results, 1):
        edge = r['rate'] - base_rate
        lines.append(f"<b>#{i} — {r['symbol']} @ {r['date']}</b>")
        lines.append(f"   📈 نرخ موفقیت: <code>{r['rate']:.1f}٪</code> "
                     f"({r['n_hits']} از {r['n_neighbors']})")
        lines.append(f"   🎯 برتری نسبت به پایه: <code>{edge:+.1f}٪</code>")
        f = r['feat']
        lines.append(f"   🕯 بدنه <code>{f[0]*100:+.2f}٪</code> | "
                     f"سایه‌بالا <code>{f[1]*100:.2f}٪</code> | "
                     f"سایه‌پایین <code>{f[2]*100:.2f}٪</code> | "
                     f"دامنه <code>{f[3]*100:.2f}٪</code>")
        lines.append("")

    return "\n".join(lines).rstrip()


# ==================== اجرای اصلی ====================
def main():
    log("=" * 60)
    log("🚀 اسکن الگوهای کندلی با نرخ موفقیت بالا")
    log("=" * 60)

    # ۱) داده‌ی ۴ ساعته
    dfs = {}
    for sym in SYMBOLS:
        df = fetch_4h(sym)
        if df is not None and len(df) > LOOKAHEAD + 10:
            dfs[sym] = df

    missing = [s for s in SYMBOLS if s not in dfs]
    if missing:
        log(f"⚠️ بدون داده: {missing}")
    if not dfs:
        raise RuntimeError("هیچ داده‌ای دریافت نشد.")

    # ۲) ساخت دیتاست ویژگی + نتیجه
    all_feats    = []
    all_outcomes = []
    all_meta     = []      # (symbol, candle_idx)
    symbol_index = {}      # برای دسترسی به date

    for sym, df in dfs.items():
        o = df['Open'].values.astype(float)
        h = df['High'].values.astype(float)
        l = df['Low'].values.astype(float)
        c = df['Close'].values.astype(float)
        symbol_index[sym] = df.index
        n = len(df)

        for i in range(n):
            feat = candle_features(o, h, l, c, i)
            if feat is None:
                continue
            outcome = forward_outcome(o, h, l, c, i,
                                      LOOKAHEAD, TARGET_PCT, DD_LIMIT)
            if outcome is None:
                continue
            all_feats.append(feat)
            all_outcomes.append(outcome)
            all_meta.append((sym, i))

    feats    = np.array(all_feats, dtype=float)
    outcomes = np.array(all_outcomes, dtype=int)
    log(f"\n📊 کل نمونه‌ها: {len(feats)}")
    base_rate = float(outcomes.mean() * 100.0)
    log(f"📊 نرخ پایه‌ی موفقیت (baseline): {base_rate:.2f}٪")
    log(f"📊 تعداد موفق‌ها: {int(outcomes.sum())} از {len(outcomes)}")

    if len(feats) < MIN_NEIGHBORS * 2:
        raise RuntimeError("نمونه‌ها برای تحلیل کافی نیستند.")

    # ۳) KDTree
    log("\n🌳 ساخت KDTree...")
    tree = cKDTree(feats)

    k = min(K_NEIGHBORS + 1, len(feats))
    log(f"🔍 جستجوی {k-1} همسایه برای هر نمونه...")
    dists, idxs = tree.query(feats, k=k)

    # ۴) امتیازدهی
    candidates = []
    for i in range(len(feats)):
        sym_i, ci = all_meta[i]
        nb_idx = idxs[i]

        valid = []
        for j in nb_idx:
            if j == i:
                continue
            sym_j, cj = all_meta[j]
            # حذف همسایه‌های هم‌نماد با فاصله‌ی زمانی کم (جلوگیری از نشتی)
            if sym_j == sym_i and abs(cj - ci) <= EXCLUDE_TEMPORAL:
                continue
            valid.append(j)

        if len(valid) < MIN_NEIGHBORS:
            continue

        nb_out = outcomes[valid]
        rate = float(nb_out.mean() * 100.0)

        if rate >= MIN_HIT_RATE:
            candidates.append({
                'idx':         i,
                'symbol':      sym_i,
                'candle_idx':  ci,
                'feat':        feats[i],
                'rate':        rate,
                'n_neighbors': len(valid),
                'n_hits':      int(nb_out.sum()),
            })

    log(f"\n🎯 کاندیدهای اولیه: {len(candidates)}")
    if not candidates:
        report = build_report([], base_rate)
        send_message(report)
        log("⚠️ هیچ الگویی بالای آستانه پیدا نشد.")
        return

    # ۵) حذف تکراری‌ها (نزدیک در فضای ویژگی)
    candidates.sort(key=lambda x: -x['rate'])
    picked = []
    for cand in candidates:
        too_similar = False
        for p in picked:
            if np.linalg.norm(cand['feat'] - p['feat']) < DEDUP_DIST:
                too_similar = True
                break
        if not too_similar:
            picked.append(cand)
        if len(picked) >= TOP_PATTERNS:
            break

    log(f"🏆 الگوهای یکتای نهایی: {len(picked)}")

    # ۶) افزودن تاریخ به نتیجه
    results = []
    for r in picked:
        sym = r['symbol']
        dt  = symbol_index[sym][r['candle_idx']]
        if getattr(dt, 'tz', None) is not None:
            dt = dt.tz_localize(None)
        r['date'] = dt.strftime('%Y-%m-%d %H:%M')
        results.append(r)
        log(f"  ✅ {sym} @ {r['date']} | "
            f"rate={r['rate']:.1f}% | "
            f"n={r['n_neighbors']} | "
            f"edge={r['rate']-base_rate:+.1f}%")

    # ۷) گزارش و ارسال
    report = build_report(results, base_rate)
    plain = (report.replace("<b>", "").replace("</b>", "")
                   .replace("<code>", "").replace("</code>", ""))
    log("\n" + plain)
    send_message(report)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"❌ خطا: {e}")
        traceback.print_exc()
        sys.exit(1)
