import os
import asyncio
import logging
import datetime
import discord
from discord.ext import commands, tasks
from discord import app_commands, ui, Interaction
import yfinance as yf
import statistics

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
logger = logging.getLogger("stockbot")

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = os.environ.get("GUILD_ID")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

user_targets = {}
user_messages = {}

def fetch_price(symbol: str):
    try:
        ticker = yf.Ticker(symbol)
        data = ticker.history(period="1d", interval="1m")
        if data.empty:
            return None
        return float(data["Close"].iloc[-1])
    except Exception as e:
        logger.warning(f"ไม่สามารถดึงราคาหุ้น {symbol}: {e}")
        return None

def fetch_support_resistance(symbol: str):
    try:
        ticker = yf.Ticker(symbol)
        data = ticker.history(period="6mo", interval="1d")
        if data.empty:
            return None, None
        closes = data["Close"].tolist()
        avg = statistics.mean(closes)
        std = statistics.pstdev(closes)
        support = round(avg - std, 2)
        resistance = round(avg + std, 2)
        return support, resistance
    except Exception as e:
        logger.warning(f"ไม่สามารถคำนวณแนวรับแนวต้าน {symbol}: {e}")
        return None, None

class StockView(ui.View):
    def __init__(self, user_id: int, symbol: str, target: float):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.symbol = symbol
        self.target = target

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ คุณไม่สามารถกดปุ่มของคนอื่นได้", ephemeral=True)
            return False
        return True

    @ui.button(label="🔄 เช็คราคาใหม่", style=discord.ButtonStyle.primary)
    async def check_price(self, interaction: Interaction, button: ui.Button):
        price = fetch_price(self.symbol)
        if price is None:
            await interaction.response.send_message(f"❌ ไม่สามารถดึงราคาของ {self.symbol} ได้", ephemeral=True)
            return
        msg = f"📈 หุ้น {self.symbol} ราคาปัจจุบัน: {price} บาท\n🎯 เป้าหมายที่ตั้งไว้: {self.target} บาท"
        await interaction.response.send_message(msg, ephemeral=True)

    @ui.button(label="✏️ แก้ไขเป้าหมาย", style=discord.ButtonStyle.secondary)
    async def edit_target(self, interaction: Interaction, button: ui.Button):
        await interaction.response.send_modal(EditTargetModal(self.user_id, self.symbol))

    @ui.button(label="❌ ลบเป้าหมาย", style=discord.ButtonStyle.danger)
    async def delete_target(self, interaction: Interaction, button: ui.Button):
        if self.user_id in user_targets and self.symbol in user_targets[self.user_id]:
            del user_targets[self.user_id][self.symbol]
            await interaction.response.send_message(f"🗑️ ลบเป้าหมายหุ้น {self.symbol} เรียบร้อยแล้ว", ephemeral=True)
        else:
            await interaction.response.send_message("❌ ไม่พบเป้าหมายที่คุณตั้งไว้", ephemeral=True)

    @ui.button(label="📊 แนวรับ/แนวต้าน", style=discord.ButtonStyle.success)
    async def support_resistance(self, interaction: Interaction, button: ui.Button):
        support, resistance = fetch_support_resistance(self.symbol)
        if support is None:
            await interaction.response.send_message(f"❌ ไม่สามารถคำนวณแนวรับแนวต้าน {self.symbol} ได้", ephemeral=True)
            return
        msg = f"📊 หุ้น {self.symbol}\nแนวรับ ≈ {support} บาท\nแนวต้าน ≈ {resistance} บาท"
        await interaction.response.send_message(msg, ephemeral=True)

class EditTargetModal(ui.Modal, title="แก้ไขเป้าหมายหุ้น"):
    new_target = ui.TextInput(label="ราคาเป้าหมายใหม่", style=discord.TextStyle.short)

    def __init__(self, user_id: int, symbol: str):
        super().__init__()
        self.user_id = user_id
        self.symbol = symbol

    async def on_submit(self, interaction: Interaction):
        try:
            value = float(self.new_target.value)
        except ValueError:
            await interaction.response.send_message("❌ กรุณากรอกราคาเป็นตัวเลข", ephemeral=True)
            return
        if self.user_id not in user_targets:
            user_targets[self.user_id] = {}
        user_targets[self.user_id][self.symbol] = value
        await interaction.response.send_message(f"✅ ตั้งเป้าหมายใหม่สำหรับ {self.symbol} ที่ {value} บาทเรียบร้อยแล้ว", ephemeral=True)

