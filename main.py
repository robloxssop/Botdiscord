import os
import asyncio
import logging
import datetime
import discord
from discord.ext import commands, tasks
from discord import app_commands, ui, Interaction, embeds
import yfinance as yf
import statistics
import concurrent.futures

# --- Setup Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("stockbot")

# --- Environment Variables ---
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = os.environ.get("GUILD_ID")
DEFAULT_CHANNEL_ID = int(os.environ.get("CHANNEL_ID", 0))

# --- Global Data Storage (Consider a database for persistence) ---
# Store trigger_type along with target price
# {user_id: {symbol: {'target': float, 'trigger_type': 'below' | 'above'}}}
user_targets = {}
user_messages = {}
user_dm_preference = {}

# --- Asynchronous Wrappers for Blocking I/O ---
executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)

async def async_fetch_price(symbol: str):
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(executor, fetch_price_blocking, symbol)
    except Exception as e:
        logger.error(f"Error fetching price for {symbol}: {e}")
        return None

def fetch_price_blocking(symbol: str):
    """Blocking function to fetch a stock's current price."""
    try:
        ticker = yf.Ticker(symbol)
        data = ticker.history(period="1d", interval="1m")
        if data.empty:
            return None
        return float(data["Close"].iloc[-1])
    except Exception as e:
        logger.warning(f"ไม่สามารถดึงราคาหุ้น {symbol}: {e}")
        return None

async def async_fetch_support_resistance(symbol: str):
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(executor, fetch_support_resistance_blocking, symbol)
    except Exception as e:
        logger.error(f"Error fetching support/resistance for {symbol}: {e}")
        return None, None

def fetch_support_resistance_blocking(symbol: str):
    """Blocking function to fetch a stock's support and resistance levels."""
    try:
        ticker = yf.Ticker(symbol)
        data = ticker.history(period="6mo", interval="1d")
        if data.empty:
            return None, None
        closes = data["Close"].tolist()
        if not closes:
            return None, None
        mean_price = statistics.mean(closes)
        std_price = statistics.pstdev(closes)
        pivot = (max(closes[-20:]) + min(closes[-20:]) + closes[-1]) / 3
        support = round(pivot - std_price, 2)
        resistance = round(pivot + std_price, 2)
        return support, resistance
    except Exception as e:
        logger.warning(f"ไม่สามารถคำนวณแนวรับแนวต้าน {symbol}: {e}")
        return None, None

# --- Custom Views and Modals ---

