import os
import requests

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def fetch_top_traders(limit=10):
    """
    دریافت ۱۰ معامله‌گر برتر از API عمومی Hyperliquid.
    برای منبع دیگر (Binance, eToro و...) این تابع را تغییر بده.
    """
    url = "https://api.hyperliquid.xyz/info"
    payload = {"type": "leaderboard"}
    
    try:
        response = requests.post(url, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        # مرتب‌سازی بر اساس سود (PnL) نزولی
        sorted_traders = sorted(
            data.get("leaderboard", []),
            key=lambda x: x.get("pnl", 0),
            reverse=True
        )[:limit]
        
        return sorted_traders
    
    except Exception as e:
        print(f"خطا در دریافت داده: {e}")
        return []

def format_message(traders):
    """قالب‌بندی پیام تلگرام"""
    if not traders:
        return "⚠️ داده‌ای برای نمایش یافت نشد."
    
    msg = "🏆 **۱۰ معامله‌گر برتر (بر اساس سود)**\n\n"
    for i, t in enumerate(traders, 1):
        address = t.get("address", "نامشخص")[:10] + "..."
        pnl = t.get("pnl", 0)
        roi = t.get("roi", "N/A")
        msg += (
            f"{i}. `{address}`\n"
            f"   💰 سود: **${pnl:,.2f}**\n"
            f"   📈 بازدهی: {roi}\n\n"
        )
    return msg

def send_telegram(message):
    """ارسال پیام به تلگرام"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("خطا: توکن یا Chat ID تنظیم نشده است.")
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        res = requests.post(url, json=payload, timeout=30)
        res.raise_for_status()
        print("پیام با موفقیت ارسال شد.")
    except Exception as e:
        print(f"خطا در ارسال پیام: {e}")

def main():
    traders = fetch_top_traders(limit=10)
    message = format_message(traders)
    send_telegram(message)

if __name__ == "__main__":
    main()
