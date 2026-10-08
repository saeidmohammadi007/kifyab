import os
import pandas as pd
import numpy as np
import yfinance as yf
from tqdm import tqdm
import time
import requests
import ccxt

# --- الگوی مرجع: دو کندل روزانه ETH ---
PATTERN_SYMBOL = 'ETH-USD'
PATTERN_DATES  = ['2025-08-01', '2025-08-02']   # ← دو کندل متوالی
SHOW_N         = 10

# ---------- توابع ----------
def get_lbank_futures_symbols():
    exchange = ccxt.lbank({'options': {'defaultType': 'future'}})
    try:
        markets = exchange.load_markets()
    except Exception as e:
        print(f"❌ خطا در اتصال به LBank: {e}")
        return []

    base_list = []
    for symbol, market in markets.items():
        if not market.get('swap'):
            continue
        base = market.get('base')
        if not base or base.isdigit():
            continue
        base_list.append(base.upper())

    seen, unique_bases = set(), []
    for b in base_list:
        if b not in seen:
            seen.add(b)
            unique_bases.append(b)
    print(f"✅ تعداد ارزهای پایه‌ی منحصربه‌فرد فیوچرز LBank: {len(unique_bases)}")
    return unique_bases

def get_daily_data(ticker, start='2024-01-01'):
    df = yf.download(ticker, start=start, interval='1d',
                     progress=False, auto_adjust=False)
    if df.empty:
        return None
    df = df[['Open', 'High', 'Low', 'Close']].copy()
    df.index = pd.to_datetime(df.index)
    df.columns = ['open', 'high', 'low', 'close']
    return df

def get_4h_data(ticker, start_date=None, end_date=None):
    try:
        if start_date:
            df = yf.download(ticker,
                             start=start_date,
                             end=end_date,
                             interval='1h',
                             progress=False,
                             auto_adjust=False)
        else:
            df = yf.download(ticker, period='60d', interval='1h',
                             progress=False, auto_adjust=False)
    except Exception:
        return None

    if df is None or df.empty:
        return None

    df = df[['Open', 'High', 'Low', 'Close']].copy()
    df.index = pd.to_datetime(df.index)
    df.columns = ['open', 'high', 'low', 'close']

    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)

    df_4h = df.resample('4h').agg({
        'open':   'first',
        'high':   'max',
        'low':    'min',
        'close':  'last',
    }).dropna()
    return df_4h

def candle_vector(o, h, l, c):
    """بردار ۴بعدی نرمال‌شده‌ی یک کندل نسبت به دامنه High−Low"""
    rng = float(h - l)
    if rng <= 0 or not np.isfinite(rng):
        return None
    return np.array([
        (float(o) - float(l)) / rng,
        1.0,
        0.0,
        (float(c) - float(l)) / rng,
    ])

