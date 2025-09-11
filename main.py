import discord
from discord.ext import tasks
from discord import app_commands, ui
import yfinance as yf
import json, os, logging
from datetime import datetime

# =================== Logging ===================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("StockBot")

# =================== Token ===================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "YOUR_DISCORD_TOKEN")

# =================== Client ===================
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# =================== Data ===================
DATA_FILE = "targets.json"
targets = {}  # {user_id: {stock: {"target": float, "dm": bool, "last_msg": None}}}

def load_data():
    global targets
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                targets = json.load(f)
        except Exception as e:
            logger.error(f"Load data error: {e}")
            targets = {}
    else:
        targets = {}

def save_data():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(targets, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Save data error: {e}")

# =================== Stock Price ===================
def get_price(symbol: str):
    try:
        ticker = yf.Ticker(symbol)
        data = ticker.history(period="1d")
        if data.empty: return None
        return float(data["Close"].iloc[-1])
    except Exception as e:
        logger.error(f"Get price error ({symbol}): {e}")
        return None

# =================== Support / Resistance ===================
def calc_support(symbol: str):
    try:
        ticker = yf.Ticker(symbol)
        data = ticker.history(period="60d")
        if data.empty or len(data)<20: return None
        low5 = data["Low"][-5:].min()
        low10 = data["Low"][-10:].min()
        low20 = data["Low"][-20:].min()
        ma20 = data["Close"][-20:].mean()
        ma50 = data["Close"][-50:].mean() if len(data)>=50 else ma20
        std20 = data["Close"][-20:].std()
        current = data["Close"][-1]
        vol_factor = std20 / current
        trend = 0.01 if ma20>ma50 else -0.01
        last_drop = (data["Close"][-1]-data["Open"][-1])/data["Open"][-1]
        gap = 0.01 if last_drop<-0.02 else 0
        weighted_low = (low5*0.4 + low10*0.3 + low20*0.3)
        support = weighted_low * (1 - vol_factor*0.5 + trend - gap)
        return round(support,2)
    except Exception as e:
        logger.error(f"Calc support error ({symbol}): {e}")
        return None

def calc_resistance(symbol: str):
    try:
        ticker = yf.Ticker(symbol)
        data = ticker.history(period="60d")
        if data.empty or len(data)<20: return None
        high5 = data["High"][-5:].max()
        high10 = data["High"][-10:].max()
        high20 = data["High"][-20:].max()
        ma20 = data["Close"][-20:].mean()
        ma50 = data["Close"][-50:].mean() if len(data)>=50 else ma20
        std20 = data["Close"][-20:].std()
        current = data["Close"][-1]
        vol_factor = std20 / current
        trend = 0.01 if ma20>ma50 else -0.01
        last_gain = (data["Close"][-1]-data["Open"][-1])/data["Open"][-1]
        gap = 0.01 if last_gain>0.02 else 0
        weighted_high = (high5*0.4 + high10*0.3 + high20*0.3)
        resistance = weighted_high * (1 + vol_factor*0.5 + trend + gap)
        return round(resistance,2)
    except Exception as e:
        logger.error(f"Calc resistance error ({symbol}): {e}")
        return None

# =================== Buttons ===================
class StockButtons(discord.ui.View):
    def __init__(self, stock, user_id):
        super().__init__(timeout=None)
        self.stock = stock
        self.user_id = user_id

    @discord.ui.button(label="📊 Check Price", style=discord.ButtonStyle.primary)
    async def check_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        price = get_price(self.stock)
        support = calc_support(self.stock)
        resistance = calc_resistance(self.stock)
        msg = f"💰 Current Price `{self.stock}` = {price:.2f}" if price else "⚠️ Price unavailable"
        if support: msg += f" | 📉 Support ≈ {support}"
        if resistance: msg += f" | 📈 Resistance ≈ {resistance}"
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="❌ Delete Target", style=discord.ButtonStyle.danger)
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = str(interaction.user.id)
        if user_id in targets and self.stock in targets[user_id]:
            del targets[user_id][self.stock]
            save_data()
            await interaction.response.send_message(f"🗑️ Deleted target `{self.stock}` successfully", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ You have no target for this stock", ephemeral=True)

# =================== Slash Commands ===================
@tree.command(name="set", description="Set target stock price")
@app_commands.describe(stock="Stock symbol e.g., AAPL or PTT.BK", target="Target price", dm="Send DM or channel")
async def set_stock(interaction: discord.Interaction, stock: str, target: float, dm: bool=False):
    user_id = str(interaction.user.id)
    if user_id not in targets: targets[user_id] = {}
    targets[user_id][stock.upper()] = {"target": target, "dm": dm, "last_msg": None}
    save_data()
    await interaction.response.send_message(
        f"✅ Target for `{stock.upper()}` set at {target} {'(DM)' if dm else '(Channel)'}", ephemeral=True
    )

@tree.command(name="check", description="Check your stock targets")
async def check_stocks(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if user_id not in targets or not targets[user_id]:
        await interaction.response.send_message("ℹ️ You have no stock targets", ephemeral=True)
        return
    embed = discord.Embed(title="📊 Your Stock Targets", color=discord.Color.blue())
    for stock, info in targets[user_id].items():
        price = get_price(stock)
        support = calc_support(stock)
        resistance = calc_resistance(stock)
        msg = f"🎯 Target: {info['target']} | 💰 Current: {price:.2f}" if price else "⚠️ Price unavailable"
        if support: msg += f" | 📉 Support: {support}"
        if resistance: msg += f" | 📈 Resistance: {resistance}"
        embed.add_field(name=stock, value=msg, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="all", description="See all stock targets")
async def all_stocks(interaction: discord.Interaction):
    if not targets:
        await interaction.response.send_message("ℹ️ No targets set yet", ephemeral=True)
        return
    embed = discord.Embed(title="📢 All Stock Targets", color=discord.Color.green())
    for user_id, stocks in targets.items():
        try:
            user = await bot.fetch_user(int(user_id))
        except: user = None
        for stock, info in stocks.items():
            uname = user.display_name if user else user_id
            embed.add_field(name=f"{stock} (by {uname})",
                            value=f"🎯 Target: {info['target']} | DM: {'✅' if info['dm'] else '❌'}",
                            inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# =================== Auto Alert ===================
@tasks.loop(minutes=5)
async def check_loop():
    for user_id, stocks in list(targets.items()):
        try:
            user = await bot.fetch_user(int(user_id))
        except: continue
        for stock, info in list(stocks.items()):
            price = get_price(stock)
            support = calc_support(stock)
            resistance = calc_resistance(stock)
            if price and price <= info["target"]:
                try:
                    content = f"📢 <@{user_id}> `{stock}` reached target!\n💰 Current: {price:.2f}"
                    if support: content += f" | 📉 Support ≈ {support}"
                    if resistance: content += f" | 📈 Resistance ≈ {resistance}"
                    view = StockButtons(stock, user_id)
                    last_msg_id = info.get("last_msg")
                    if last_msg_id:
                        try:
                            msg = await user.fetch_message(int(last_msg_id))
                            await msg.delete()
                        except: pass
                    if info["dm"]:
                        channel = await user.create_dm()
                        msg = await channel.send(content, view=view)
                    else:
                        continue
                    targets[user_id][stock]["last_msg"] = str(msg.id)
                    save_data()
                except Exception as e:
                    logger.error(f"Alert failed for {stock}: {e}")

# =================== Bot Ready ===================
@bot.event
async def on_ready():
    load_data()
    await tree.sync()
    check_loop.start()
    logger.info("📈 Bot ready — checking every 5 minutes")

bot.run(DISCORD_TOKEN)
