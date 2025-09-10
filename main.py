import discord
from discord.ext import tasks
from discord import app_commands
import requests
import os
import matplotlib.pyplot as plt
import io
from datetime import datetime

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")

class StockBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.user_targets = {}   # uid → {symbol: target}
        self.last_alerts = {}    # uid → {symbol: msg_obj}
        self.notify_dm = {}      # uid → bool

    async def setup_hook(self):
        await self.tree.sync()
        check_targets.start()

bot = StockBot()

# ==================== API ราคาหุ้น ====================
def get_stock_price(symbol: str):
    url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={FINNHUB_API_KEY}"
    try:
        r = requests.get(url, timeout=5)
        r.raise_for_status()
        return r.json().get("c")
    except Exception as e:
        print(f"[ERROR] Finnhub: {e}")
        return None

# ดึงข้อมูลย้อนหลังสำหรับกราฟ
def get_stock_history(symbol: str, resolution="5", count=50):
    url = f"https://finnhub.io/api/v1/stock/candle?symbol={symbol}&resolution={resolution}&count={count}&token={FINNHUB_API_KEY}"
    try:
        r = requests.get(url, timeout=5)
        r.raise_for_status()
        data = r.json()
        if data.get("s") != "ok":
            return None, None
        times = [datetime.fromtimestamp(t) for t in data["t"]]
        prices = data["c"]
        return times, prices
    except Exception as e:
        print(f"[ERROR] History: {e}")
        return None, None

# วาดกราฟ
def make_chart(symbol: str):
    times, prices = get_stock_history(symbol)
    if not times or not prices:
        return None
    plt.figure(figsize=(6,3))
    plt.plot(times, prices, marker="o", linestyle="-", color="blue")
    plt.title(f"{symbol} Price History")
    plt.xlabel("Time")
    plt.ylabel("Price")
    plt.grid(True, linestyle="--", alpha=0.6)
    buf = io.BytesIO()
    plt.savefig(buf, format="png")
    buf.seek(0)
    plt.close()
    return buf

# ==================== แจ้งเตือนด้วย Embed + กราฟ ====================
async def send_alert(user: discord.User, symbol: str, price: float, target: float):
    embed = discord.Embed(
        title=f"📉 หุ้น {symbol}",
        description=f"💹 ราคา: **{price}**\n🎯 เป้า: **{target}**",
        color=discord.Color.red() if price <= target else discord.Color.green()
    )
    embed.set_footer(text="Stock Alert Bot • powered by Finnhub")

    # ใส่กราฟถ้ามี
    chart = make_chart(symbol)
    file = None
    if chart:
        file = discord.File(chart, filename="chart.png")
        embed.set_image(url="attachment://chart.png")

    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="เช็คราคา", style=discord.ButtonStyle.primary, custom_id=f"check_{symbol}"))
    view.add_item(discord.ui.Button(label="ลบเป้าหมาย", style=discord.ButtonStyle.danger, custom_id=f"remove_{symbol}"))

    if file:
        msg = await user.send(embed=embed, view=view, file=file)
    else:
        msg = await user.send(embed=embed, view=view)
    return msg

# ==================== Slash Commands ====================
@bot.tree.command(name="set", description="ตั้งเป้าหมายหุ้น เช่น /set AAPL 170")
async def set_target(interaction: discord.Interaction, symbol: str, target: float):
    uid = interaction.user.id
    symbol = symbol.upper()
    if uid not in bot.user_targets:
        bot.user_targets[uid] = {}
    bot.user_targets[uid][symbol] = target
    await interaction.response.send_message(f"✅ {interaction.user.mention} ตั้งเป้า {symbol} = {target}")

@bot.tree.command(name="all", description="ดูเป้าหมายทั้งหมดของคุณ")
async def all_targets(interaction: discord.Interaction):
    uid = interaction.user.id
    targets = bot.user_targets.get(uid, {})
    if not targets:
        await interaction.response.send_message("ยังไม่มีเป้าหมาย")
    else:
        embed = discord.Embed(title="🎯 เป้าหมายของคุณ", color=discord.Color.blue())
        for sym, t in targets.items():
            embed.add_field(name=sym, value=f"เป้า {t}", inline=False)
        await interaction.response.send_message(embed=embed)

@bot.tree.command(name="remove", description="ลบเป้าหมายหุ้น เช่น /remove AAPL")
async def remove_target(interaction: discord.Interaction, symbol: str):
    uid = interaction.user.id
    symbol = symbol.upper()
    if uid in bot.user_targets and symbol in bot.user_targets[uid]:
        del bot.user_targets[uid][symbol]
        await interaction.response.send_message(f"🗑 ลบ {symbol} แล้ว")
    else:
        await interaction.response.send_message(f"❌ ไม่มีเป้าหมาย {symbol}")

@bot.tree.command(name="notifydm", description="เลือกว่าจะให้แจ้งเตือนทาง DM หรือไม่")
async def notify_dm(interaction: discord.Interaction, option: str):
    uid = interaction.user.id
    if option.lower() == "on":
        bot.notify_dm[uid] = True
        await interaction.response.send_message("✅ เปิดแจ้งทาง DM")
    elif option.lower() == "off":
        bot.notify_dm[uid] = False
        await interaction.response.send_message("❌ ปิดแจ้งทาง DM")
    else:
        await interaction.response.send_message("⚠️ ใช้ on หรือ off เท่านั้น")

# ==================== Loop ตรวจสอบหุ้น ====================
@tasks.loop(minutes=1)
async def check_targets():
    for uid, targets in list(bot.user_targets.items()):
        user = await bot.fetch_user(uid)
        if not user:
            continue
        if uid not in bot.last_alerts:
            bot.last_alerts[uid] = {}
        for sym, target in targets.items():
            price = get_stock_price(sym)
            if price is None:
                continue
            if price <= target:
                if sym in bot.last_alerts[uid]:
                    try:
                        await bot.last_alerts[uid][sym].delete()
                    except:
                        pass
                if bot.notify_dm.get(uid, True):
                    msg = await send_alert(user, sym, price, target)
                    bot.last_alerts[uid][sym] = msg

# ==================== Event ====================
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    check_targets.start()

bot.run(DISCORD_TOKEN)
