import os
import json
import asyncio
import requests
import logging
import random
import time
import re
from datetime import datetime, timedelta
from urllib.parse import urljoin
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
FT_COOKIE = os.environ.get("FT_COOKIE", "")

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

FT_SECTIONS = [
    ("Markets", "https://www.ft.com/markets"),
    ("China", "https://www.ft.com/world/asia-pacific/china"),
    ("Tech", "https://www.ft.com/technology"),
]

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# ---------- 位置管理 ----------
def load_location():
    if os.path.exists(LOCATION_FILE):
        with open(LOCATION_FILE) as f:
            return json.load(f)
    return None

def save_location(lat, lon):
    with open(LOCATION_FILE, "w") as f:
        json.dump({"lat": lat, "lon": lon}, f)

# ---------- 饮食历史 ----------
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

# ---------- 位置与天气 ----------
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

# ---------- 附近餐厅 ----------
def find_nearby_restaurants(lat, lon, radius=2000):
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

# ---------- FT 抓取 ----------
def ft_fetch(url, full_article=False):
    """获取 FT 页面 HTML"""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if FT_COOKIE:
        headers["Cookie"] = FT_COOKIE
    try:
        r = requests.get(url, headers=headers, timeout=20)
        return r.text if r.status_code == 200 else None
    except Exception as e:
        logging.error(f"FT fetch error {url}: {e}")
        return None

def ft_get_section_articles(section_url, max_articles=6):
    html = ft_fetch(section_url)
    if not html:
        logging.error(f"❌ 没抓到 HTML: {section_url}")
        return []
    
    logging.info(f"✅ HTML 长度 {len(html)} for {section_url}")
    
    # 检测付费墙/登录页
    if "Subscribe to read" in html:
        logging.warning(f"⚠️ 付费墙: {section_url}")
    if html.count("/content/") < 5:
        logging.warning(f"⚠️ 内容链接很少 ({html.count('/content/')}个)，可能被拦")
    
    pattern = r'<a[^>]*href="(/content/[a-f0-9-]+)"[^>]*data-trackable="heading-link"[^>]*>([^<]+)</a>'
    matches = re.findall(pattern, html)
    if not matches:
        pattern = r'href="(/content/[a-f0-9-]+)"[^>]*aria-label="([^"]+)"'
        matches = re.findall(pattern, html)
    
    logging.info(f"📰 匹配到 {len(matches)} 篇文章")
    
    seen = set()
    articles = []
    for href, title in matches:
        if href in seen:
            continue
        seen.add(href)
        title_clean = re.sub(r'\s+', ' ', title).strip()
        if len(title_clean) < 10:
            continue
        articles.append({
            "url": urljoin("https://www.ft.com", href),
            "title": title_clean
        })
        if len(articles) >= max_articles:
            break
    return articles

def ft_get_article_text(url):
    """提取文章正文"""
    html = ft_fetch(url, full_article=True)
    if not html:
        return ""
    # FT 文章正文在 <div class="article__content-body"> 或 <article> 标签内的 <p>
    paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', html, re.DOTALL)
    text_parts = []
    for p in paragraphs:
        clean = re.sub(r'<[^>]+>', '', p).strip()
        clean = re.sub(r'\s+', ' ', clean)
        if len(clean) > 40:
            text_parts.append(clean)
    return "\n".join(text_parts[:15])  # 最多前15段

def ft_collect_articles():
    """采集所有板块的文章"""
    all_articles = []
    for section_name, section_url in FT_SECTIONS:
        logging.info(f"抓取 FT {section_name}...")
        articles = ft_get_section_articles(section_url, max_articles=5)
        for a in articles:
            a["section"] = section_name
            # 抓正文（每篇控制一下避免太慢）
            a["body"] = ft_get_article_text(a["url"])[:3000]
            all_articles.append(a)
            time.sleep(0.5)
    return all_articles

