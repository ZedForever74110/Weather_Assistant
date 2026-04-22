import os
import json
import asyncio
import requests
import logging
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

if not TOKEN:
    raise ValueError("TELEGRAM_TOKEN 未设置")
LOCATION_FILE = "location.json"

WEATHER_DESC = {
    0:"晴天",1:"大部晴朗",2:"局部多云",3:"阴天",
    45:"雾",48:"冻雾",51:"毛毛雨",53:"中等毛毛雨",55:"浓密毛毛雨",
    61:"小雨",63:"中雨",65:"大雨",71:"小雪",73:"中雪",75:"大雪",
    80:"阵雨",81:"中等阵雨",82:"强阵雨",95:"雷雨",99:"强雷暴"
}

WEATHER_EMOJI = {
    0:"☀️",1:"🌤️",2:"⛅",3:"☁️",45:"🌫️",48:"🌫️",
    51:"🌦️",53:"🌦️",55:"🌧️",61:"🌧️",63:"🌧️",65:"⛈️",
    71:"❄️",73:"❄️",75:"❄️",80:"🌦️",81:"🌧️",82:"⛈️",
    95:"⛈️",99:"⛈️"
}

def load_location():
    if os.path.exists(LOCATION_FILE):
        with open(LOCATION_FILE) as f:
            return json.load(f)
    return None

def save_location(lat, lon):
    with open(LOCATION_FILE, "w") as f:
        json.dump({"lat": lat, "lon": lon}, f)

def get_city_name(lat, lon):
    try:
        r = requests.get(
            f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lon}&format=json&accept-language=zh",
            headers={"User-Agent": "WeatherOutfitBot/1.0"},
            timeout=5
        )
        a = r.json().get("address", {})
        return a.get("city") or a.get("town") or a.get("county") or a.get("state") or "当前位置"
    except:
        return "当前位置"

def get_weather(lat, lon):
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&current=temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code"
        f"&timezone=auto"
    )
    c = requests.get(url, timeout=10).json()["current"]
    return c["temperature_2m"], c["relative_humidity_2m"], round(c["wind_speed_10m"]), c["weather_code"]

def get_outfit_advice(city, temp, humidity, wind, weather_desc):
    prompt = (
        f"今天{city}天气：{weather_desc}，温度{temp}℃，湿度{humidity}%，风速{wind}km/h。"
        f"请给出简洁的今日穿搭建议，包括上衣、下装、外套（如需）、鞋子，约80字，直接给建议。"
    )
    resp = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={GEMINI_KEY}",
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=15
    )
    return resp.json()["candidates"][0]["content"]["parts"][0]["text"]

async def send_daily_outfit(app):
    loc = load_location()
    if not loc:
        await app.bot.send_message(
            chat_id=CHAT_ID,
            text="📍 还没有你的位置信息！\n请在 Telegram 里发送你的位置给我，之后每天会自动推送穿搭建议。"
        )
        return

    try:
        lat, lon = loc["lat"], loc["lon"]
        city = get_city_name(lat, lon)
        temp, humidity, wind, code = get_weather(lat, lon)
        desc = WEATHER_DESC.get(code, "未知天气")
        emoji = WEATHER_EMOJI.get(code, "🌡️")
        advice = get_outfit_advice(city, temp, humidity, wind, desc)

        msg = (
            f"{emoji} *{city} 今日穿搭建议*\n\n"
            f"🌡 温度：{temp}°C　💧 湿度：{humidity}%　💨 风速：{wind} km/h\n"
            f"天气：{desc}\n\n"
            f"👗 *穿搭建议*\n{advice}"
        )
        await app.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
    except Exception as e:
        await app.bot.send_message(chat_id=CHAT_ID, text=f"⚠️ 获取天气失败：{e}")

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loc = update.message.location
    save_location(loc.latitude, loc.longitude)
    city = get_city_name(loc.latitude, loc.longitude)
    await update.message.reply_text(
        f"✅ 位置已更新为：{city}\n每天早上 7:00 会根据这里的天气推送穿搭建议！"
    )

async def handle_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ 正在获取天气和穿搭建议…")
    await send_daily_outfit(context.application)

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 你好！我是你的每日穿搭助手。\n\n"
        "📍 发送你的位置给我，我会每天早上 7:00 自动推送当地天气和穿搭建议。\n\n"
        "指令：\n"
        "/now — 立即获取今日穿搭建议\n"
        "发送位置 — 更新你的位置"
    )

def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("now", handle_now))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))

    scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(
        send_daily_outfit,
        "cron",
        hour=7, minute=0,
        args=[app]
    )
    scheduler.start()

    print("Bot 启动成功 ✅")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
