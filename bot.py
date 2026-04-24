import os
import json
import asyncio
import requests
import logging
import random
import time
from datetime import datetime, timedelta
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
FOOD_HISTORY_FILE = "food_history.json"

TIME_SLOTS = [9, 12, 18, 22]
TIME_LABELS = {9: "早上", 12: "中午", 18: "傍晚", 22: "夜晚"}

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

def load_food_history():
    if os.path.exists(FOOD_HISTORY_FILE):
        with open(FOOD_HISTORY_FILE) as f:
            return json.load(f)
    return []

def save_food(name):
    history = load_food_history()
    history.append({"name": name, "time": datetime.now().isoformat()})
    cutoff = datetime.now() - timedelta(days=7)
    history = [h for h in history if datetime.fromisoformat(h["time"]) > cutoff]
    with open(FOOD_HISTORY_FILE, "w") as f:
        json.dump(history, f, ensure_ascii=False)

def get_recent_foods(days=3):
    history = load_food_history()
    cutoff = datetime.now() - timedelta(days=days)
    return [h["name"] for h in history if datetime.fromisoformat(h["time"]) > cutoff]

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

def get_hourly_weather(lat, lon):
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&hourly=temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code"
        f"&forecast_days=1"
        f"&timezone=auto"
    )
    for attempt in range(3):
        try:
            data = requests.get(url, timeout=20).json()
            hourly = data["hourly"]
            result = {}
            for i, t in enumerate(hourly["time"]):
                hour = int(t.split("T")[1].split(":")[0])
                result[hour] = {
                    "temp": hourly["temperature_2m"][i],
                    "code": hourly["weather_code"][i],
                    "humidity": hourly["relative_humidity_2m"][i],
                    "wind": round(hourly["wind_speed_10m"][i])
                }
            return result
        except Exception as e:
            if attempt == 2:
                raise
            time.sleep(2)

def get_current_weather(lat, lon):
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&current=temperature_2m,weather_code"
        f"&timezone=auto"
    )
    for attempt in range(3):
        try:
            c = requests.get(url, timeout=20).json()["current"]
            return c["temperature_2m"], c["weather_code"]
        except Exception as e:
            if attempt == 2:
                raise
            time.sleep(2)

def find_nearby_restaurants(lat, lon, radius=800):
    query = f"""
    [out:json][timeout:10];
    (
      node["amenity"="restaurant"](around:{radius},{lat},{lon});
      node["amenity"="cafe"](around:{radius},{lat},{lon});
      node["amenity"="fast_food"](around:{radius},{lat},{lon});
    );
    out body 30;
    """
    try:
        r = requests.post("https://overpass-api.de/api/interpreter", data={"data": query}, timeout=15)
        elements = r.json().get("elements", [])
        restaurants = []
        for e in elements:
            tags = e.get("tags", {})
            name = tags.get("name") or tags.get("name:zh") or tags.get("name:en")
            if not name:
                continue
            restaurants.append({
                "name": name,
                "cuisine": tags.get("cuisine", "").replace("_", " ").replace(";", ", "),
                "amenity": tags.get("amenity", ""),
                "lat": e.get("lat"),
                "lon": e.get("lon")
            })
        return restaurants
    except Exception as e:
        logging.error(f"Overpass API error: {e}")
        return []

def get_day_outfit_advice(city, slots_data):
    slots_str = "\n".join([
        f"- {TIME_LABELS[h]} {h}:00: {t}℃，{d}"
        for h, t, d in slots_data
    ])
    temps = [t for _, t, _ in slots_data]
    temp_range = f"{min(temps):.0f}°C 到 {max(temps):.0f}°C"

    prompt = (
        f"今天{city}全天温度分布：\n{slots_str}\n"
        f"全天温差 {temp_range}。\n\n"
        f"请给出一套保险的核心穿搭（以中位温度为基准），再给2-3条加减衣提示。\n"
        f"格式：\n"
        f"👗 *核心搭配*\n（一句话描述上衣+下装+鞋子+外套）\n\n"
        f"⏱ *加减时机*\n• ...\n• ...\n\n"
        f"简洁直接，不用多余客套。"
    )
    try:
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={GEMINI_KEY}",
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=15
        )
        data = resp.json()
        if "candidates" in data:
            return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        logging.error(f"Gemini 穿搭失败: {e}")
    return fallback_day_outfit(slots_data)

