import os
import sys
import traceback

import numpy as np
import pandas as pd
import requests
import yfinance as yf


def log(m):
    print(m, flush=True)


# ==================== تنظیمات ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()

TARGET_SYMBOL = "API3-USD"

# ─── داده ───
SEARCH_PERIOD = "730d"
INTERVAL_SRC  = "1h"
RESAMPLE_RULE = "4h"
PATTERN_LEN   = 1

SYMBOLS = [
    "API3-USD",
    "BTC-USD", "ETH-USD", "LTC-USD", "XRP-USD",
    "XMR-USD", "ZEC-USD", "DASH-USD", "ETC-USD",
    "XLM-USD", "REP-USD",
]

# ─── بازه‌ی کاندیدها ───
MIN_CANDIDATE_DATE = "2024-10-01"
MAX_CANDIDATE_DATE = "2026-12-31"

EXCLUDE_EXACT_TARGET_CANDLE     = True
RESTRICT_CANDIDATES_BEFORE_TARGET = False   # True = بدون look-ahead

# ─── حالت ورود ───
ENTRY_MODE = "next_open"

# ─── پارامترهای جستجو ───
LOOKAHEAD              = 42
K_PER_SYMBOL           = 20
TOTAL_TOP              = 100
EXCLUDE_TEMPORAL_HOURS = 168

TARGETS       = [2.0, 4.0, 20.0, 50.0]
DD_THRESHOLDS = [1.0, 2.0, 3.0]

# 🎯 ریزش کاملاً زیر ۱٪ (نه مساوی) برای DD=1
STRICT_DD1 = True

# ─── اسکن و رتبه‌بندی ───
RANK_BY       = "hit_4_dd1"   # "hit_4_dd1" | "hit_2_dd1" | "exp_val" | "edge" | "win_pct"
TOP_N_TO_SEND = 5

MAX_TARGETS_TO_SCAN = None    # None = همه
MIN_PATTERNS        = 10
ONE_CANDIDATE_PER_WEEK = True


# ==================== دریافت داده ====================
def fetch_4h(symbol):
    log(f"📥 دریافت 4h {symbol}")
    try:
        df = yf.download(symbol, interval=INTERVAL_SRC, period=SEARCH_PERIOD,
                         progress=False, auto_adjust=True, timeout=60)
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df[["Open", "High", "Low", "Close"]].dropna()
        if df.empty:
            return None

        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)
        if df.index.tz is not None:
            df.index = df.index.tz_convert("UTC").tz_localize(None)
        df.index = df.index.floor("h")

        df_4h = df.resample(RESAMPLE_RULE, label="left",
                            closed="left").agg({
            "Open":  "first",
            "High":  "max",
            "Low":   "min",
            "Close": "last",
        }).dropna()

        return df_4h if not df_4h.empty else None
    except Exception as e:
        log(f"❌ fetch_4h {symbol}: {e}")
        traceback.print_exc()
        return None


# ==================== ویژگی برداری ====================
def build_feature_matrix(df, pattern_len=PATTERN_LEN):
    o = df["Open"].values.astype(float)
    h = df["High"].values.astype(float)
    l = df["Low"].values.astype(float)
    c = df["Close"].values.astype(float)

    safe_o = np.where(o > 0, o, np.nan)
    body  = (c - o) / safe_o
    upper = (h - np.maximum(o, c)) / safe_o
    lower = (np.minimum(o, c) - l) / safe_o
    rng   = (h - l) / safe_o

    base = np.stack([body, upper, lower, rng], axis=1)
    n = len(base)
    if pattern_len == 1:
        return base

    out = np.full((n, 4 * pattern_len), np.nan)
    for i in range(pattern_len - 1, n):
        out[i] = base[i - pattern_len + 1: i + 1].ravel()
    return out


