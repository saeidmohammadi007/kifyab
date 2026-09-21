import os
import pandas as pd
import numpy as np
import yfinance as yf
from tqdm import tqdm
import time
import requests
import ccxt

# ---------- تنظیمات ----------
SHOW_N        = 30          # حداکثر تعداد نتیجه در تلگرام
MACD_FAST     = 12
MACD_SLOW     = 26
MACD_SIGNAL   = 9
CHECK_CANDLES = 2           # چند کندل آخر ۴h برای بررسی کراس (۱ = فقط کندل آخر بسته‌شده)
ONLY_GOLDEN   = False       # اگر True فقط کراس صعودی، اگر False هر دو نوع کراس

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


def get_4h_data(ticker):
    """دریافت داده‌ی ۱ ساعته و تبدیل به ۴ ساعته."""
    df = yf.download(ticker, period='60d', interval='1h',
                     progress=False, auto_adjust=False)
    if df.empty:
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


def compute_macd(close, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def detect_cross(macd_line, signal_line, lookback=CHECK_CANDLES):
    """
    آخرین کراس رخ‌داده در 'lookback' کندل اخیر را بررسی می‌کند.
    خروجی: (نوع کراس, ایندکس کندل) یا (None, None)
    """
    n = len(macd_line)
    if n < 3:
        return None, None

    # از آخرین کندل بسته‌شده به عقب نگاه می‌کنیم
    # نکته: آخرین کندل ممکن است در حال تشکیل باشد، پس با iloc[-2] شروع می‌کنیم
    for i in range(n - 2, max(1, n - 2 - lookback), -1):
        prev_diff = macd_line.iloc[i - 1] - signal_line.iloc[i - 1]
        curr_diff = macd_line.iloc[i]     - signal_line.iloc[i]
        if not (np.isfinite(prev_diff) and np.isfinite(curr_diff)):
            continue
        if prev_diff <= 0 and curr_diff > 0:
            return 'golden', i
        if prev_diff >= 0 and curr_diff < 0:
            return 'death', i
    return None, None


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
print(f"📊 دریافت نمادهای فیوچرز LBank ...")
symbols = get_lbank_futures_symbols()
if not symbols:
    print("❌ هیچ نمادی برای اسکن وجود ندارد!")
    exit()

print(f"🔍 اسکن MACD ({MACD_FAST},{MACD_SLOW},{MACD_SIGNAL}) روی تایم‌فریم ۴ ساعته ...")
print(f"   بررسی {CHECK_CANDLES} کندل آخر بسته‌شده | "
      f"{'فقط کراس صعودی' if ONLY_GOLDEN else 'هر دو کراس صعودی/نزولی'}\n")

results = []
for sym in tqdm(symbols, desc="اسکن MACD 4h"):
    try:
        df_4h = get_4h_data(f"{sym}-USD")
        if df_4h is None or len(df_4h) < MACD_SLOW + MACD_SIGNAL + 5:
            continue

        macd_line, signal_line, hist = compute_macd(df_4h['close'])
        cross_type, cross_idx = detect_cross(macd_line, signal_line, CHECK_CANDLES)

        if cross_type is None:
            continue
        if ONLY_GOLDEN and cross_type != 'golden':
            continue

        candle = df_4h.iloc[cross_idx]
        results.append({
            'symbol':     sym,
            'type':       cross_type,
            'time':       df_4h.index[cross_idx].strftime('%Y-%m-%d %H:%M'),
            'close':      float(candle['close']),
            'macd':       float(macd_line.iloc[cross_idx]),
            'signal':     float(signal_line.iloc[cross_idx]),
            'hist':       float(hist.iloc[cross_idx]),
            'last_close': float(df_4h['close'].iloc[-2]),   # آخرین کندل بسته‌شده
        })
        time.sleep(0.25)
    except Exception:
        continue

if results:
    # مرتب‌سازی: صعودی‌ها اول، سپس بر اساس زمان (جدیدترین اول)
    df_res = pd.DataFrame(results)
    df_res['type_order'] = df_res['type'].map({'golden': 0, 'death': 1})
    df_res = df_res.sort_values(['type_order', 'time'], ascending=[True, False])

    goldens = df_res[df_res['type'] == 'golden']
    deaths  = df_res[df_res['type'] == 'death']

    lines = []
    lines.append(f"📈 <b>کراس‌های MACD در تایم‌فریم ۴h (LBank Futures)</b>")
    lines.append(f"پارامترها: ({MACD_FAST},{MACD_SLOW},{MACD_SIGNAL}) | "
                 f"بررسی {CHECK_CANDLES} کندل آخر\n")

    lines.append(f"🟢 <b>کراس صعودی (Golden Cross): {len(goldens)} نماد</b>")
    for _, row in goldens.head(SHOW_N).iterrows():
        lines.append(
            f"  • <b>{row['symbol']}</b> | {row['time']}\n"
            f"    Close={row['close']:.6g} | MACD={row['macd']:.4g} | "
            f"Signal={row['signal']:.4g} | Hist={row['hist']:.4g}"
        )

    if not ONLY_GOLDEN:
        lines.append(f"\n🔴 <b>کراس نزولی (Death Cross): {len(deaths)} نماد</b>")
        for _, row in deaths.head(SHOW_N).iterrows():
            lines.append(
                f"  • <b>{row['symbol']}</b> | {row['time']}\n"
                f"    Close={row['close']:.6g} | MACD={row['macd']:.4g} | "
                f"Signal={row['signal']:.4g} | Hist={row['hist']:.4g}"
            )

    lines.append(f"\n📅 ارزهای اسکن‌شده: {len(symbols)}")
    message = "\n".join(lines)

    send_telegram_message(message)
    print("\n" + message)
else:
    print("\n❌ هیچ کراس MACD یافت نشد.")
    send_telegram_message("❌ در اسکن MACD ۴h امروز هیچ کراسی یافت نشد.")

print("\n✅ اسکن کامل شد!")