def fallback_day_outfit(slots_data):
    temps = [t for _, t, _ in slots_data]
    median = sorted(temps)[len(temps)//2]
    diff = max(temps) - min(temps)

    if median < 5:
        base = "羽绒服 + 厚毛衣 + 长裤 + 保暖靴"
    elif median < 12:
        base = "大衣 + 毛衣 + 长裤 + 休闲鞋"
    elif median < 18:
        base = "风衣 + 长袖 + 牛仔裤 + 运动鞋"
    elif median < 24:
        base = "薄卫衣或衬衫 + 长裤 + 运动鞋"
    else:
        base = "短袖 + 薄长裤 + 帆布鞋"

    tips = []
    if diff >= 8:
        tips.append(f"温差较大（{diff:.0f}°C），外套早晚必备")
    coldest = min(slots_data, key=lambda x: x[1])
    hottest = max(slots_data, key=lambda x: x[1])
    tips.append(f"{coldest[0]}:00 最冷（{coldest[1]:.0f}°C），加衣")
    tips.append(f"{hottest[0]}:00 最暖（{hottest[1]:.0f}°C），可减衣")

    tips_str = "\n".join([f"• {t}" for t in tips])
    return f"👗 *核心搭配*\n{base}\n\n⏱ *加减时机*\n{tips_str}"

def pick_restaurant_with_ai(restaurants, meal_type, weather_desc, temp, recent_foods):
    if not restaurants:
        return None, "附近没找到餐厅 😢"
    rest_list = "\n".join([
        f"- {r['name']}" + (f"（{r['cuisine']}）" if r['cuisine'] else "")
        for r in restaurants[:25]
    ])
    recent_str = "、".join(recent_foods) if recent_foods else "无"
    prompt = (
        f"我现在要吃{meal_type}，今天天气{weather_desc}，温度{temp}℃。\n"
        f"最近吃过（请避免类似）：{recent_str}\n\n"
        f"附近餐厅候选：\n{rest_list}\n\n"
        f"请从上面选一家推荐给我，并用1-2句话说明为什么适合现在。\n"
        f"格式：\n**餐厅名**\n推荐理由"
    )
    try:
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={GEMINI_KEY}",
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=15
        )
        data = resp.json()
        if "candidates" in data:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            matched = None
            for r in restaurants:
                if r["name"] in text:
                    matched = r
                    break
            return matched, text
    except Exception as e:
        logging.error(f"Gemini 餐厅推荐失败: {e}")
    chosen = random.choice(restaurants[:10])
    return chosen, f"**{chosen['name']}**\n随机为你选了这家 🎲"

async def send_daily_outfit(app):
    loc = load_location()
    if not loc:
        await app.bot.send_message(
            chat_id=CHAT_ID,
            text="📍 还没有你的位置信息！请在 Telegram 里发送你的位置给我。"
        )
        return
    try:
        lat, lon = loc["lat"], loc["lon"]
        city = get_city_name(lat, lon)
        hourly = get_hourly_weather(lat, lon)

        slots_data = []
        slots_display = []
        for h in TIME_SLOTS:
            if h in hourly:
                d = hourly[h]
                desc = WEATHER_DESC.get(d["code"], "未知")
                emoji = WEATHER_EMOJI.get(d["code"], "🌡")
                slots_data.append((h, d["temp"], desc))
                slots_display.append(
                    f"⏰ `{h:02d}:00`  {emoji}  *{d['temp']:.0f}°C*  {desc}"
                )

        temps = [t for _, t, _ in slots_data]
        temp_diff = max(temps) - min(temps)

        advice = get_day_outfit_advice(city, slots_data)

        msg = (
            f"🗓 *{city} 今日全天天气*\n\n"
            + "\n".join(slots_display)
            + f"\n\n📊 全天温差：*{temp_diff:.0f}°C*（{min(temps):.0f}° ~ {max(temps):.0f}°）\n\n"
            + advice
        )
        await app.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
    except Exception as e:
        await app.bot.send_message(chat_id=CHAT_ID, text=f"⚠️ 获取天气失败：{e}")