# ==================== تحلیل فوروارد ====================
def analyze_forward(entry_idx, o, h, l, c, n, lookahead):
    if ENTRY_MODE == "next_open":
        if entry_idx + 1 >= n:
            return None
        entry_price = float(o[entry_idx + 1])
        start = entry_idx + 1
    else:
        entry_price = float(c[entry_idx])
        start = entry_idx + 1

    if entry_price <= 0:
        return None

    end = start + lookahead
    if end > n:
        return None

    fut_c = c[start:end]
    fut_h = h[start:end]
    fut_l = l[start:end]
    if len(fut_c) == 0:
        return None

    final_price = float(fut_c[-1])
    total_ret = (final_price - entry_price) / entry_price * 100.0

    out = {"ret": total_ret}
    for T in TARGETS:
        target_price = entry_price * (1 + T / 100.0)
        hits = np.where(fut_h >= target_price)[0]
        if len(hits) == 0:
            out[f"hit_{T}"] = False
            for D in DD_THRESHOLDS:
                out[f"hit_{T}_dd{D}"] = False
        else:
            out[f"hit_{T}"] = True
            k = int(hits[0])
            dd = (float(np.min(fut_l[:k + 1])) - entry_price) / entry_price * 100.0
            for D in DD_THRESHOLDS:
                if D == 1.0 and STRICT_DD1:
                    out[f"hit_{T}_dd{D}"] = dd > -1.0   # کاملاً زیر ۱٪
                else:
                    out[f"hit_{T}_dd{D}"] = dd >= -D
    return out


# ==================== جستجوی مشابه (با ماسک پیش‌محاسبه) ====================
def find_similar(target_feat, target_dt_actual, dfs, feats_cache, static_masks):
    candidates_all = []

    for sym in SYMBOLS:
        df = dfs.get(sym)
        if df is None:
            continue

        feats = feats_cache[sym]
        valid_idxs = static_masks[sym]

        if RESTRICT_CANDIDATES_BEFORE_TARGET:
            valid_idxs = valid_idxs[df.index[valid_idxs] < target_dt_actual]

        if EXCLUDE_EXACT_TARGET_CANDLE and sym == TARGET_SYMBOL:
            valid_idxs = valid_idxs[df.index[valid_idxs] != target_dt_actual]

        if len(valid_idxs) == 0:
            continue

        sub = feats[valid_idxs]
        diffs = sub - target_feat
        dists = np.sqrt(np.sum(diffs * diffs, axis=1))

        k = min(K_PER_SYMBOL * 5, len(valid_idxs))
        order = np.argsort(dists)[:k]
        for oi in order:
            cand_idx = int(valid_idxs[oi])
            candidates_all.append({
                "symbol": sym,
                "idx":    cand_idx,
                "dist":   float(dists[oi]),
                "date":   df.index[cand_idx],
            })

    candidates_all.sort(key=lambda x: x["dist"])

    unique_candidates = []
    seen_weeks = set()

    for c_ in candidates_all:
        if ONE_CANDIDATE_PER_WEEK:
            iso = c_["date"].isocalendar()
            yw = (int(iso[0]), int(iso[1]))
            if yw in seen_weeks:
                continue

        too_close = False
        for u in unique_candidates:
            if u["symbol"] != c_["symbol"]:
                continue
            dt_hours = abs((u["date"] - c_["date"]).total_seconds()) / 3600.0
            if dt_hours < EXCLUDE_TEMPORAL_HOURS:
                too_close = True
                break
        if too_close:
            continue

        unique_candidates.append(c_)
        if ONE_CANDIDATE_PER_WEEK:
            iso = c_["date"].isocalendar()
            seen_weeks.add((int(iso[0]), int(iso[1])))

        if len(unique_candidates) >= TOTAL_TOP:
            break

    return unique_candidates


