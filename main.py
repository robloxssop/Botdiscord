import discord
from discord.ext import tasks
from discord import app_commands
import requests
import os

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")

class StockBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.user_targets = {}   # uid → {symbol: target}
        self.last_alerts = {}    # uid → {symbol: msg_obj}
        self.notify_dm = {}      # uid → bool (true = ส่ง DM)

    async def setup_hook(self):
        await self.tree.sync()
        check_targets.start()

bot = StockBot()

# ==================== ฟังก์ชันดึงราคาหุ้น ====================
def get_stock_price(symbol: str):
    url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={FINNHUB_API_KEY}"
    try:
        r = requests.get(url, timeout=5)
        r.raise_for_status()
        data = r.json()
        return data.get("c", None)
    except Exception as e:
        print(f"[ERROR] Finnhub: {e}")
        return None

# ==================== Slash Commands ====================
@bot.tree.command(name="set", description="ตั้งเป้าหมายหุ้น เช่น /set AAPL 170")
async def set_target(interaction: discord.Interaction, symbol: str, target: float):
    uid = interaction.user.id
    symbol = symbol.upper()
    if uid not in bot.user_targets:
        bot.user_targets[uid] = {}
    bot.user_targets[uid][symbol] = target
    await interaction.response.send_message(
        f"✅ {interaction.user.mention} ตั้งเป้าหมาย {symbol} = {target}"
    )

@bot.tree.command(name="all", description="ดูเป้าหมายทั้งหมดของเรา")
async def all_targets(interaction: discord.Interaction):
    uid = interaction.user.id
    targets = bot.user_targets.get(uid, {})
    if not targets:
        await interaction.response.send_message("ยังไม่ได้ตั้งเป้าหมาย")
    else:
        msg = "\n".join([f"{s} → {t}" for s, t in targets.items()])
        await interaction.response.send_message(f"🎯 เป้าหมายของคุณ:\n{msg}")

@bot.tree.command(name="remove", description="ลบเป้าหมายหุ้น")
async def remove_target(interaction: discord.Interaction, symbol: str):
    uid = interaction.user.id
    symbol = symbol.upper()
    if uid in bot.user_targets and symbol in bot.user_targets[uid]:
        del bot.user_targets[uid][symbol]
        await interaction.response.send_message(f"🗑 ลบเป้าหมาย {symbol} แล้ว")
    else:
        await interaction.response.send_message(f"ไม่มีเป้าหมาย {symbol}")

@bot.tree.command(name="check", description="เช็คราคาหุ้น (ถ้าไม่ใส่ symbol จะเช็คทั้งหมด)")
async def check_price(interaction: discord.Interaction, symbol: str = None):
    uid = interaction.user.id
    if symbol:
        price = get_stock_price(symbol.upper())
        if price is None:
            await interaction.response.send_message("⚠️ ดึงราคาล้มเหลว")
        else:
            await interaction.response.send_message(f"💹 {symbol.upper()} = {price}")
    else:
        targets = bot.user_targets.get(uid, {})
        if not targets:
            await interaction.response.send_message("ยังไม่มีเป้าหมาย")
            return
        msgs = []
        for sym in targets:
            price = get_stock_price(sym)
            if price:
                msgs.append(f"{sym} = {price}")
        await interaction.response.send_message("💹 " + " | ".join(msgs))

@bot.tree.command(name="notifydm", description="เลือกว่าจะให้ส่งแจ้งเตือนทาง DM หรือไม่")
async def notify_dm(interaction: discord.Interaction, option: str):
    uid = interaction.user.id
    if option.lower() == "on":
        bot.notify_dm[uid] = True
        await interaction.response.send_message("✅ เปิดแจ้งเตือนทาง DM แล้ว")
    elif option.lower() == "off":
        bot.notify_dm[uid] = False
        await interaction.response.send_message("❌ ปิดแจ้งเตือนทาง DM แล้ว")
    else:
        await interaction.response.send_message("กรุณาใช้ on หรือ off")

# ==================== Loop ตรวจหุ้น ====================
@tasks.loop(minutes=1.0)
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
                # ลบแจ้งเตือนเก่า
                if sym in bot.last_alerts[uid]:
                    try:
                        await bot.last_alerts[uid][sym].delete()
                    except:
                        pass
                # ส่ง DM ถ้าเลือก
                if bot.notify_dm.get(uid, True):  # default = True
                    msg = await user.send(f"🚨 {sym} = {price} (<= {target})")
                    bot.last_alerts[uid][sym] = msg

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    check_targets.start()

bot.run(DISCORD_TOKEN)
