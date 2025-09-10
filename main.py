import os
import discord
from discord.ext import tasks
from discord import app_commands
import requests
import datetime

# ====== ENV ======
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "0"))

# ====== Bot Setup ======
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# user_targets: user_id -> {symbol: {"target": float, "dm": bool}}
user_targets: dict[int, dict[str, dict]] = {}

# last_alerts: user_id -> {symbol: message_obj}
last_alerts: dict[int, dict[str, discord.Message]] = {}

# ====== Finnhub API ======
def get_stock_price(symbol: str) -> float | None:
    url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={FINNHUB_API_KEY}"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        return data.get("c")
    except Exception as e:
        print("API Error:", e)
        return None

# ====== Background Task ======
@tasks.loop(minutes=3)  # เช็คทุก 3 นาที
async def check_stocks():
    for user_id, targets in user_targets.items():
        user = await bot.fetch_user(user_id)
        for symbol, info in targets.items():
            price = get_stock_price(symbol)
            if price is None:
                continue
            target = info["target"]
            use_dm = info["dm"]

            # แจ้งเตือนเฉพาะกรณีราคาต่ำกว่าหรือเท่ากับเป้า
            if price <= target:
                msg_text = f"⚠️ หุ้น {symbol} ราคาปัจจุบัน {price} ต่ำกว่าหรือเท่ากับเป้า {target}"

                # ลบข้อความเก่าถ้ามี
                if user_id in last_alerts and symbol in last_alerts[user_id]:
                    try:
                        await last_alerts[user_id][symbol].delete()
                    except Exception:
                        pass

                # ส่งข้อความใหม่
                if use_dm:
                    msg = await user.send(msg_text)
                else:
                    channel = bot.get_channel(CHANNEL_ID)
                    if channel and isinstance(channel, discord.TextChannel):
                        msg = await channel.send(f"{user.mention} {msg_text}")
                    else:
                        continue

                # เก็บข้อความล่าสุด
                if user_id not in last_alerts:
                    last_alerts[user_id] = {}
                last_alerts[user_id][symbol] = msg

# ====== Slash Commands ======
@tree.command(name="set", description="ตั้งเป้าหมายราคาหุ้น")
async def set_cmd(interaction: discord.Interaction, symbol: str, target: float, dm: bool = False):
    symbol = symbol.upper()
    uid = interaction.user.id
    if uid not in user_targets:
        user_targets[uid] = {}
    user_targets[uid][symbol] = {"target": target, "dm": dm}
    await interaction.response.send_message(
        f"✅ ตั้งเป้าหมาย {symbol} ที่ {target} (ส่งทาง {'DM' if dm else 'ช่องรวม'})",
        ephemeral=True
    )

@tree.command(name="all", description="ดูเป้าหมายหุ้นของคุณ")
async def all_cmd(interaction: discord.Interaction):
    uid = interaction.user.id
    if uid not in user_targets or not user_targets[uid]:
        await interaction.response.send_message("คุณยังไม่ได้ตั้งเป้าหมาย", ephemeral=True)
        return
    msg = "📊 เป้าหมายของคุณ:\n"
    for sym, info in user_targets[uid].items():
        msg += f"- {sym}: {info['target']} ({'DM' if info['dm'] else 'ช่องรวม'})\n"
    await interaction.response.send_message(msg, ephemeral=True)

@tree.command(name="check", description="เช็คราคาหุ้นตอนนี้")
async def check_cmd(interaction: discord.Interaction, symbol: str):
    symbol = symbol.upper()
    price = get_stock_price(symbol)
    if price is None:
        await interaction.response.send_message(f"❌ ไม่สามารถดึงราคาของ {symbol}", ephemeral=True)
        return
    await interaction.response.send_message(f"💹 หุ้น {symbol} ราคาปัจจุบัน {price}", ephemeral=True)

@tree.command(name="remove", description="ลบเป้าหมายหุ้น")
async def remove_cmd(interaction: discord.Interaction, symbol: str):
    uid = interaction.user.id
    symbol = symbol.upper()
    if uid in user_targets and symbol in user_targets[uid]:
        del user_targets[uid][symbol]
        await interaction.response.send_message(f"🗑 ลบเป้าหมาย {symbol} แล้ว", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ คุณไม่ได้ตั้ง {symbol}", ephemeral=True)

# ====== Events ======
@bot.event
async def on_ready():
    await tree.sync()
    print(f"✅ Logged in as {bot.user}")
    check_stocks.start()

# ====== Run ======
if DISCORD_TOKEN:
    bot.run(DISCORD_TOKEN)
else:
    print("❌ กรุณาตั้งค่า DISCORD_TOKEN ใน Secrets/Environment")