class StockView(ui.View):
    def __init__(self, user_id: int, symbol: str, target_data: dict):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.symbol = symbol
        self.target = target_data.get('target')
        self.trigger_type = target_data.get('trigger_type', 'below')

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ คุณไม่สามารถกดปุ่มของคนอื่นได้", ephemeral=True)
            return False
        return True

    @ui.select(placeholder="เลือกวิธีแจ้งเตือน", options=[
        discord.SelectOption(label="ส่ง DM", description="แจ้งเตือนแบบข้อความส่วนตัว", emoji="📩"),
        discord.SelectOption(label="ส่ง Channel", description="แจ้งเตือนใน Channel ปกติ", emoji="📢")
    ])
    async def notification_method(self, interaction: Interaction, select: ui.Select):
        choice = select.values[0]
        user_dm_preference[self.user_id] = "dm" if choice == "ส่ง DM" else "channel"
        await interaction.response.send_message(f"✅ ตั้งค่าแจ้งเตือนเป็น: {choice}", ephemeral=True)

    @ui.button(label="🔄 เช็คราคาใหม่", style=discord.ButtonStyle.primary)
    async def check_price(self, interaction: Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        price = await async_fetch_price(self.symbol)
        if price is None:
            await interaction.followup.send(f"❌ ไม่สามารถดึงราคาของ **{self.symbol}** ได้ กรุณาตรวจสอบชื่อหุ้นอีกครั้ง", ephemeral=True)
            return
        
        status = "📈 สูงกว่าหรือเท่ากับเป้าหมาย" if price >= self.target else "📉 ต่ำกว่าเป้าหมาย"
        support, resistance = await async_fetch_support_resistance(self.symbol)
        
        embed = discord.Embed(
            title=f"หุ้น {self.symbol}",
            color=0x3498db,
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        embed.add_field(name="ราคาปัจจุบัน", value=f"**{price}** บาท", inline=True)
        embed.add_field(name="ราคาเป้าหมาย", value=f"**{self.target}** บาท", inline=True)
        embed.add_field(name="ประเภทการแจ้งเตือน", value=f"{'เมื่อราคาต่ำกว่า/เท่ากับเป้าหมาย' if self.trigger_type == 'below' else 'เมื่อราคาสูงกว่า/เท่ากับเป้าหมาย'}", inline=False)
        if support and resistance:
            embed.add_field(name="แนวรับ/แนวต้าน", value=f"แนวรับ ≈ {support} บาท\nแนวต้าน ≈ {resistance} บาท", inline=False)
        embed.set_footer(text=f"{status} | ข้อมูลจาก yfinance")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @ui.button(label="✏️ แก้ไขเป้าหมาย", style=discord.ButtonStyle.secondary)
    async def edit_target(self, interaction: Interaction, button: ui.Button):
        await interaction.response.send_modal(EditTargetModal(self.user_id, self.symbol))

    @ui.button(label="❌ ลบเป้าหมาย", style=discord.ButtonStyle.danger)
    async def delete_target(self, interaction: Interaction, button: ui.Button):
        if self.user_id in user_targets and self.symbol in user_targets[self.user_id]:
            del user_targets[self.user_id][self.symbol]
            await interaction.response.send_message(f"🗑️ ลบเป้าหมายหุ้น **{self.symbol}** เรียบร้อยแล้ว", ephemeral=True)
        else:
            await interaction.response.send_message("❌ ไม่พบเป้าหมายที่คุณตั้งไว้", ephemeral=True)

    @ui.button(label="📊 แนวรับ/แนวต้าน", style=discord.ButtonStyle.success)
    async def support_resistance(self, interaction: Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        support, resistance = await async_fetch_support_resistance(self.symbol)
        if support is None:
            await interaction.followup.send(f"❌ ไม่สามารถคำนวณแนวรับแนวต้าน **{self.symbol}** ได้ กรุณาตรวจสอบชื่อหุ้นอีกครั้ง", ephemeral=True)
            return
        
        embed = discord.Embed(
            title=f"แนวรับ/แนวต้าน {self.symbol}",
            description=f"แนวรับ ≈ **{support}** บาท\nแนวต้าน ≈ **{resistance}** บาท",
            color=0x1abc9c,
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

class EditTargetModal(ui.Modal, title="แก้ไขเป้าหมายหุ้น"):
    new_target = ui.TextInput(label="ราคาเป้าหมายใหม่", style=discord.TextStyle.short, placeholder="กรุณาใส่ราคาเป้าหมายเป็นตัวเลข")
    new_trigger_type = ui.TextInput(label="ประเภทการแจ้งเตือน (below/above)", style=discord.TextStyle.short, default="below")
    
    def __init__(self, user_id: int, symbol: str):
        super().__init__()
        self.user_id = user_id
        self.symbol = symbol
        
    async def on_submit(self, interaction: Interaction):
        try:
            value = float(self.new_target.value)
        except ValueError:
            await interaction.response.send_message("❌ กรุณากรอกราคาเป็นตัวเลขที่ถูกต้อง", ephemeral=True)
            return
        
        trigger = self.new_trigger_type.value.lower()
        if trigger not in ['below', 'above']:
            await interaction.response.send_message("❌ ประเภทการแจ้งเตือนไม่ถูกต้อง กรุณาใช้ 'below' หรือ 'above'", ephemeral=True)
            return

        if self.user_id not in user_targets:
            user_targets[self.user_id] = {}
        user_targets[self.user_id][self.symbol] = {'target': value, 'trigger_type': trigger}
        
        await interaction.response.send_message(f"✅ ตั้งเป้าหมายใหม่สำหรับ **{self.symbol}** ที่ **{value}** บาท (แจ้งเตือนเมื่อราคา {trigger}) เรียบร้อยแล้ว", ephemeral=True)

# --- Bot Class and Commands ---
class StockBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def on_ready(self):
        try:
            if GUILD_ID:
                guild = discord.Object(id=int(GUILD_ID))
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                logger.info("คำสั่ง Slash ถูกซิงค์แบบเซิร์ฟเวอร์")
            else:
                await self.tree.sync()
                logger.info("คำสั่ง Slash ถูกซิงค์แบบ Global")
        except Exception as e:
            logger.error(f"ซิงค์คำสั่งล้มเหลว: {e}")
        
        self.auto_check.start()
        logger.info(f"บอท {self.user.name} พร้อมใช้งานแล้ว - ตรวจสอบทุกๆ 5 นาที")

    @tasks.loop(minutes=5)
    async def auto_check(self):
        logger.info("เริ่มการตรวจสอบราคาหุ้นอัตโนมัติ...")
        for uid, targets in list(user_targets.items()):
            for stock, data in list(targets.items()):
                target = data.get('target')
                trigger_type = data.get('trigger_type', 'below')
                
                price = await async_fetch_price(stock)
                if price is None:
                    continue
                
                should_notify = False
                if trigger_type == 'below' and price <= target:
                    should_notify = True
                elif trigger_type == 'above' and price >= target:
                    should_notify = True
                
                if should_notify:
                    try:
                        user = await self.fetch_user(uid)
                        if user is None:
                            continue
                        
                        old_msg = user_messages.get((uid, stock))
                        if old_msg:
                            try:
                                await old_msg.delete()
                            except (discord.NotFound, discord.Forbidden):
                                logger.info(f"ไม่สามารถลบข้อความเก่าได้ (อาจถูกลบไปแล้ว): {uid}, {stock}")
                                pass
                        
                        support, resistance = await async_fetch_support_resistance(stock)
                        
                        embed = discord.Embed(
                            title="📢 แจ้งเตือน: ราคาหุ้นถึงเป้าหมายแล้ว!",
                            color=0xe67e22,
                            timestamp=datetime.datetime.now(datetime.timezone.utc)
                        )
                        embed.add_field(name="หุ้น", value=f"**{stock}**", inline=True)
                        embed.add_field(name="ราคาปัจจุบัน", value=f"**{price}** บาท", inline=True)
                        embed.add_field(name="ราคาเป้าหมาย", value=f"**{target}** บาท", inline=True)
                        embed.add_field(name="ประเภทการแจ้งเตือน", value=f"{'เมื่อราคาต่ำกว่า/เท่ากับเป้าหมาย' if trigger_type == 'below' else 'เมื่อราคาสูงกว่า/เท่ากับเป้าหมาย'}", inline=False)
                        if support and resistance:
                            embed.add_field(name="แนวรับ/แนวต้าน", value=f"แนวรับ ≈ {support} บาท\nแนวต้าน ≈ {resistance} บาท", inline=False)
                        
                        view = StockView(uid, stock, data)
                        method = user_dm_preference.get(uid, "dm")
                        
                        sent_message = None
                        if method == "dm":
                            sent_message = await user.send(embed=embed, view=view)
                        else:
                            channel = self.get_channel(DEFAULT_CHANNEL_ID)
                            if channel:
                                sent_message = await channel.send(f"แจ้งเตือนสำหรับ {user.mention}", embed=embed, view=view)
                            else:
                                logger.warning(f"ไม่พบ channel ID: {DEFAULT_CHANNEL_ID}")
                                continue
                        
                        if sent_message:
                            user_messages[(uid, stock)] = sent_message
                            logger.info(f"ส่งแจ้งเตือนสำหรับ {stock} ถึง {user.name} แล้ว")

                    except Exception as e:
                        logger.error(f"เกิดข้อผิดพลาดในการส่งแจ้งเตือนสำหรับ {stock} ถึง {uid}: {e}")

# --- Slash Command Group ---
stock_group = app_commands.Group(name="stock", description="คำสั่งสำหรับจัดการข้อมูลหุ้น")

@stock_group.command(name="set", description="ตั้งเป้าหมายราคาหุ้น")
@app_commands.describe(stock="ชื่อหุ้น เช่น AAPL หรือ PTT.BK", target="ราคาเป้าหมาย", trigger_type="ประเภทการแจ้งเตือน ('below' หรือ 'above', ค่าเริ่มต้นคือ below)")
@app_commands.choices(trigger_type=[
    app_commands.Choice(name="ต่ำกว่าหรือเท่ากับเป้าหมาย", value="below"),
    app_commands.Choice(name="สูงกว่าหรือเท่ากับเป้าหมาย", value="above")
])
async def set_target_cmd(interaction: Interaction, stock: str, target: float, trigger_type: str = 'below'):
    uid = interaction.user.id
    stock = stock.upper()
    
    price = await async_fetch_price(stock)
    if price is None:
        await interaction.response.send_message(f"❌ ไม่พบหุ้นชื่อ **{stock}** หรือข้อมูลไม่ถูกต้อง กรุณาตรวจสอบชื่อหุ้นอีกครั้ง", ephemeral=True)
        return

    if uid not in user_targets:
        user_targets[uid] = {}
    
    user_targets[uid][stock] = {'target': target, 'trigger_type': trigger_type}
    
    embed = discord.Embed(
        title="✅ ตั้งเป้าหมายสำเร็จ",
        description=f"{interaction.user.mention} ตั้งเป้าหมายหุ้น **{stock}**",
        color=0x2ecc71,
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )
    embed.add_field(name="ราคาเป้าหมาย", value=f"**{target}** บาท", inline=True)
    embed.add_field(name="ประเภทการแจ้งเตือน", value=f"{'เมื่อราคาต่ำกว่า/เท่ากับเป้าหมาย' if trigger_type == 'below' else 'เมื่อราคาสูงกว่า/เท่ากับเป้าหมาย'}", inline=True)

    view = StockView(uid, stock, user_targets[uid][stock])
    await interaction.response.send_message(embed=embed, view=view)

@stock_group.command(name="check", description="เช็คราคาหุ้นปัจจุบัน")
@app_commands.describe(stock="ชื่อหุ้น เช่น AAPL หรือ PTT.BK")
async def check_stock_cmd(interaction: Interaction, stock: str):
    await interaction.response.defer(ephemeral=True)
    stock = stock.upper()
    price = await async_fetch_price(stock)
    
    if price is None:
        await interaction.followup.send(f"❌ ไม่สามารถดึงราคาหุ้น **{stock}** ได้ กรุณาตรวจสอบชื่อหุ้นอีกครั้ง", ephemeral=True)
        return
    
    uid = interaction.user.id
    target_data = user_targets.get(uid, {}).get(stock)
    
    embed = discord.Embed(
        title=f"ข้อมูลหุ้น {stock}",
        color=0x3498db,
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )
    embed.add_field(name="ราคาปัจจุบัน", value=f"**{price}** บาท", inline=True)
    
    if target_data:
        target = target_data['target']
        trigger_type = target_data['trigger_type']
        
        status = "📈 สูงกว่าหรือเท่ากับเป้าหมาย" if price >= target else "📉 ต่ำกว่าเป้าหมาย"
        support, resistance = await async_fetch_support_resistance(stock)
        
        embed.add_field(name="ราคาเป้าหมาย", value=f"**{target}** บาท", inline=True)
        embed.add_field(name="ประเภทการแจ้งเตือน", value=f"{'เมื่อราคาต่ำกว่า/เท่ากับเป้าหมาย' if trigger_type == 'below' else 'เมื่อราคาสูงกว่า/เท่ากับเป้าหมาย'}", inline=False)
        
        if support and resistance:
            embed.add_field(name="แนวรับ/แนวต้าน", value=f"แนวรับ ≈ {support} บาท\nแนวต้าน ≈ {resistance} บาท", inline=False)
        embed.set_footer(text=f"{status} | ข้อมูลจาก yfinance")
        view = StockView(uid, stock, target_data)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
    else:
        embed.description = f"**ราคา: {price} บาท**\n(คุณยังไม่ได้ตั้งเป้าหมายสำหรับหุ้นนี้)"
        embed.color = 0x95a5a6
        await interaction.followup.send(embed=embed, ephemeral=True)

@stock_group.command(name="list", description="ดูเป้าหมายที่คุณตั้งไว้ทั้งหมด")
async def show_targets_cmd(interaction: Interaction):
    uid = interaction.user.id
    targets = user_targets.get(uid, {})
    if not targets:
        await interaction.response.send_message("❌ คุณยังไม่ได้ตั้งเป้าหมายหุ้นใด ๆ", ephemeral=True)
        return
        
    embed = discord.Embed(
        title="📊 เป้าหมายหุ้นของคุณ",
        description="นี่คือรายการเป้าหมายหุ้นที่คุณตั้งไว้:",
        color=0x3498db
    )
    for s, data in targets.items():
        trigger_text = 'ต่ำกว่าหรือเท่ากับ' if data['trigger_type'] == 'below' else 'สูงกว่าหรือเท่ากับ'
        embed.add_field(name=f"หุ้น {s}", value=f"ราคาเป้าหมาย: **{data['target']}** บาท\nแจ้งเตือนเมื่อราคา {trigger_text} เป้าหมาย", inline=False)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@stock_group.command(name="delete", description="ลบเป้าหมายหุ้น")
@app_commands.describe(stock="ชื่อหุ้นที่จะลบ")
async def delete_target_cmd(interaction: Interaction, stock: str):
    uid = interaction.user.id
    stock = stock.upper()
    
    if uid in user_targets and stock in user_targets[uid]:
        del user_targets[uid][stock]
        await interaction.response.send_message(f"🗑️ ลบเป้าหมายหุ้น **{stock}** เรียบร้อยแล้ว", ephemeral=True)
    else:
        await interaction.response.send_message("❌ ไม่พบเป้าหมายที่คุณตั้งไว้สำหรับหุ้นนี้", ephemeral=True)

@stock_group.command(name="support", description="ดูแนวรับหุ้น")
@app_commands.describe(stock="ชื่อหุ้นที่จะดูแนวรับ")
async def support_cmd(interaction: Interaction, stock: str):
    await interaction.response.defer(ephemeral=True)
    stock = stock.upper()
    support, _ = await async_fetch_support_resistance(stock)
    
    if support is None:
        await interaction.followup.send(f"❌ ไม่สามารถคำนวณแนวรับ **{stock}** ได้ กรุณาตรวจสอบชื่อหุ้นอีกครั้ง", ephemeral=True)
        return
        
    await interaction.followup.send(f"📉 แนวรับ **{stock}** ≈ **{support}** บาท", ephemeral=True)

@stock_group.command(name="resistance", description="ดูแนวต้านหุ้น")
@app_commands.describe(stock="ชื่อหุ้นที่จะดูแนวต้าน")
async def resistance_cmd(interaction: Interaction, stock: str):
    await interaction.response.defer(ephemeral=True)
    stock = stock.upper()
    _, resistance = await async_fetch_support_resistance(stock)
    
    if resistance is None:
        await interaction.followup.send(f"❌ ไม่สามารถคำนวณแนวต้าน **{stock}** ได้ กรุณาตรวจสอบชื่อหุ้นอีกครั้ง", ephemeral=True)
        return
        
    await interaction.followup.send(f"📈 แนวต้าน **{stock}** ≈ **{resistance}** บาท", ephemeral=True)


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("❌ กรุณาตั้งค่า DISCORD_TOKEN ใน Secrets/Environment Variables")
    else:
        bot = StockBot()
        bot.tree.add_command(stock_group)
        bot.run(DISCORD_TOKEN)