@tree.command(name="set", description="ตั้งเป้าหมายราคาหุ้น")
@app_commands.describe(stock="หุ้น เช่น AAPL หรือ PTT.BK", target="ราคาเป้าหมาย")
async def set_target(interaction: Interaction, stock: str, target: float):
    uid = interaction.user.id
    stock = stock.upper()
    if uid not in user_targets:
        user_targets[uid] = {}
    user_targets[uid][stock] = target
    embed = discord.Embed(title="✅ ตั้งเป้าหมายสำเร็จ", description=f"{interaction.user.mention} ตั้งเป้าหมายหุ้น **{stock}** ที่ {target} บาท", color=0x2ecc71)
    view = StockView(uid, stock, target)
    await interaction.response.send_message(embed=embed, view=view)

@tree.command(name="check", description="เช็คราคาหุ้นปัจจุบัน")
@app_commands.describe(stock="หุ้น เช่น AAPL หรือ PTT.BK")
async def check_stock(interaction: Interaction, stock: str):
    stock = stock.upper()
    price = fetch_price(stock)
    if price is None:
        await interaction.response.send_message(f"❌ ไม่สามารถดึงราคาหุ้น {stock} ได้", ephemeral=True)
        return
    uid = interaction.user.id
    target = user_targets.get(uid, {}).get(stock)
    if target:
        status = "📉 ต่ำกว่าเป้าหมาย" if price < target else "📈 สูงกว่าหรือเท่ากับเป้าหมาย"
        embed = discord.Embed(title=f"หุ้น {stock}", description=f"ราคา: {price} บาท\nเป้าหมาย: {target} บาท\n{status}", color=0x3498db)
        view = StockView(uid, stock, target)
        await interaction.response.send_message(embed=embed, view=view)
    else:
        embed = discord.Embed(title=f"หุ้น {stock}", description=f"ราคา: {price} บาท\n(ยังไม่ได้ตั้งเป้าหมาย)", color=0x95a5a6)
        await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="targets", description="ดูเป้าหมายที่คุณตั้งไว้ทั้งหมด")
async def show_targets(interaction: Interaction):
    uid = interaction.user.id
    targets = user_targets.get(uid, {})
    if not targets:
        await interaction.response.send_message("❌ คุณยังไม่ได้ตั้งเป้าหมายหุ้นใด ๆ", ephemeral=True)
        return
    msg = "📊 เป้าหมายหุ้นของคุณ:\n"
    for s, t in targets.items():
        msg += f"- {s}: {t} บาท\n"
    await interaction.response.send_message(msg, ephemeral=True)

@tasks.loop(minutes=5)
async def auto_check():
    for uid, targets in list(user_targets.items()):
        for stock, target in list(targets.items()):
            price = fetch_price(stock)
            if price is None:
                continue
            if price <= target:
                try:
                    user = await bot.fetch_user(uid)
                    if user is None:
                        continue
                    old_msg = user_messages.get((uid, stock))
                    if old_msg:
                        try:
                            await old_msg.delete()
                        except:
                            pass
                    embed = discord.Embed(title="📢 แจ้งเตือนหุ้น", description=f"{user.mention} หุ้น {stock}\nราคา: {price} บาท\nเป้าหมาย: {target} บาท", color=0xe67e22)
                    view = StockView(uid, stock, target)
                    sent = await user.send(embed=embed, view=view)
                    user_messages[(uid, stock)] = sent
                except Exception as e:
                    logger.warning(f"แจ้งเตือน {stock} ให้ {uid} ไม่ได้: {e}")

@bot.event
async def on_ready():
    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            await tree.sync(guild=guild)
            logger.info("คำสั่ง Slash ถูกซิงค์แบบระบุเซิร์ฟเวอร์")
        else:
            await tree.sync()
            logger.info("คำสั่ง Slash ถูกซิงค์แบบ Global")
    except Exception as e:
        logger.error(f"ซิงค์คำสั่งล้มเหลว: {e}")
    auto_check.start()
    logger.info("บอทพร้อมใช้งานแล้ว — ตรวจสอบทุกๆ 5 นาที")

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("❌ กรุณาตั้งค่า DISCORD_TOKEN ใน Secrets/Environment Variables")
    else:
        bot.run(DISCORD_TOKEN)