def send_telegram_message(text):
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        print("❌ توکن یا chat_id تنظیم نشده است.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'}
    try:
        r = requests.post(url, data=payload, timeout=10)
        if r.status_code != 200:
            print(f"⚠️ خطا در ارسال پیام: {r.text}")
        else:
            print("✅ پیام با موفقیت به تلگرام ارسال شد.")
    except Exception as e:
        print(f"❌ خطا در ارسال به تلگرام: {e}")

# ---------- اجرای اصلی ----------
print(f"🔍 استخراج الگوی دو‌کندلی {PATTERN_SYMBOL} برای تاریخ‌های {PATTERN_DATES} (روزانه) ...")
ref_daily = get_daily_data(PATTERN_SYMBOL, start='2024-01-01')
if ref_daily is None:
    print(f"❌ خطا در دریافت داده‌های {PATTERN_SYMBOL}")
    exit()

pattern_vectors = []
pattern_candles = []
for pdate in PATTERN_DATES:
    tdate = pd.to_datetime(pdate).date()
    m = ref_daily.index.date == tdate
    if not m.any():
        print(f"❌ کندلی برای تاریخ {pdate} یافت نشد.")
        exit()
    cd = ref_daily[m].iloc[0]
    v = candle_vector(cd['open'], cd['high'], cd['low'], cd['close'])
    if v is None:
        print(f"❌ کندل {pdate} نامعتبر است.")
        exit()
    pattern_vectors.append(v)
    pattern_candles.append((pdate, cd['open'], cd['high'], cd['low'], cd['close']))

# بردار نهایی الگو = الحاق دو بردار ۴بعدی → ۸بعدی
pattern_vec = np.concatenate(pattern_vectors)

print("📌 کندل‌های مرجع (روزانه):")
for (pdate, o, h, l, c) in pattern_candles:
    print(f"   {pdate} → O={o:.4f}  H={h:.4f}  L={l:.4f}  C={c:.4f}")
print(f"📐 بردار الگو (۸بعدی): {np.round(pattern_vec, 3).tolist()}")

# --- بازه‌ی دریافت داده‌ی ۴ ساعته ---
# آخرین کندل ۴h هدف: منتهی به 20:00 روز قبل از آخرین تاریخ الگو (یعنی 2025-08-01 20:00)
# کندل ماقبل آن: منتهی به 20:00 روز قبل‌تر (یعنی 2025-07-31 20:00)
first_pat_dt  = pd.to_datetime(PATTERN_DATES[0])
last_pat_dt   = pd.to_datetime(PATTERN_DATES[-1])
scan_end_dt   = last_pat_dt                                  # انحصاری
scan_start_dt = first_pat_dt - pd.Timedelta(days=10)         # برای اطمینان از وجود داده

scan_start_str = scan_start_dt.strftime('%Y-%m-%d')
scan_end_str   = scan_end_dt.strftime('%Y-%m-%d')

print(f"\n🕒 بازه‌ی دریافت کندل ۴ ساعته: {scan_start_str} تا {scan_end_str}")
print(f"🎯 هدف: دو کندل ۴ ساعته‌ی متوالی منتهی به 20:00 روز قبل از {PATTERN_DATES[-1]}")

print("\n📊 دریافت نمادهای فیوچرز LBank ...")
symbols = get_lbank_futures_symbols()
if not symbols:
    print("❌ هیچ نمادی برای اسکن وجود ندارد!")
    exit()

results = []
for sym in tqdm(symbols, desc="اسکن دو کندل ۴ ساعته‌ی متوالی"):
    try:
        df_4h = get_4h_data(f"{sym}-USD",
                            start_date=scan_start_str,
                            end_date=scan_end_str)
        if df_4h is None or len(df_4h) < 2:
            continue

        c1 = df_4h.iloc[-2]   # کندل ماقبل آخر
        c2 = df_4h.iloc[-1]   # کندل آخر

        v1 = candle_vector(c1['open'], c1['high'], c1['low'], c1['close'])
        v2 = candle_vector(c2['open'], c2['high'], c2['low'], c2['close'])
        if v1 is None or v2 is None:
            continue

        scan_vec = np.concatenate([v1, v2])
        dist = float(np.linalg.norm(pattern_vec - scan_vec))

        results.append({
            'symbol':   sym,
            'dist':     dist,
            't1':       df_4h.index[-2].strftime('%Y-%m-%d %H:%M'),
            't2':       df_4h.index[-1].strftime('%Y-%m-%d %H:%M'),
            'o1': float(c1['open']), 'h1': float(c1['high']),
            'l1': float(c1['low']),  'c1': float(c1['close']),
            'o2': float(c2['open']), 'h2': float(c2['high']),
            'l2': float(c2['low']),  'c2': float(c2['close']),
        })
        time.sleep(0.3)
    except Exception:
        continue

if results:
    df_res = pd.DataFrame(results).sort_values('dist').head(SHOW_N)

    lines = []
    lines.append(f"🏆 <b>دو کندل ۴ ساعته‌ی متوالی مشابه دو کندل روزانه {PATTERN_SYMBOL} ({PATTERN_DATES[0]} و {PATTERN_DATES[1]})</b>\n")
    lines.append("📌 الگو (روزانه):")
    for (pdate, o, h, l, c) in pattern_candles:
        lines.append(f"   {pdate} → O={o:.6g} H={h:.6g} L={l:.6g} C={c:.6g}")
    lines.append("")
    for _, row in df_res.iterrows():
        lines.append(
            f"🔸 <b>{row['symbol']}</b>  (فاصله: {row['dist']:.4f})\n"
            f"   ۴h #1 [{row['t1']}]  O={row['o1']:.6g} H={row['h1']:.6g} "
            f"L={row['l1']:.6g} C={row['c1']:.6g}\n"
            f"   ۴h #2 [{row['t2']}]  O={row['o2']:.6g} H={row['h2']:.6g} "
            f"L={row['l2']:.6g} C={row['c2']:.6g}"
        )
    lines.append(f"\n📅 تعداد ارزهای اسکن‌شده: {len(symbols)}")
    message = "\n".join(lines)

    send_telegram_message(message)
    print("\n" + message)
else:
    print("\n❌ نتیجه‌ای یافت نشد.")
    send_telegram_message("❌ در اسکن امروز هیچ نتیجه‌ای یافت نشد.")

print("\n✅ اسکن کامل شد!")
