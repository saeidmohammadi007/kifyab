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
LOOKAHEAD    = 100              # تعداد کندل ۴ساعته‌ی بعدی

# اهداف و ریزش‌ها (چند هدف × چند ریزش)
TARGETS       = [20.0, 50.0]
DD_THRESHOLDS = [2.0, 3.0]

# KDTree
K_NEIGHBORS      = 100
MIN_NEIGHBORS    = 50
MIN_HIT_RATE     = 20.0          # حداقل نرخ برای گزارش
EXCLUDE_TEMPORAL = 30            # حذف همسایه‌ی هم‌نماد با فاصله‌ی زمانی کم
DEDUP_DIST       = 0.01
TOP_PATTERNS     = 25


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


def analyze_forward(entry_idx, o, h, l, c, n, lookahead):
    """تحلیل فوروارد چند-هدفی/چند-ریزشی"""
    if entry_idx + 1 + lookahead > n:
        return None
    entry_price = float(c[entry_idx])
    if entry_price <= 0:
        return None
    end = entry_idx + 1 + lookahead
    fut_c = c[entry_idx + 1:end]
    fut_h = h[entry_idx + 1:end]
    fut_l = l[entry_idx + 1:end]
    if len(fut_c) == 0:
        return None

    final_price = float(fut_c[-1])
    total_ret = (final_price - entry_price) / entry_price * 100.0

    out = {'ret': total_ret}
    for T in TARGETS:
        target_price = entry_price * (1 + T / 100.0)
        hits = np.where(fut_h >= target_price)[0]
        if len(hits) == 0:
            out[f'hit_{T}'] = False
            for D in DD_THRESHOLDS:
                out[f'hit_{T}_dd{D}'] = False
        else:
            out[f'hit_{T}'] = True
            k = int(hits[0])
            dd = (float(np.min(fut_l[:k + 1])) - entry_price) / entry_price * 100.0
            for D in DD_THRESHOLDS:
                out[f'hit_{T}_dd{D}'] = dd >= -D
    return out


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
def build_report(results, baseline, win_base, scan_stats):
    lines = []
    lines.append("🔍 <b>اسکن الگوهای کندلی ۴ساعته (KDTree)</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"🎯 اهداف: <code>{', '.join(f'+{T:.0f}٪' for T in TARGETS)}</code>")
    lines.append(f"📉 ریزش‌های مجاز: "
                 f"<code>{', '.join(f'{D:.0f}٪' for D in DD_THRESHOLDS)}</code>")
    lines.append(f"🧮 نمونه‌ها: <code>{scan_stats['n_samples']}</code> | "
                 f"K همسایه: <code>{K_NEIGHBORS}</code>")
    lines.append("")

    # نرخ پایه
    lines.append("📊 <b>نرخ پایه (baseline):</b>")
    lines.append(f"├ سوددهی کلی: <code>{win_base:.1f}٪</code>")
    for T in TARGETS:
        for D in DD_THRESHOLDS:
            v = baseline[(T, D)]
            lines.append(f"├ +{T:.0f}٪ | DD≤{D:.0f}٪: <code>{v:.1f}٪</code>")
    lines.append("")

    if not results:
        lines.append("❌ هیچ الگویی با نرخ موفقیت مورد نیاز پیدا نشد.")
        return "\n".join(lines)

    lines.append(f"🏆 <b>الگوهای برتر (edge نسبت به پایه):</b>")
    lines.append("")

    for i, r in enumerate(results, 1):
        lines.append(f"<b>#{i} — {r['symbol']} @ {r['date']}</b>")
        lines.append(f"   🔎 n=<code>{r['n_neighbors']}</code> | "
                     f"سوددهی <code>{r['win_rate']:.1f}٪</code> "
                     f"(پایه {win_base:.1f}٪ | "
                     f"edge <code>{r['win_rate']-win_base:+.1f}٪</code>)")
        # جدول اهداف
        for T in TARGETS:
            for D in DD_THRESHOLDS:
                hit = r[f'rate_{T}_dd{D}']
                base = baseline[(T, D)]
                edge = hit - base
                lines.append(
                    f"   ├ +{T:.0f}٪ | DD≤{D:.0f}٪: "
                    f"<code>{hit:.1f}٪</code> "
                    f"(edge <code>{edge:+.1f}٪</code>)"
                )
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
    log("🚀 اسکن الگوهای کندلی ۴ساعته با تحلیل چند-هدفی")
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

    # ۲) ساخت دیتاست
    all_feats    = []
    all_meta     = []          # (symbol, candle_idx)
    all_outcomes = []
    symbol_index = {}

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
            out = analyze_forward(i, o, h, l, c, n, LOOKAHEAD)
            if out is None:
                continue
            all_feats.append(feat)
            all_meta.append((sym, i))
            all_outcomes.append(out)

    feats = np.array(all_feats, dtype=float)
    n_samples = len(feats)
    log(f"\n📊 کل نمونه‌ها: {n_samples}")
    if n_samples < MIN_NEIGHBORS * 2:
        raise RuntimeError("نمونه‌ها برای تحلیل کافی نیستند.")

    # ۳) نرخ پایه
    win_base = sum(1 for o in all_outcomes if o['ret'] > 0) / n_samples * 100.0
    baseline = {}
    for T in TARGETS:
        for D in DD_THRESHOLDS:
            cnt = sum(1 for o in all_outcomes if o[f'hit_{T}_dd{D}'])
            baseline[(T, D)] = cnt / n_samples * 100.0

    log(f"📊 سوددهی پایه: {win_base:.2f}٪")
    for (T, D), v in baseline.items():
        log(f"   +{T:.0f}٪ / DD≤{D:.0f}٪: {v:.2f}٪")

    # ۴) KDTree
    log("\n🌳 ساخت KDTree...")
    tree = cKDTree(feats)
    k = min(K_NEIGHBORS + 1, n_samples)
    log(f"🔍 جستجوی {k-1} همسایه برای هر نمونه...")
    dists, idxs = tree.query(feats, k=k)

    # ۵) امتیازدهی
    log("\n🎯 امتیازدهی نمونه‌ها...")
    candidates = []

    for i in range(n_samples):
        sym_i, ci = all_meta[i]
        valid = []
        for j in idxs[i]:
            j = int(j)
            if j == i:
                continue
            sym_j, cj = all_meta[j]
            if sym_j == sym_i and abs(cj - ci) <= EXCLUDE_TEMPORAL:
                continue
            valid.append(j)

        if len(valid) < MIN_NEIGHBORS:
            continue

        nb_out = [all_outcomes[j] for j in valid]
        n_nb = len(nb_out)

        win_rate = sum(1 for o in nb_out if o['ret'] > 0) / n_nb * 100.0

        # نرخ‌های هر ترکیب هدف/ریزش
        rates = {}
        for T in TARGETS:
            for D in DD_THRESHOLDS:
                cnt = sum(1 for o in nb_out if o[f'hit_{T}_dd{D}'])
                rates[(T, D)] = cnt / n_nb * 100.0

        # شرط گزارش: حداقل برای یکی از ترکیب‌ها نرخ بالای آستانه
        best_rate = max(rates.values())
        if best_rate < MIN_HIT_RATE:
            continue

        cand = {
            'idx':         i,
            'symbol':      sym_i,
            'candle_idx':  ci,
            'feat':        feats[i],
            'win_rate':    win_rate,
            'n_neighbors': n_nb,
            'best_rate':   best_rate,
        }
        for (T, D), v in rates.items():
            cand[f'rate_{T}_dd{D}'] = v
        candidates.append(cand)

    log(f"🎯 کاندیدهای اولیه: {len(candidates)}")
    if not candidates:
        report = build_report([], baseline, win_base,
                              {'n_samples': n_samples})
        send_message(report)
        return

    # ۶) حذف تکراری‌ها
    candidates.sort(key=lambda x: -x['best_rate'])
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

    # ۷) افزودن تاریخ
    results = []
    for r in picked:
        sym = r['symbol']
        dt = symbol_index[sym][r['candle_idx']]
        if getattr(dt, 'tz', None) is not None:
            dt = dt.tz_localize(None)
        r['date'] = dt.strftime('%Y-%m-%d %H:%M')
        results.append(r)
        log(f"  ✅ {sym} @ {r['date']} | "
            f"win={r['win_rate']:.1f}٪ | "
            f"best={r['best_rate']:.1f}٪ | n={r['n_neighbors']}")

    # ۸) گزارش و ارسال
    report = build_report(results, baseline, win_base,
                          {'n_samples': n_samples})
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