# ==================== تحلیل یک کندل مرجع ====================
def analyze_target_candle(target_idx, df_target, target_feat,
                          dfs, feats_cache, static_masks, baseline):
    if target_feat is None or np.isnan(target_feat).any():
        return None

    target_dt = df_target.index[target_idx]

    candidates = find_similar(target_feat, target_dt, dfs, feats_cache, static_masks)
    if len(candidates) < MIN_PATTERNS:
        return None

    results = []
    for c_ in candidates:
        sym = c_["symbol"]
        df = dfs[sym]
        o = df["Open"].values.astype(float)
        h = df["High"].values.astype(float)
        l = df["Low"].values.astype(float)
        cc = df["Close"].values.astype(float)
        n = len(df)
        r = analyze_forward(c_["idx"], o, h, l, cc, n, LOOKAHEAD)
        if r is None:
            continue
        r["symbol"] = sym
        r["date"]   = c_["date"]
        r["dist"]   = c_["dist"]
        results.append(r)

    if len(results) < MIN_PATTERNS:
        return None

    n_res = len(results)
    wins = sum(1 for r in results if r["ret"] > 0)
    win_pct = wins / n_res * 100.0

    # 🎯 معیار اصلی: +۴٪ با ریزش زیر ۱٪
    hit_4_dd1_list = [r for r in results if r.get("hit_4.0_dd1.0", False)]
    hit_4_dd1 = len(hit_4_dd1_list) / n_res * 100.0

    # معیارهای کمکی
    hit_2_dd1_list = [r for r in results if r.get("hit_2.0_dd1.0", False)]
    hit_2_dd1 = len(hit_2_dd1_list) / n_res * 100.0

    base_4_dd1 = baseline.get((4.0, 1.0), 0.0)
    edge = hit_4_dd1 - base_4_dd1

    # امید ریاضی برای R/R = 1:4
    p = hit_4_dd1 / 100.0
    exp_val = p * 4.0 - (1.0 - p) * 1.0

    hit_rates = {}
    for T in TARGETS:
        for D in DD_THRESHOLDS:
            cnt = sum(1 for r in results if r.get(f"hit_{T}_dd{D}", False))
            hit_rates[(T, D)] = cnt / n_res * 100.0

    target_info = {
        "date": target_dt,
        "o": float(df_target["Open"].values[target_idx]),
        "h": float(df_target["High"].values[target_idx]),
        "l": float(df_target["Low"].values[target_idx]),
        "c": float(df_target["Close"].values[target_idx]),
        "body": float(target_feat[0]),
        "up":   float(target_feat[1]) if target_feat.size > 1 else 0.0,
        "dn":   float(target_feat[2]) if target_feat.size > 2 else 0.0,
        "rng":  float(target_feat[3]) if target_feat.size > 3 else 0.0,
    }

    return {
        "target_idx":     target_idx,
        "target_info":    target_info,
        "n_patterns":     n_res,
        "win_pct":        win_pct,
        "hit_4_dd1":      hit_4_dd1,
        "hit_2_dd1":      hit_2_dd1,
        "edge":           edge,
        "exp_val":        exp_val,
        "hit_rates":      hit_rates,
        "hit_4_dd1_list": hit_4_dd1_list,
        "hit_2_dd1_list": hit_2_dd1_list,
    }


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
def build_top_report(top_results, baseline, total_scanned, total_valid):
    if not top_results:
        return "بدون داده"

    lines = []
    lines.append(f"🎯 <b>۵ کندل برتر {TARGET_SYMBOL} برای +۴٪/DD≤۱٪</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"📅 اسکن شده: <code>{total_scanned}</code> کندل | "
                 f"معتبر: <code>{total_valid}</code>")
    lines.append(f"📊 معیار رتبه‌بندی: <b>+۴٪ با ریزش زیر ۱٪</b>")
    lines.append(f"🎯 نرخ پایه +۴٪/DD≤۱٪: "
                 f"<code>{baseline.get((4.0, 1.0), 0.0):.1f}٪</code>")
    lines.append(f"🔎 Lookahead: <code>{LOOKAHEAD}×4h</code> | "
                 f"ورود: <code>{ENTRY_MODE}</code>")
    lines.append("")

    for rank, r in enumerate(top_results, 1):
        ti = r["target_info"]
        d = ti["date"]
        date_s = d.strftime("%Y-%m-%d %H:%M (%a)")

        lines.append(f"<b>#{rank} — {date_s}</b>")
        lines.append(f"   O=<code>{ti['o']:.4f}</code> "
                     f"H=<code>{ti['h']:.4f}</code> "
                     f"L=<code>{ti['l']:.4f}</code> "
                     f"C=<code>{ti['c']:.4f}</code>")
        lines.append(f"   بدنه <code>{ti['body']*100:+.2f}٪</code> | "
                     f"بالا <code>{ti['up']*100:.2f}٪</code> | "
                     f"پایین <code>{ti['dn']*100:.2f}٪</code> | "
                     f"دامنه <code>{ti['rng']*100:.2f}٪</code>")
        lines.append(f"   الگو: <code>{r['n_patterns']}</code> | "
                     f"سوددهی: <code>{r['win_pct']:.1f}٪</code>")

        lines.append(
            f"   🎯 <b>+۴٪/DD≤۱٪: <code>{r['hit_4_dd1']:.1f}٪</code></b> "
            f"(پایه <code>{baseline.get((4.0,1.0),0):.1f}٪</code> | "
            f"edge <code>{r['edge']:+.1f}٪</code>) "
            f"| امید <code>{r['exp_val']:+.2f}٪</code>"
        )

        hr = r["hit_rates"]
        lines.append(
            f"   +۲٪/DD≤۱٪: <code>{hr.get((2.0,1.0),0):.1f}٪</code> | "
            f"+۴٪/DD≤۲٪: <code>{hr.get((4.0,2.0),0):.1f}٪</code> | "
            f"+۲۰٪/DD≤۲٪: <code>{hr.get((20.0,2.0),0):.1f}٪</code>"
        )

        if r["hit_4_dd1_list"]:
            lines.append(f"   ✅ موارد موفق +۴٪/DD≤۱٪:")
            for m in r["hit_4_dd1_list"][:5]:
                lines.append(f"      • <b>{m['symbol']}</b> @ "
                             f"{m['date'].strftime('%Y-%m-%d %H:%M')}")
            if len(r["hit_4_dd1_list"]) > 5:
                lines.append(f"      • ... و "
                             f"{len(r['hit_4_dd1_list']) - 5} مورد دیگر")

        lines.append("")

    return "\n".join(lines).rstrip()


