import os
import pandas as pd
import numpy as np
import yfinance as yf
from tqdm import tqdm
import time
import requests
import ccxt

# ---------- تنظیمات ----------
SHOW_N        = 30          # حداکثر تعداد نتیجه در هر دسته (اگر None باشه = همه)
MACD_FAST     = 12
MACD_SLOW     = 26
MACD_SIGNAL   = 9
CHECK_CANDLES = 2           # چند کندل آخر ۴h برای بررسی کراس
ONLY_GOLDEN   = False       # True = فقط کراس صعودی

# --- تنظیمات Pagination تلگرام ---
MAX_MSG_LEN   = 3800        # حداکثر طول هر پیام (کمتر از 4096 برای امنیت)
SEND_DELAY    = 1.2         # ثانیه، فاصله بین پیام‌ها (برای rate limit)

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
    """آخرین کراس رخ‌داده در 'lookback' کندل اخیر."""
    n = len(macd_line)
    if n < 3:
        return None, None
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


# ---------- تلگرام با Pagination ----------
def _split_message(text, max_len=MAX_MSG_LEN):
    """
    متن رو به قطعه‌های ≤ max_len تقسیم می‌کنه،
    بدون شکستن وسط یک خط (line-aware).
    """
    lines = text.split('\n')
    chunks = []
    current = ""

    for line in lines:
        # اگر خود خط از max_len بزرگ‌تره، به زور تکه‌تکه‌اش کن
        while len(line) > max_len:
            if current:
                chunks.append(current.rstrip())
                current = ""
            chunks.append(line[:max_len])
            line = line[max_len:]

        candidate = (current + '\n' + line) if current else line
        if len(candidate) > max_len:
            chunks.append(current.rstrip())
            current = line
        else:
            current = candidate

    if current:
        chunks.append(current.rstrip())
    return chunks


def send_telegram_message(text, title=None):
    """
    پیام رو (در صورت نیاز) به چند بخش تقسیم کرده و ارسال می‌کند.
    """
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        print("❌ توکن یا chat_id تنظیم نشده است.")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    # اگر title داریم، به ابتدای متن اضافه می‌کنیم
    if title:
        text = f"{title}\n\n{text}"

    chunks = _split_message(text, MAX_MSG_LEN)
    total = len(chunks)
    print(f"📨 در حال ارسال {total} پیام به تلگرام ...")

    for i, chunk in enumerate(chunks, start=1):
        # شماره‌گذاری پیام‌ها در صورت تعدد
        header = f"<b>[{i}/{total}]</b>\n" if total > 1 else ""
        body = header + chunk

        payload = {
            'chat_id': chat_id,
            'text': body,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True,
        }
        try:
            r = requests.post(url, data=payload, timeout=15)
            if r.status_code != 200:
                print(f"⚠️ خطا در ارسال پیام {i}/{total}: {r.text}")
                # اگر خطای HTML بود، دوباره بدون parse_mode امتحان کن
                if 'parse' in r.text.lower() or "can't parse" in r.text.lower():
                    payload.pop('parse_mode', None)
                    r2 = requests.post(url, data=payload, timeout=15)
                    if r2.status_code == 200:
                        print(f"✅ پیام {i}/{total} بدون فرمت ارسال شد.")
                    else:
                        print(f"❌ تلاش دوم هم شکست خورد: {r2.text}")
            else:
                print(f"✅ پیام {i}/{total} ارسال شد.")
        except Exception as e:
            print(f"❌ خطا در ارسال پیام {i}/{total}: {e}")

        if i < total:
            time.sleep(SEND_DELAY)  # جلوگیری از rate limit تلگرام


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
            'last_close': float(df_4h['close'].iloc[-2]),
        })
        time.sleep(0.25)
    except Exception:
        continue

# ---------- ساخت و ارسال پیام ----------
if results:
    df_res = pd.DataFrame(results)
    df_res['type_order'] = df_res['type'].map({'golden': 0, 'death': 1})
    df_res = df_res.sort_values(['type_order', 'time'], ascending=[True, False])

    goldens = df_res[df_res['type'] == 'golden']
    deaths  = df_res[df_res['type'] == 'death']

    # اگر SHOW_N = None باشه، همه نمایش داده می‌شن
    g_show = goldens if SHOW_N is None else goldens.head(SHOW_N)
    d_show = deaths  if SHOW_N is None else deaths.head(SHOW_N)

    header_title = (
        f"📈 <b>کراس‌های MACD — تایم‌فریم ۴h (LBank Futures)</b>\n"
        f"پارامترها: ({MACD_FAST},{MACD_SLOW},{MACD_SIGNAL}) | "
        f"بررسی {CHECK_CANDLES} کندل آخر\n"
        f"🟢 صعودی: <b>{len(goldens)}</b>   |   🔴 نزولی: <b>{len(deaths)}</b>"
    )

    lines = []
    lines.append(f"🟢 <b>کراس صعودی (Golden Cross)</b> — نمایش {len(g_show)} از {len(goldens)}")
    if len(g_show) == 0:
        lines.append("  (موردی یافت نشد)")
    else:
        for _, row in g_show.iterrows():
            lines.append(
                f"  • <b>{row['symbol']}</b> | {row['time']}\n"
                f"    Close={row['close']:.6g} | MACD={row['macd']:.4g} | "
                f"Signal={row['signal']:.4g} | Hist={row['hist']:.4g}"
            )

    if not ONLY_GOLDEN:
        lines.append("")
        lines.append(f"🔴 <b>کراس نزولی (Death Cross)</b> — نمایش {len(d_show)} از {len(deaths)}")
        if len(d_show) == 0:
            lines.append("  (موردی یافت نشد)")
        else:
            for _, row in d_show.iterrows():
                lines.append(
                    f"  • <b>{row['symbol']}</b> | {row['time']}\n"
                    f"    Close={row['close']:.6g} | MACD={row['macd']:.4g} | "
                    f"Signal={row['signal']:.4g} | Hist={row['hist']:.4g}"
                )

    lines.append("")
    lines.append(f"📅 ارزهای اسکن‌شده: {len(symbols)}")

    body = "\n".join(lines)
    send_telegram_message(body, title=header_title)
    print("\n" + body)
else:
    print("\n❌ هیچ کراس MACD یافت نشد.")
    send_telegram_message(
        "❌ در اسکن MACD ۴h امروز هیچ کراسی یافت نشد.",
        title="📈 <b>اسکن MACD — تایم‌فریم ۴h (LBank Futures)</b>"
    )

print("\n✅ اسکن کامل شد!")