async def send_meal_recommendation(app, meal_type):
    loc = load_location()
    if not loc:
        await app.bot.send_message(chat_id=CHAT_ID, text="📍 还没有位置信息！")
        return
    try:
        lat, lon = loc["lat"], loc["lon"]
        temp, code = get_current_weather(lat, lon)
        desc = WEATHER_DESC.get(code, "未知天气")
        restaurants = find_nearby_restaurants(lat, lon)
        recent = get_recent_foods()
        chosen, text = pick_restaurant_with_ai(restaurants, meal_type, desc, temp, recent)
        emoji = "🍜" if meal_type == "午餐" else "🍽"
        msg = f"{emoji} *今日{meal_type}推荐*\n\n{text}"
        if chosen:
            maps_url = f"https://www.google.com/maps/search/?api=1&query={chosen['lat']},{chosen['lon']}"
            msg += f"\n\n📍 [在地图中查看]({maps_url})"
            msg += f"\n\n吃完后发 `/ate {chosen['name']}` 记录"
        await app.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        await app.bot.send_message(chat_id=CHAT_ID, text=f"⚠️ 获取餐厅失败：{e}")

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loc = update.message.location
    save_location(loc.latitude, loc.longitude)
    city = get_city_name(loc.latitude, loc.longitude)
    await update.message.reply_text(
        f"✅ 位置已更新：{city}\n"
        f"每天 07:00 推送全天天气穿搭计划。"
    )

async def handle_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ 正在获取全天天气穿搭…")
    await send_daily_outfit(context.application)

async def handle_lunch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🍜 正在挑午餐…")
    await send_meal_recommendation(context.application, "午餐")

async def handle_dinner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🍽 正在挑晚餐…")
    await send_meal_recommendation(context.application, "晚餐")

async def handle_ate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("用法：/ate 菜名或餐厅名")
        return
    name = " ".join(context.args)
    save_food(name)
    await update.message.reply_text(f"✅ 已记录：{name}")

async def handle_recent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    recent = get_recent_foods(days=7)
    if not recent:
        await update.message.reply_text("📝 最近7天还没有记录")
    else:
        text = "📝 *最近7天吃过*：\n\n" + "\n".join([f"• {f}" for f in recent])
        await update.message.reply_text(text, parse_mode="Markdown")

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 你好！我是你的生活助手\n\n"
        "📍 发送位置给我（每次到新城市发一次）\n\n"
        "*指令：*\n"
        "/now — 获取全天天气穿搭计划\n"
        "/lunch — 午餐推荐\n"
        "/dinner — 晚餐推荐\n"
        "/ate 菜名 — 记录吃过的\n"
        "/recent — 查看最近吃过什么\n\n"
        "*自动推送：*\n"
        "• 07:00 全天天气 + 穿搭\n"
        "• 11:30 午餐推荐\n"
        "• 17:30 晚餐推荐",
        parse_mode="Markdown"
    )

def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("now", handle_now))
    app.add_handler(CommandHandler("lunch", handle_lunch))
    app.add_handler(CommandHandler("dinner", handle_dinner))
    app.add_handler(CommandHandler("ate", handle_ate))
    app.add_handler(CommandHandler("recent", handle_recent))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))

    scheduler = AsyncIOScheduler(timezone="Europe/Paris")
    scheduler.add_job(send_daily_outfit, "cron", hour=7, minute=0, args=[app])
    scheduler.add_job(send_meal_recommendation, "cron", hour=11, minute=30, args=[app, "午餐"])
    scheduler.add_job(send_meal_recommendation, "cron", hour=17, minute=30, args=[app, "晚餐"])
    scheduler.start()

    print("Bot 启动成功 ✅")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
