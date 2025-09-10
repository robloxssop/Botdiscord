import os
import discord
from discord.ext import tasks
from discord import app_commands
import requests
import json

# =====================
# ENV (ใส่ใน Secrets)
# =====================
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "0"))

# =====================
# Bot setup
# =====================
intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# user_targets: user_id -> {symbol: {"target": float, "dm": bool}}
user_targets: dict[str, dict] = {}
# last_alerts: user_id -> {symbol: message_obj}
last_alerts: dict[str, dict[str, discord.Message]] = {}

DATA_FILE = "targets.json"

# =====================
# Load / Save data
# =====================
def load_data():
    global user_targets
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            user_targets = json.load(f)

def save_data():
    with open(DATA_FILE, "w") as f:
        json.dump(user_targets, f)

# =====================
# Finnhub API
# =====================
def get_stock_price(symbol: str) -> float | None:
    base_url = "https://finnhub.io/api/v1/quote"
    try:
        r = requests.get(f"{base_url}?symbol={symbol}&token={FINNHUB_API_KEY}", timeout=10)
        data = r.json()
        if data.get("c"):
            return data["c"]
    except Exception:
        pass

    # ถ้าไม่เจอ → ลองต่อท้าย .BK (หุ้นไทย)
    try:
        r = requests.get(f"{base_url}?symbol={symbol}.BK&token={FINNHUB_API_KEY}", timeout=10)
        data = r.json()
        if data.get("c"):
            return data["c"]
    except Exception:
        pass

    return None

# =====================
# View with buttons
# =====================
class StockAlertView(discord.ui.View):
    def __init__(self, user_id: int, symbol: str):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.symbol = symbol

    @discord.ui.button(label="🗑 ลบเป้าหมายนี้", style=discord.ButtonStyle.danger)
    async def remove_target(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ คุณไม่มีสิทธิ์กดปุ่มนี้", ephemeral=True)
            return

        uid_str = str(self.user_id)
        if uid_str in user_targets and self.symbol in user_targets[uid_str]:
            del user_targets[uid_str][self.symbol]
            save_data()
            await interaction.response.edit_message(content="🗑 เป้าหมายนี้ถูกลบแล้ว", view=None, embed=None)
        else:
            await interaction.response.send_message("เป้าหมายนี้ไม่มีอยู่แล้ว", ephemeral=True)

    @discord.ui.button(label="🔄 เช็คราคาอีกครั้ง", style=discord.ButtonStyle.primary)
    async def recheck_price(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ คุณไม่มีสิทธิ์กดปุ่มนี้", ephemeral=True)
            return

        price = get_stock_price(self.symbol)
        if price is None:
            await interaction.response.send_message(f"❌ ไม่สามารถดึงราคาของ {self.symbol}", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"📊 {self.symbol} (อัปเดตใหม่)",
            description=f"ราคาปัจจุบัน: **{price}**",
            color=discord.Color.green()
        )
        await interaction.response.edit_message(embed=embed, view=self)

# =====================
# Background task
# =====================
@tasks.loop(minutes=3)
async def check_stocks():
    for uid_str, targets in user_targets.items():
        user_id = int(uid_str)
        user = await bot.fetch_user(user_id)

        for sym, info in targets.items():
            price = get_stock_price(sym)
            if price is None:
                continue

            target = info["target"]
            use_dm = info["dm"]

            if price <= target:
                embed = discord.Embed(
                    title=f"⚠️ หุ้น {sym}",
                    description=f"ราคาปัจจุบัน {price} ≤ เป้าหมาย {target}",
                    color=discord.Color.red()
                )

                # ลบข้อความเก่า
                if uid_str in last_alerts and sym in last_alerts[uid_str]:
                    try:
                        await last_alerts[uid_str][sym].delete()
                    except Exception:
                        pass

                # ส่งข้อความใหม่
                if use_dm:
                    msg = await user.send(embed=embed, view=StockAlertView(user_id, sym))
                else:
                    channel = bot.get_channel(CHANNEL_ID)
                    if channel and isinstance(channel, discord.TextChannel):
                        msg = await channel.send(
                            content=f"{user.mention}",
                            embed=embed,
                            view=StockAlertView(user_id, sym)
                        )
                    else:
                        continue

                if uid_str not in last_alerts:
                    last_alerts[uid_str] = {}
                last_alerts[uid_str][sym] = msg

# =====================
# Slash Commands
# =====================
@tree.command(name="set", description="ตั้งเป้าหมายราคาหุ้น")
async def set_cmd(interaction: discord.Interaction, symbol: str, target: float, dm: bool = False):
    uid = str(interaction.user.id)
    symbol = symbol.upper()
    if uid not in user_targets:
        user_targets[uid] = {}
    user_targets[uid][symbol] = {"target": target, "dm": dm}
    save_data()
    await interaction.response.send_message(
        f"✅ ตั้ง {symbol} ที่ {target} (ส่งทาง {'DM' if dm else 'ช่องรวม'})",
        ephemeral=True
    )

@tree.command(name="all", description="ดูเป้าหมายหุ้นของคุณ")
async def all_cmd(interaction: discord.Interaction):
    uid = str(interaction.user.id)
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
    await interaction.response.send_message(f"💹 {symbol} ตอนนี้ {price}", ephemeral=True)

@tree.command(name="remove", description="ลบเป้าหมายหุ้น")
async def remove_cmd(interaction: discord.Interaction, symbol: str):
    uid = str(interaction.user.id)
    symbol = symbol.upper()
    if uid in user_targets and symbol in user_targets[uid]:
        del user_targets[uid][symbol]
        save_data()
        await interaction.response.send_message(f"🗑 ลบ {symbol} แล้ว", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ คุณไม่ได้ตั้ง {symbol}", ephemeral=True)

# =====================
# Events
# =====================
@bot.event
async def on_ready():
    load_data()
    await tree.sync()
    print(f"✅ Logged in as {bot.user}")
    check_stocks.start()

# =====================
# Run bot
# =====================
if DISCORD_TOKEN:
    bot.run(DISCORD_TOKEN)
else:
    print("❌ กรุณาตั้งค่า DISCORD_TOKEN")
