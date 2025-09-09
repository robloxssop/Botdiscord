import os
import discord
from discord.ext import tasks, commands
import requests
import time

# ====================
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ====================
user_targets = {}  # user_id -> {symbol: target}
last_reset_day = time.localtime().tm_mday

# ====================
def get_stock_price(symbol):
    url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={FINNHUB_API_KEY}"
    try:
        r = requests.get(url)
        data = r.json()
        return data.get("c")
    except:
        return None

# ====================
@tasks.loop(seconds=300)  # ตรวจสอบทุก 5 นาที
async def check_stocks():
    global last_reset_day
    today_day = time.localtime().tm_mday
    if today_day != last_reset_day:
        user_targets.clear()
        last_reset_day = today_day

    for user_id, targets in user_targets.items():
        user = bot.get_user(user_id)
        if not user:
            continue
        for symbol, target in targets.items():
            price = get_stock_price(symbol)
            if price is None:
                continue
            if price < target:
                try:
                    await user.send(f"⚠️ หุ้น {symbol} ราคาปัจจุบัน {price} ยังต่ำกว่าเป้าหมาย {target}")
                except:
                    pass

# ====================
# ===== คำสั่ง Discord =====
@bot.command()
async def set(ctx, symbol: str, target: float):
    symbol = symbol.upper()
    user_id = ctx.author.id
    user_targets.setdefault(user_id, {})[symbol] = target
    await ctx.send(f"✅ ตั้งเป้าหมายหุ้น {symbol} ที่ {target} เรียบร้อยแล้ว!")

@bot.command()
async def all(ctx):
    user_id = ctx.author.id
    targets = user_targets.get(user_id, {})
    if not targets:
        await ctx.send("คุณยังไม่ได้ตั้งเป้าหมายหุ้นใด ๆ")
        return
    msg = "📊 เป้าหมายหุ้นของคุณ:\n"
    for symbol, target in targets.items():
        msg += f"- {symbol}: {target}\n"
    await ctx.send(msg)

@bot.command()
async def check(ctx):
    user_id = ctx.author.id
    targets = user_targets.get(user_id, {})
    if not targets:
        await ctx.send("คุณยังไม่ได้ตั้งเป้าหมายหุ้นใด ๆ")
        return
    for symbol, target in targets.items():
        price = get_stock_price(symbol)
        if price is None:
            await ctx.send(f"❌ ไม่สามารถดึงราคาหุ้น {symbol} ได้")
            continue
        if price < target:
            await ctx.send(f"⚠️ หุ้น {symbol} ราคาปัจจุบัน {price} ยังต่ำกว่าเป้าหมาย {target}")
        else:
            await ctx.send(f"✅ หุ้น {symbol} ราคาปัจจุบัน {price} เกินหรือถึงเป้าหมายแล้ว")

@bot.command()
async def delete(ctx, symbol: str):
    symbol = symbol.upper()
    user_id = ctx.author.id
    targets = user_targets.get(user_id, {})
    if symbol in targets:
        del targets[symbol]
        await ctx.send(f"🗑️ ลบเป้าหมายหุ้น {symbol} เรียบร้อยแล้ว")
    else:
        await ctx.send(f"❌ คุณไม่มีเป้าหมายหุ้น {symbol}")

@bot.command(name="helpme")
async def help_command(ctx):
    help_text = (
        "📌 คำสั่งทั้งหมด:\n"
        "`!set SYMBOL PRICE` - ตั้งเป้าหมายหุ้นส่วนตัว\n"
        "`!all` - ดูเป้าหมายหุ้นของตัวเอง\n"
        "`!check` - ตรวจสอบราคาหุ้นของตัวเอง\n"
        "`!delete SYMBOL` - ลบเป้าหมายหุ้นตัวเอง\n"
        "`!helpme` - แสดงคำสั่งทั้งหมด"
    )
    await ctx.send(help_text)

# ====================
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    check_stocks.start()

# ====================
if DISCORD_TOKEN:
    bot.run(DISCORD_TOKEN)
else:
    print("กรุณาตั้งค่า DISCORD_TOKEN ใน Secrets")
