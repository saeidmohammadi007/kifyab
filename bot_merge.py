#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ادغام نتایج همه chunkها و ارسال Top N به تلگرام
"""
import os
import glob
import json
import time
import datetime as dt
import requests

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
TOP_N = int(os.environ.get("TOP_N", "100"))

S = requests.Session()
S.headers.update({"User-Agent": "hl-scanner-merge/1.0"})


def log(*a):
    print(*a, flush=True)


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


def main():
    now = dt.datetime.now(dt.timezone.utc)

    files = sorted(glob.glob("results_*.json"))
    log(f"فایل‌های یافت‌شده: {files}")

    all_passed = []
    for f in files:
        try:
            with open(f, encoding="utf-8") as fp:
                data = json.load(fp)
                all_passed.extend(data)
                log(f"  {f}: {len(data)} مورد")
        except Exception as e:
            log(f"  خطا در خواندن {f}: {e}")

    # حذف تکراری‌ها بر اساس آدرس
    seen = {}
    for t in all_passed:
        seen[t["addr"]] = t
    passed = list(seen.values())
    passed.sort(key=lambda x: x["avg"], reverse=True)

    log(f"مجموع پس از ادغام: {len(passed)}")

    if not passed:
        tg_send("🔎 اسکن Hyperliquid تمام شد؛ هیچ تریدری با همه شرط‌ها پیدا نشد.")
        return

    head = (f"🏆 نتایج اسکن Hyperliquid ({now:%Y-%m-%d})\n"
            f"تعداد تریدر پیدا شده: {len(passed)}\n"
            f"نمایش {min(TOP_N, len(passed))} نتیجه برتر\n")
    lines = [head]
    for n, t in enumerate(passed[:TOP_N], 1):
        months_txt = " | ".join(f"{m[5:]}: {x*100:.0f}%" for m, x in t["last"])
        lines.append(
            f"{n}) میانگین سود ۶ ماه: <b>{t['avg']*100:.1f}%</b>\n"
            f"<code>{t['addr']}</code>\n"
            f"سابقه: {t['months']} ماه | کمترین ماه: {t['min']*100:.1f}%\n"
            f"وین ریت {t['win_n']} معامله اخیر: <b>{t['win_rate']*100:.1f}%</b>\n"
            f"ارز البانک: {t['ok']}/{t['total']} ({t['share']*100:.0f}%)\n"
            f"ماه‌ها: {months_txt}\n"
        )
    tg_send("\n".join(lines))


if __name__ == "__main__":
    main()
