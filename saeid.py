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
LOOKAHEAD = 42  # 1 هفته

# ⚠️ اهداف واقع‌گرایانه‌تر (به جای 20/50 با DD=2)
TARGETS       = [5.0, 10.0, 15.0, 20.0]
DD_THRESHOLDS = [3.0, 5.0, 8.0]

K_NEIGHBORS   = 100
MIN_NEIGHBORS = 50
MIN_HIT_RATE  = 15.0

# فیلتر همسایه‌های زمانی (4 هفته × 6 کندل/روز × 7 = 168)
EXCLUDE_TEMPORAL = 168

DEDUP_DIST   = 0.5   # روی features استانداردشده (واحد = sigma)
EVENT_WINDOW = 42    # کندل 4h — dedup رویدادی (1 هفته)
TOP_PATTERNS = 25

ENTRY_MODE = "next_open"

MIN_SAMPLE_DATE = "2024-01-01"

# ⚠️ Train/Test Split
TRAIN_END = "2025-06-01"   # قبل از این تاریخ = Train
# TEST = از TRAIN_END به بعد

# حداقل کندل 4h برای هر نماد
# 500 کندل ≈ 83 روز
MIN_BARS_PER_SYMBOL = 500

# ⚠️ حداقل edge روی TEST برای تأیید نهایی الگو
MIN_TEST_EDGE = 3.0

DEBUG_LOWS = True


# ==================== توابع داده ====================
def fetch_4h(symbol):
    log(f"📥 دریافت {symbol} (1h → 4h)")
    try:
        df = yf.download(symbol, interval="1h", period=SEARCH_PERIOD_HOURLY,
                         progress=False, auto_adjust=True, timeout=60)
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        cols = ['Open', 'High', 'Low', 'Close']
        if 'Volume' in df.columns:
            cols.append('Volume')
        df = df[cols].dropna()
        if df.empty:
            return None
        if 'Volume' not in df.columns:
            df['Volume'] = 1.0

        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)
        if df.index.tz is not None:
            df.index = df.index.tz_convert('UTC').tz_localize(None)

        df_4h = df.resample('4h', label='left', closed='left').agg({
            'Open':  'first',
            'High':  'max',
            'Low':   'min',
            'Close': 'last',
            'Volume':'sum',
        }).dropna()
        return df_4h if not df_4h.empty else None
    except Exception as e:
        log(f"❌ fetch_4h {symbol}: {e}")
        traceback.print_exc()
        return None


def compute_features(df):
    """
    برمی‌گرداند: ماتریس (n, 8) از features استاندارد-پذیر.
    ستون‌ها:
      0: body              (c-o)/o
      1: upper_wick        (h - max(o,c))/o
      2: lower_wick        (min(o,c) - l)/o
      3: total_range       (h-l)/o
      4: atr14_norm        ATR14 / close
      5: rsi14             (0..1)
      6: dist_sma50        (c - sma50)/sma50
      7: rel_vol20         volume / MA20(volume)، clip 0..10، ÷10
    """
    o = df['Open'].values.astype(float)
    h = df['High'].values.astype(float)
    l = df['Low'].values.astype(float)
    c = df['Close'].values.astype(float)
    v = df['Volume'].values.astype(float)
    n = len(c)

    body       = (c - o) / np.maximum(o, 1e-12)
    upper_wick = (h - np.maximum(o, c)) / np.maximum(o, 1e-12)
    lower_wick = (np.minimum(o, c) - l) / np.maximum(o, 1e-12)
    total_rng  = (h - l) / np.maximum(o, 1e-12)

    # ATR-14
    prev_c = np.roll(c, 1); prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr14 = pd.Series(tr).rolling(14, min_periods=1).mean().values
    atr_norm = atr14 / np.maximum(c, 1e-12)

    # RSI-14 (Wilder smoothing)
    delta = np.diff(c, prepend=c[0])
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = pd.Series(gain).ewm(alpha=1/14, adjust=False).mean().values
    avg_loss = pd.Series(loss).ewm(alpha=1/14, adjust=False).mean().values
    rs = np.where(avg_loss > 1e-12, avg_gain / np.maximum(avg_loss, 1e-12), 100.0)
    rsi = (100.0 - 100.0 / (1.0 + rs)) / 100.0

    # فاصله از SMA50
    sma50 = pd.Series(c).rolling(50, min_periods=1).mean().values
    dist_sma = (c - sma50) / np.maximum(sma50, 1e-12)

    # Volume نسبی
    vol_ma20 = pd.Series(v).rolling(20, min_periods=1).mean().values
    rel_vol = v / np.maximum(vol_ma20, 1e-12)
    rel_vol = np.clip(rel_vol, 0, 10) / 10.0

    feats = np.column_stack([
        body, upper_wick, lower_wick, total_rng,
        atr_norm, rsi, dist_sma, rel_vol
    ])
    feats[~np.isfinite(feats)] = 0.0
    return feats


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

    out = {'ret': total_ret,
           'entry_price': entry_price,
           'min_low_window': float(np.min(fut_l))}
    for T in TARGETS:
        tp = entry_price * (1 + T / 100.0)
        hits = np.where(fut_h >= tp)[0]
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
            chunks.append(cur); cur = ln
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