def ft_summarize(articles):
    """用 Gemini 汇总成3个主题"""
    if not articles:
        return "今天没抓到文章 😢 可能 cookie 失效了"

    articles_text = "\n\n".join([
        f"[{a['section']}] {a['title']}\nURL: {a['url']}\n{a['body'][:1500]}"
        for a in articles
    ])

    prompt = (
        f"以下是今天 Financial Times 的多篇文章（来自 Markets、China、Tech 三个板块）。\n\n"
        f"{articles_text}\n\n"
        f"请用中文为我总结今天最重要的 **3 个主题**。每个主题要求：\n"
        f"1. 用一句话点出核心趋势/事件\n"
        f"2. 用 2-3 句话展开背景和影响\n"
        f"3. 列出涉及的 1-2 篇相关文章标题\n\n"
        f"格式严格按照：\n"
        f"📌 *主题1：xxx*\n"
        f"核心：xxxx\n"
        f"展开：xxxx\n"
        f"相关：[文章标题1] / [文章标题2]\n\n"
        f"📌 *主题2：xxx*\n"
        f"...\n\n"
        f"风格简洁专业，中文表达自然。"
    )
    try:
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={GEMINI_KEY}",
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=30
        )
        data = resp.json()
        if "candidates" in data:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        logging.error(f"Gemini 返回: {data}")
        return "AI 汇总失败 😢"
    except Exception as e:
        logging.error(f"FT 汇总失败: {e}")
        return f"AI 汇总失败：{e}"

# ---------- AI 全天穿搭建议 ----------
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

# ---------- AI 餐厅推荐 ----------
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

# ---------- 任务 ----------
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

async def send_ft_digest(app):
    try:
        await app.bot.send_message(chat_id=CHAT_ID, text="📰 正在抓取 FT...")
        articles = ft_collect_articles()
        if not articles:
            await app.bot.send_message(
                chat_id=CHAT_ID,
                text="⚠️ 没抓到文章，可能 FT_COOKIE 失效，请更新"
            )
            return
        summary = ft_summarize(articles)
        # Telegram 单条消息 4096 字符上限
        msg = f"📰 *Financial Times 今日要闻*\n_{datetime.now().strftime('%Y-%m-%d')}_\n\n{summary}"
        if len(msg) > 4000:
            msg = msg[:3950] + "\n\n_（内容已截断）_"
        await app.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        await app.bot.send_message(chat_id=CHAT_ID, text=f"⚠️ FT 汇总失败：{e}")

# ---------- 命令处理 ----------
async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loc = update.message.location
    save_location(loc.latitude, loc.longitude)
    city = get_city_name(loc.latitude, loc.longitude)
    await update.message.reply_text(f"✅ 位置已更新：{city}")

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

async def handle_ft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📰 正在抓取 FT 并汇总…需要1-2分钟")
    await send_ft_digest(context.application)

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 你好！我是你的生活 + 财经助手\n\n"
        "📍 发送位置给我\n\n"
        "*指令：*\n"
        "/now — 获取全天天气穿搭\n"
        "/ft — 获取今日 FT 重点\n"
        "/lunch — 午餐推荐\n"
        "/dinner — 晚餐推荐\n"
        "/ate 菜名 — 记录吃过的\n"
        "/recent — 查看最近吃过什么\n\n"
        "*自动推送：*\n"
        "• 07:00 全天天气 + 穿搭\n"
        "• 08:00 FT 财经要闻\n"
        "• 11:30 午餐推荐\n"
        "• 17:30 晚餐推荐",
        parse_mode="Markdown"
    )

def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("now", handle_now))
    app.add_handler(CommandHandler("ft", handle_ft))
    app.add_handler(CommandHandler("lunch", handle_lunch))
    app.add_handler(CommandHandler("dinner", handle_dinner))
    app.add_handler(CommandHandler("ate", handle_ate))
    app.add_handler(CommandHandler("recent", handle_recent))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))

    scheduler = AsyncIOScheduler(timezone="Europe/Paris")
    scheduler.add_job(send_daily_outfit, "cron", hour=7, minute=0, args=[app])
    scheduler.add_job(send_ft_digest, "cron", hour=8, minute=0, args=[app])
    scheduler.add_job(send_meal_recommendation, "cron", hour=11, minute=30, args=[app, "午餐"])
    scheduler.add_job(send_meal_recommendation, "cron", hour=17, minute=30, args=[app, "晚餐"])
    scheduler.start()

    print("Bot 启动成功 ✅")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