# ==================== اجرای اصلی ====================
def main():
    log("=" * 60)
    log(f"🚀 اسکن همه‌ی کندل‌های 4h {TARGET_SYMBOL} | ورود={ENTRY_MODE}")
    log(f"   بازه: {SEARCH_PERIOD} | اینتروال: {INTERVAL_SRC} → {RESAMPLE_RULE}")
    log(f"   کاندیدها: {MIN_CANDIDATE_DATE} → {MAX_CANDIDATE_DATE}")
    log(f"   TOP_N={TOP_N_TO_SEND} | RANK_BY={RANK_BY} | "
        f"MAX_SCAN={MAX_TARGETS_TO_SCAN}")
    log(f"   STRICT_DD1={STRICT_DD1} | "
        f"RESTRICT_BEFORE_TARGET={RESTRICT_CANDIDATES_BEFORE_TARGET}")
    log("=" * 60)

    if pd.Timestamp(MIN_CANDIDATE_DATE) >= pd.Timestamp(MAX_CANDIDATE_DATE):
        raise RuntimeError("⛔ بازه‌ی کاندید نامعتبر است.")

    # ۱) دریافت داده
    dfs = {}
    for sym in SYMBOLS:
        df = fetch_4h(sym)
        if df is not None and len(df) > LOOKAHEAD + 10:
            dfs[sym] = df
            log(f"   ↳ {sym}: {len(df)} کندل 4h "
                f"({df.index[0]} → {df.index[-1]})")

    if TARGET_SYMBOL not in dfs:
        raise RuntimeError(f"⛔ داده‌ی 4h برای {TARGET_SYMBOL} موجود نیست.")

    # ۲) کش ویژگی‌ها
    log("\n🧮 ساخت ماتریس ویژگی‌ها...")
    feats_cache = {}
    for sym, df in dfs.items():
        feats_cache[sym] = build_feature_matrix(df, PATTERN_LEN)
        log(f"   ↳ {sym}: {feats_cache[sym].shape}")

    # ۳) پیش‌محاسبه‌ی ماسک‌های ثابت (سرعت)
    log("\n🔧 پیش‌محاسبه‌ی ماسک‌های ثابت...")
    floor_global = pd.Timestamp(MIN_CANDIDATE_DATE)
    cap_global   = pd.Timestamp(MAX_CANDIDATE_DATE)
    static_masks = {}
    for sym, df in dfs.items():
        feats = feats_cache[sym]
        n = len(df)
        idx_arr = np.arange(n)
        mask = (df.index >= floor_global) & (df.index < cap_global)
        mask &= (idx_arr >= PATTERN_LEN - 1)
        mask &= (idx_arr <= n - LOOKAHEAD - 2)
        mask &= ~np.isnan(feats).any(axis=1)
        static_masks[sym] = np.where(mask)[0]
        log(f"   ↳ {sym}: {len(static_masks[sym])} کندل معتبر")

    # ۴) نرخ پایه
    log("\n📊 محاسبه‌ی نرخ پایه...")
    all_outcomes = []
    for sym, df in dfs.items():
        o = df["Open"].values.astype(float)
        h = df["High"].values.astype(float)
        l = df["Low"].values.astype(float)
        c = df["Close"].values.astype(float)
        n = len(df)
        for i in range(n):
            out = analyze_forward(i, o, h, l, c, n, LOOKAHEAD)
            if out is not None:
                all_outcomes.append(out)

    n_base = len(all_outcomes)
    if n_base == 0:
        raise RuntimeError("نمونه‌ی پایه صفر است.")
    baseline = {
        "win": sum(1 for o in all_outcomes if o["ret"] > 0) / n_base * 100.0
    }
    for T in TARGETS:
        for D in DD_THRESHOLDS:
            cnt = sum(1 for o in all_outcomes if o[f"hit_{T}_dd{D}"])
            baseline[(T, D)] = cnt / n_base * 100.0

    log(f"   نمونه: {n_base} | سوددهی پایه: {baseline['win']:.2f}٪")
    log(f"   +۲٪/DD≤۱٪ پایه: {baseline[(2.0, 1.0)]:.2f}٪")
    log(f"   +۴٪/DD≤۱٪ پایه: {baseline[(4.0, 1.0)]:.2f}٪")

    # ۵) اسکن همه‌ی کندل‌های TARGET_SYMBOL (از جدید به قدیم)
    df_target    = dfs[TARGET_SYMBOL]
    target_feats = feats_cache[TARGET_SYMBOL]
    n_t          = len(df_target)

    valid_target_idxs = list(range(PATTERN_LEN - 1, n_t))[::-1]
    valid_target_idxs = [i for i in valid_target_idxs
                         if not np.isnan(target_feats[i]).any()]
    if MAX_TARGETS_TO_SCAN is not None:
        valid_target_idxs = valid_target_idxs[:MAX_TARGETS_TO_SCAN]

    total_targets = len(valid_target_idxs)
    log(f"\n🔍 اسکن {total_targets} کندل مرجع...")

    all_results = []
    for k, idx in enumerate(valid_target_idxs):
        try:
            res = analyze_target_candle(
                idx, df_target, target_feats[idx],
                dfs, feats_cache, static_masks, baseline
            )
            if res:
                all_results.append(res)
        except Exception as e:
            log(f"   ⚠️ خطا idx={idx}: {e}")

        if (k + 1) % 500 == 0:
            log(f"   پیشرفت: {k+1}/{total_targets} | معتبر: {len(all_results)}")

    log(f"✅ تحلیل کامل شد: {len(all_results)} کندل معتبر")

    if not all_results:
        raise RuntimeError("هیچ کندل معتبری یافت نشد.")

    # ۶) رتبه‌بندی
    valid_rank_keys = ("hit_4_dd1", "hit_2_dd1", "exp_val", "edge", "win_pct")
    if RANK_BY not in valid_rank_keys:
        raise RuntimeError(f"RANK_BY نامعتبر: {RANK_BY}")

    all_results.sort(key=lambda x: x[RANK_BY], reverse=True)
    top5 = all_results[:TOP_N_TO_SEND]

    # ۷) گزارش و ارسال
    report = build_top_report(top5, baseline, total_targets, len(all_results))
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