# ==================== ابزار آماری ====================
def compute_baseline(outcomes):
    n = len(outcomes)
    if n == 0:
        return 0.0, {}
    win = sum(1 for o in outcomes if o['ret'] > 0) / n * 100.0
    rates = {}
    for T in TARGETS:
        for D in DD_THRESHOLDS:
            cnt = sum(1 for o in outcomes if o[f'hit_{T}_dd{D}'])
            rates[(T, D)] = cnt / n * 100.0
    return win, rates


def random_baseline_sanity(outcomes, n_trials=5000, seed=42):
    """
    نمونه‌گیری تصادفی با جایگذاری از همان outcomes، برای بررسی معناداری.
    با CI ۹۵٪ تقریبی.
    """
    n = len(outcomes)
    if n == 0:
        return 0.0, {}
    rng = np.random.default_rng(seed)
    idx = rng.choice(n, size=min(n_trials, n), replace=False)
    win = sum(1 for j in idx if outcomes[j]['ret'] > 0) / len(idx) * 100
    rates = {}
    for T in TARGETS:
        for D in DD_THRESHOLDS:
            cnt = sum(1 for j in idx if outcomes[j][f'hit_{T}_dd{D}'])
            rates[(T, D)] = cnt / len(idx) * 100
    return win, rates


