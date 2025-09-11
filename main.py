import discord
from discord import app_commands
from discord.ext import tasks
import yfinance as yf
import asyncio
import json
import logging
import os

# Logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("stockbot")

# Discord token
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "ใส่โทเคนของคุณ")
DATA_FILE = "targets.json"

intents = discord.Intents.default()
intents.message_content = False
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# เก็บเป้าหมายหุ้นของแต่ละผู้ใช้
targets = {}

# โหลด/บันทึกข้อมูล
def load_data():
    global targets
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                targets = json.load(f)
            logger.info("โหลดข้อมูลเป้าหมายเรียบร้อยแล้ว")
        except Exception as e:
            logger.error(f"โหลดข้อมูลไม่สำเร็จ: {e}")
            targets = {}
    else:
        targets = {}

def save_data():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(targets, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logger.error(f"บันทึกข้อมูลไม่สำเร็จ: {e}")

# ================== ฟังก์ชันดึงราคาหุ้น ==================
def get_price(symbol: str):
    try:
        ticker = yf.Ticker(symbol)
        data = ticker.history(period="1d")
        if data.empty:
            return None
        return float(data["Close"].iloc[-1])
    except Exception as e:
        logger.error(f"ดึงราคาหุ้น {symbol} ไม่สำเร็จ: {e}")
        return None

# ================== แนวรับระดับเซียน ==================
def calc_pro_support(symbol: str):
    try:
        ticker = yf.Ticker(symbol)
        data = ticker.history(period="60d")
        if data.empty or len(data) < 20:
            return None

        # ราคาต่ำสุดย้อนหลังหลายช่วง
        low5 = data["Low"][-5:].min()
        low10 = data["Low"][-10:].min()
        low20 = data["Low"][-20:].min()

        # Moving Average
        ma5 = data["Close"][-5:].mean()
        ma10 = data["Close"][-10:].mean()
        ma20 = data["Close"][-20:].mean()
        ma50 = data["Close"][-50:].mean() if len(data) >= 50 else ma20

        # Volatility
        std20 = data["Close"][-20:].std()
        current_price = data["Close"][-1]
        vol_factor = std20 / current_price

        # Trend factor
        trend_up = ma20 > ma50
        trend_factor = 0.01 if trend_up else -0.01

        # Gap / Candle analysis
        last_candle_drop = (data["Close"][-1] - data["Open"][-1]) / data["Open"][-1]
        gap_factor = 0.01 if last_candle_drop < -0.02 else 0

        # Weighted support
        weighted_low = (low5*0.4 + low10*0.3 + low20*0.3)
        support = weighted_low * (1 - vol_factor*0.5 + trend_factor - gap_factor)

        return round(support, 2)
    except Exception as e:
        logger.error(f"คำนวณแนวรับ {symbol} ไม่สำเร็จ: {e}")
        return None

# ================== Slash Commands ==================
@tree.command(name="set", description="ตั้งเป้าหมายราคาหุ้น")
@app_commands.describe(stock="ชื่อหุ้น เช่น AAPL หรือ PTT.BK", target="ราคาเป้าหมาย", dm="ส่งการแจ้งเตือนทาง DM หรือไม่")
async def set_stock(interaction: discord.Interaction, stock: str, target: float, dm: bool = False):
    user_id = str(interaction.user.id)
    if user_id not in targets:
        targets[user_id] = {}
    targets[user_id][stock.upper()] = {"target": target, "dm": dm}
    save_data()
    await interaction.response.send_message(
        f"✅ ตั้งเป้าหมายหุ้น `{stock.upper()}` ที่ราคา {target} บาท เรียบร้อยแล้ว! "
        + ("(ส่งทาง DM)" if dm else "(ส่งในห้องแชท)"),
        ephemeral=True
    )

@tree.command(name="check", description="เช็คราคาหุ้นที่ตั้งเป้าไว้")
async def check_stocks(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if user_id not in targets or not targets[user_id]:
        await interaction.response.send_message("ℹ️ คุณยังไม่ได้ตั้งเป้าหมายหุ้น", ephemeral=True)
        return
    embed = discord.Embed(title="📊 หุ้นที่คุณติดตาม", color=discord.Color.blue())
    for stock, info in targets[user_id].items():
        price = get_price(stock)
        support = calc_pro_support(stock)
        if price:
            msg = f"🎯 เป้าหมาย: {info['target']} | 💰 ปัจจุบัน: {price:.2f}"
            if support:
                msg += f" | 📉 แนวรับ: {support}"
            embed.add_field(name=f"{stock}", value=msg, inline=False)
        else:
            embed.add_field(name=f"{stock}", value="⚠️ ไม่สามารถดึงราคาได้", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="all", description="ดูเป้าหมายหุ้นทั้งหมดของผู้ใช้ทุกคน")
async def all_stocks(interaction: discord.Interaction):
    if not targets:
        await interaction.response.send_message("ℹ️ ยังไม่มีใครตั้งเป้าหมายหุ้นเลย", ephemeral=True)
        return
    embed = discord.Embed(title="📢 เป้าหมายหุ้นทั้งหมด", color=discord.Color.green())
    for user_id, stocks in targets.items():
        user = await bot.fetch_user(int(user_id))
        for stock, info in stocks.items():
            embed.add_field(
                name=f"{stock} (โดย {user.display_name})",
                value=f"🎯 เป้า: {info['target']} | DM: {'✅' if info['dm'] else '❌'}",
                inline=False
            )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ================== ปุ่มลบ/เช็ค/ตั้งเป้าใหม่ ==================
class StockButtons(discord.ui.View):
    def __init__(self, stock, user_id):
        super().__init__(timeout=None)
        self.stock = stock
        self.user_id = user_id

    @discord.ui.button(label="📊 เช็คราคา", style=discord.ButtonStyle.primary)
    async def check_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        price = get_price(self.stock)
        support = calc_pro_support(self.stock)
        msg = f"💰 ราคาปัจจุบันของ `{self.stock}` = {price:.2f}" if price else "⚠️ ไม่สามารถดึงราคาได้"
        if support:
            msg += f" | 📉 แนวรับ ≈ {support}"
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="❌ ลบเป้า", style=discord.ButtonStyle.danger)
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = str(interaction.user.id)
        if user_id in targets and self.stock in targets[user_id]:
            del targets[user_id][self.stock]
            save_data()
            await interaction.response.send_message(f"🗑️ ลบเป้าหมายของ `{self.stock}` เรียบร้อยแล้ว", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ คุณไม่ได้ตั้งเป้าหมายหุ้นนี้", ephemeral=True)

# ================== แจ้งเตือนอัตโนมัติทุก 5 นาที ==================
@tasks.loop(minutes=5)
async def check_loop():
    if not targets:
        return
    logger.info(f"ตรวจสอบเป้าหมาย — users={len(targets)}")
    for user_id, stocks in list(targets.items()):
        user = await bot.fetch_user(int(user_id))
        for stock, info in list(stocks.items()):
            price = get_price(stock)
            support = calc_pro_support(stock)
            if price is None:
                continue
            if price <= info["target"]:
                try:
                    if info["dm"]:
                        msg_channel = await user.create_dm()
                    else:
                        # ถ้าไม่ส่ง DM, ส่งใน channel default (คุณอาจปรับได้)
                        msg_channel = None
                    content = f"📢 <@{user_id}> หุ้น `{stock}` ถึงเป้าหมายแล้ว!\n💰 ราคาปัจจุบัน: {price:.2f}"
                    if support:
                        content += f" | 📉 แนวรับ ≈ {support}"
                    view = StockButtons(stock, user_id)
                    if msg_channel:
                        await msg_channel.send(content, view=view)
                    else:
                        logger.info(f"⚠️ ไม่สามารถส่ง Channel สำหรับผู้ใช้ {user_id}")
                except Exception as e:
                    logger.error(f"แจ้งเตือน {stock} ไม่สำเร็จ: {e}")

# ================== Bot Ready ==================
@bot.event
async def on_ready():
    load_data()
    await tree.sync()
    check_loop.start()
    logger.info("📈 บอทพร้อมใช้งานแล้ว — ตรวจสอบทุก 5 นาที")

# ================== Run Bot ==================
bot.run(DISCORD_TOKEN)
