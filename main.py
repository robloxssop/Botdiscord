import os
import discord
from discord.ext import tasks, commands
import requests

# ====== ตั้งค่า ======
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "0"))

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# เก็บเป้าหมายของแต่ละคน
# ตัวอย่าง: {user_id: {"AAPL": 170, "TSLA": 200}}
user_targets: dict[int, dict[str, float]] = {}

# ====== ฟังก์ชันดึงราคาหุ้น ======
def get_stock_price(symbol: str) -> float | None:
    url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={FINNHUB_API_KEY}"
    try:
        data = requests.get(url, timeout=10).json()
        return data.get("c")
    except Exception as e:
        print("API error:", e)
        return None

# ====== Loop เช็คราคาอัตโนมัติ ======
@tasks.loop(minutes=5)
async def check_stocks():
    print("⏳ Checking stocks...")
    channel = bot.get_channel(CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        print("⚠️ Channel ไม่ถูกต้อง")
        return

    for user_id, stocks in user_targets.items():
        for symbol, target in stocks.items():
            price = get_stock_price(symbol)
            if price is None:
                continue
            if price <= target:  # แจ้งเตือนอัตโนมัติเมื่อราคาต่ำกว่าหรือเท่ากับเป้า
                user = await bot.fetch_user(user_id)
                await channel.send(f"⚠️ {user.mention} หุ้น {symbol} ราคา {price} ต่ำกว่าหรือเท่ากับเป้าหมาย {target}")

# ====== คำสั่ง ======
@bot.command(name="set")
async def settarget(ctx, symbol: str, target: float):
    symbol = symbol.upper()
    if ctx.author.id not in user_targets:
        user_targets[ctx.author.id] = {}
    user_targets[ctx.author.id][symbol] = target
    await ctx.send(f"✅ {ctx.author.mention} ตั้งเป้าหมายหุ้น {symbol} ที่ {target} เรียบร้อย")

@bot.command(name="all")
async def showtargets(ctx):
    targets = user_targets.get(ctx.author.id, {})
    if not targets:
        await ctx.send("คุณยังไม่ได้ตั้งเป้าหมายหุ้นเลย")
        return
    msg = "📊 เป้าหมายหุ้นของคุณ:\n"
    for symbol, target in targets.items():
        msg += f"- {symbol}: {target}\n"
    await ctx.send(msg)

@bot.command(name="check")
async def checkstock(ctx, symbol: str):
    symbol = symbol.upper()
    price = get_stock_price(symbol)
    if price is None:
        await ctx.send(f"❌ ไม่สามารถดึงราคาของ {symbol}")
        return
    target = user_targets.get(ctx.author.id, {}).get(symbol)
    if target:
        if price <= target:
            await ctx.send(f"⚠️ หุ้น {symbol} ราคา {price} ต่ำกว่าหรือเท่ากับเป้า {target}")
        else:
            await ctx.send(f"✅ หุ้น {symbol} ราคา {price} ยังสูงกว่าเป้า {target}")
    else:
        await ctx.send(f"💹 หุ้น {symbol} ตอนนี้ราคา {price} (ยังไม่ได้ตั้งเป้า)")

@bot.command(name="remove")
async def removetarget(ctx, symbol: str):
    symbol = symbol.upper()
    if ctx.author.id in user_targets and symbol in user_targets[ctx.author.id]:
        del user_targets[ctx.author.id][symbol]
        await ctx.send(f"🗑️ ลบเป้าหมาย {symbol} เรียบร้อยแล้ว")
    else:
        await ctx.send("❌ คุณยังไม่ได้ตั้งเป้าหมายนี้")

# ====== Start ======
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    check_stocks.start()

if DISCORD_TOKEN:
    bot.run(DISCORD_TOKEN)
else:
    print("❌ DISCORD_TOKEN ยังไม่ถูกตั้งค่า")