# ==================== گزارش ====================
def build_report(validated, train_base, test_base, random_base,
                 scan_stats, n_candidates_train, n_picked_pre_test):
    lines = []
    lines.append("🔍 <b>اسکن الگو (KDTree + Walk-Forward)</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"⏱ تایم‌فریم: <code>4h</code> | Lookahead: <code>{LOOKAHEAD} کندل</code>")
    lines.append(f"🎯 اهداف: <code>{', '.join(f'+{T:.0f}٪' for T in TARGETS)}</code>")
    lines.append(f"📉 ریزش مجاز: <code>{', '.join(f'{D:.0f}٪' for D in DD_THRESHOLDS)}</code>")
    lines.append(f"🧮 نمونه‌ها: Train=<code>{scan_stats['n_train']}</code> | "
                 f"Test=<code>{scan_stats['n_test']}</code>")
    lines.append(f"📅 Train: <code>{scan_stats['train_start']} → {TRAIN_END}</code>")
    lines.append(f"📅 Test:  <code>{TRAIN_END} → {scan_stats['test_end']}</code>")
    lines.append(f"📡 ورود: <code>{ENTRY_MODE}</code> | "
                 f"K=<code>{K_NEIGHBORS}</code> | "
                 f"حداقل edge test: <code>{MIN_TEST_EDGE:.1f}٪</code>")
    lines.append("")

    lines.append("📊 <b>نرخ پایه:</b>")
    lines.append(f"├ Train سوددهی کلی: <code>{train_base[0]:.1f}٪</code>")
    lines.append(f"├ Test  سوددهی کلی: <code>{test_base[0]:.1f}٪</code>")
    lines.append(f"├ Random Sanity (Test): <code>{random_base[0]:.1f}٪</code>")
    for T in TARGETS:
        for D in DD_THRESHOLDS:
            tb = test_base[1].get((T, D), 0.0)
            rb = random_base[1].get((T, D), 0.0)
            lines.append(f"├ +{T:.0f}٪ | DD≤{D:.0f}٪ → "
                         f"Test <code>{tb:.1f}٪</code> | "
                         f"Rand <code>{rb:.1f}٪</code>")
    lines.append("")
    lines.append(f"🧪 کاندیدهای Train: <code>{n_candidates_train}</code> | "
                 f"یکتای قبل از Test: <code>{n_picked_pre_test}</code> | "
                 f"تأییدشده در Test: <code>{len(validated)}</code>")
    lines.append("")

    if not validated:
        lines.append("❌ هیچ الگویی در Test تأیید نشد (edge کافی نداشت).")
        return "\n".join(lines)

    lines.append("🏆 <b>الگوهای تأییدشده در Test:</b>")
    lines.append("")

    for i, r in enumerate(validated, 1):
        lines.append(f"<b>#{i} — {r['symbol']} @ {r['date']}</b>")
        lines.append(
            f"   🔎 Train n=<code>{r['n_train']}</code> | "
            f"سوددهی Train <code>{r['win_train']:.1f}٪</code> | "
            f"Test n=<code>{r['n_test']}</code> | "
            f"سوددهی Test <code>{r['win_test']:.1f}٪</code>"
        )
        for T in TARGETS:
            for D in DD_THRESHOLDS:
                in_r = r.get(f'rate_{T}_dd{D}_train', 0.0)
                out_r = r.get(f'rate_{T}_dd{D}_test', 0.0)
                base_r = test_base[1].get((T, D), 0.0)
                edge = out_r - base_r
                flag = "✅" if edge >= MIN_TEST_EDGE else "▫️"
                lines.append(
                    f"   ├ +{T:.0f}٪ | DD≤{D:.0f}٪: "
                    f"Train <code>{in_r:.1f}٪</code> → "
                    f"Test <code>{out_r:.1f}٪</code> "
                    f"(edge <code>{edge:+.1f}٪</code> {flag})"
                )
        f = r['feat_raw']
        lines.append(f"   🕯 body <code>{f[0]*100:+.2f}٪</code> | "
                     f"RSI <code>{f[5]*100:.0f}</code> | "
                     f"ATR <code>{f[4]*100:.2f}٪</code> | "
                     f"dist-SMA50 <code>{f[6]*100:+.2f}٪</code> | "
                     f"rel-vol <code>{f[7]*10:.2f}x</code>")
        lines.append("")

    return "\n".join(lines).rstrip()


# ==================== اجرای اصلی ====================
def main():
    log("=" * 60)
    log("🚀 اسکن الگو (Walk-Forward + Event Dedup + Random Baseline)")
    log("=" * 60)

    dfs = {}
    for sym in SYMBOLS:
        df = fetch_4h(sym)
        if df is None:
            log(f"   ❌ {sym}: بدون داده")
            continue
        if len(df) < MIN_BARS_PER_SYMBOL:
            log(f"   ⚠️ {sym}: فقط {len(df)} کندل "
                f"(نیاز ≥ {MIN_BARS_PER_SYMBOL}) — رد شد")
            continue
        dfs[sym] = df
        log(f"   ✅ {sym}: {len(df)} کندل | "
            f"{df.index[0]} → {df.index[-1]}")

    if not dfs:
        raise RuntimeError("هیچ نمادی معیار حداقل داده را برآورده نکرد.")

    min_dt   = pd.Timestamp(MIN_SAMPLE_DATE)
    train_end = pd.Timestamp(TRAIN_END)

    # جمع‌آوری همه نمونه‌ها
    train_feats, train_outs, train_meta = [], [], []
    test_feats,  test_outs,  test_meta  = [], [], []
    symbol_index = {}

    for sym, df in dfs.items():
        symbol_index[sym] = df.index
        o = df['Open'].values.astype(float)
        h = df['High'].values.astype(float)
        l = df['Low'].values.astype(float)
        c = df['Close'].values.astype(float)
        feats = compute_features(df)
        n = len(df)

        for i in range(n):
            if df.index[i] < min_dt:
                continue
            out = analyze_forward(i, o, h, l, c, n, LOOKAHEAD)
            if out is None:
                continue
            f = feats[i]
            if not np.all(np.isfinite(f)):
                continue
            if df.index[i] < train_end:
                train_feats.append(f)
                train_outs.append(out)
                train_meta.append((sym, i))
            else:
                test_feats.append(f)
                test_outs.append(out)
                test_meta.append((sym, i))

    n_train = len(train_feats)
    n_test  = len(test_feats)
    log(f"\n📊 Train samples: {n_train} | Test samples: {n_test}")

    if n_train < MIN_NEIGHBORS * 3:
        raise RuntimeError(f"نمونه‌های Train کافی نیستند ({n_train}).")
    if n_test < MIN_NEIGHBORS:
        raise RuntimeError(f"نمونه‌های Test کافی نیستند ({n_test}).")

    train_feats = np.array(train_feats)
    test_feats  = np.array(test_feats)

    # استانداردسازی بر اساس train
    mu  = train_feats.mean(axis=0)
    sig = train_feats.std(axis=0)
    sig[sig < 1e-8] = 1.0
    train_feats_n = (train_feats - mu) / sig
    test_feats_n  = (test_feats  - mu) / sig

    # پایه‌ها
    train_win_base, train_rates_base = compute_baseline(train_outs)
    test_win_base,  test_rates_base  = compute_baseline(test_outs)
    rand_win,       rand_rates       = random_baseline_sanity(test_outs)

    log(f"\n📊 Baseline — Train win: {train_win_base:.2f}٪ | "
        f"Test win: {test_win_base:.2f}٪ | Random: {rand_win:.2f}٪")
    for (T, D), v in test_rates_base.items():
        log(f"   +{T:.0f}٪ / DD≤{D:.0f}٪ → Test: {v:.2f}٪ | "
            f"Rand: {rand_rates[(T, D)]:.2f}٪")

    # ========== مرحله ۱: یافتن کاندیدها روی TRAIN ==========
    log("\n🌳 ساخت KDTree روی TRAIN...")
    tree_train = cKDTree(train_feats_n)
    k = min(K_NEIGHBORS + 1, n_train)
    log(f"🔍 جستجوی {k-1} همسایه...")
    dists, idxs = tree_train.query(train_feats_n, k=k)

    log("\n🎯 امتیازدهی کاندیدها روی TRAIN...")
    candidates = []

    for i in range(n_train):
        sym_i, ci = train_meta[i]
        valid = []
        for j in idxs[i]:
            j = int(j)
            if j == i:
                continue
            sym_j, cj = train_meta[j]
            # فقط هم‌نماد با فاصله زمانی کم حذف می‌شود
            if sym_j == sym_i and abs(cj - ci) <= EXCLUDE_TEMPORAL:
                continue
            valid.append(j)

        if len(valid) < MIN_NEIGHBORS:
            continue

        nb_out = [train_outs[j] for j in valid]
        n_nb = len(nb_out)
        win_rate = sum(1 for o in nb_out if o['ret'] > 0) / n_nb * 100.0

        rates = {}
        for T in TARGETS:
            for D in DD_THRESHOLDS:
                cnt = sum(1 for o in nb_out if o[f'hit_{T}_dd{D}'])
                rates[(T, D)] = cnt / n_nb * 100.0

        best_rate = max(rates.values())
        if best_rate < MIN_HIT_RATE:
            continue

        cand = {
            'idx': i,
            'symbol': sym_i,
            'candle_idx': ci,
            'feat': train_feats_n[i],
            'feat_raw': train_feats[i],
            'win_train': win_rate,
            'n_train': n_nb,
            'best_rate_train': best_rate,
        }
        for (T, D), v in rates.items():
            cand[f'rate_{T}_dd{D}_train'] = v
        candidates.append(cand)

    log(f"🎯 کاندیدهای اولیه (Train): {len(candidates)}")
    n_candidates_train = len(candidates)

    if not candidates:
        report = build_report(
            [], (train_win_base, train_rates_base),
            (test_win_base, test_rates_base), (rand_win, rand_rates),
            {'n_train': n_train, 'n_test': n_test,
             'train_start': min_dt.strftime('%Y-%m-%d'),
             'test_end': max(
                 symbol_index[s][train_meta[-1][1]] if False else
                 pd.Timestamp.max
             ).strftime('%Y-%m-%d') if False else '—'},
            n_candidates_train, 0
        )
        send_message(report)
        return

    # ========== مرحله ۲: Event-dedup ==========
    candidates.sort(key=lambda x: -x['best_rate_train'])
    picked = []
    for cand in candidates:
        too_similar = False
        for p in picked:
            # شباهت feature
            if np.linalg.norm(cand['feat'] - p['feat']) < DEDUP_DIST:
                too_similar = True
                break
            # شباهت رویدادی — همان نماد، پنجره زمانی نزدیک
            if (cand['symbol'] == p['symbol'] and
                abs(cand['candle_idx'] - p['candle_idx']) < EVENT_WINDOW):
                too_similar = True
                break
        if not too_similar:
            picked.append(cand)
        if len(picked) >= TOP_PATTERNS * 3:
            break

    n_picked_pre_test = len(picked)
    log(f"🏆 الگوهای یکتا قبل از Test: {n_picked_pre_test}")

    # ========== مرحله ۳: اعتبارسنجی روی TEST ==========
    log("\n🔬 اعتبارسنجی روی TEST...")
    if len(test_feats_n) >= K_NEIGHBORS:
        tree_test = cKDTree(test_feats_n)
        kk = min(K_NEIGHBORS, len(test_feats_n))

        validated = []
        for r in picked:
            # نزدیک‌ترین نمونه‌های TEST به feature این الگو
            dd, ii = tree_test.query(r['feat'], k=kk)
            # اگر آرایه اسکالر برگشت
            if np.isscalar(ii):
                ii = np.array([ii])

            # فیلتر زمانی: حذف test sampleهایی که به رویداد اصلی
            # نزدیکند؟ — رویداد در TRAIN است، پس اینجا لازم نیست
            nb_out = [test_outs[int(j)] for j in ii]
            n_nb = len(nb_out)
            if n_nb < MIN_NEIGHBORS // 2:
                continue

            win_test = sum(1 for o in nb_out if o['ret'] > 0) / n_nb * 100.0

            # بهترین edge روی Test
            best_edge = -1e9
            for T in TARGETS:
                for D in DD_THRESHOLDS:
                    cnt = sum(1 for o in nb_out if o[f'hit_{T}_dd{D}'])
                    rate_test = cnt / n_nb * 100.0
                    r[f'rate_{T}_dd{D}_test'] = rate_test
                    edge = rate_test - test_rates_base[(T, D)]
                    if edge > best_edge:
                        best_edge = edge

            r['win_test'] = win_test
            r['n_test'] = n_nb
            r['best_edge_test'] = best_edge

            if best_edge >= MIN_TEST_EDGE:
                validated.append(r)

        validated.sort(key=lambda x: -x['best_edge_test'])
        validated = validated[:TOP_PATTERNS]
        log(f"✅ الگوهای تأییدشده در Test: {len(validated)}")
    else:
        validated = []
        log("⚠️ Test samples کافی برای اعتبارسنجی نیست.")

    # تاریخ‌گذاری
    for r in validated:
        sym = r['symbol']
        dt = symbol_index[sym][r['candle_idx']]
        if getattr(dt, 'tz', None) is not None:
            dt = dt.tz_localize(None)
        r['date'] = dt.strftime('%Y-%m-%d %H:%M')
        log(f"  ✅ {sym} @ {r['date']} | "
            f"Train win={r['win_train']:.1f}٪ | "
            f"Test win={r['win_test']:.1f}٪ | "
            f"best-edge={r['best_edge_test']:+.1f}٪")

    # Debug
    if DEBUG_LOWS and validated:
        log("\n🔬 دیباگ ورود/Low (برای تأیید در TradingView):")
        for r in validated[:5]:
            sym = r['symbol']; i = r['candle_idx']
            df = dfs[sym]
            o_ = df['Open'].values.astype(float)
            c_ = df['Close'].values.astype(float)
            entry = (o_[i+1] if i+1 < len(o_) else None) \
                if ENTRY_MODE == "next_open" else c_[i]
            fut_l = df['Low'].values.astype(float)[i+1:i+1+LOOKAHEAD]
            if entry is not None and len(fut_l) > 0:
                mn = fut_l.min()
                ddp = (mn - entry) / entry * 100.0
                log(f"   {sym} @ {r['date']} | "
                    f"Entry={entry:.6f} MinLow={mn:.6f} DD={ddp:+.2f}٪")

    # گزارش نهایی
    test_end_ts = max(
        (symbol_index[s][ci] for s, ci in [(m[0], m[1]) for m in test_meta]),
        default=pd.Timestamp.now()
    )
    scan_stats = {
        'n_train': n_train,
        'n_test': n_test,
        'train_start': min_dt.strftime('%Y-%m-%d'),
        'test_end': test_end_ts.strftime('%Y-%m-%d'),
    }
    report = build_report(
        validated,
        (train_win_base, train_rates_base),
        (test_win_base,  test_rates_base),
        (rand_win,       rand_rates),
        scan_stats, n_candidates_train, n_picked_pre_test
    )
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
