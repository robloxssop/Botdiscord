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
logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("stockbot")

# --- Environment Variables ---
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = os.environ.get("GUILD_ID")
DEFAULT_CHANNEL_ID = int(os.environ.get("CHANNEL_ID", 0))

# --- Global Data Storage (Consider a database for persistence) ---
user_targets = {}
user_messages = {}

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

def fetch_historical_data_blocking(symbol: str, period="6mo", interval="1d"):
    try:
        ticker = yf.Ticker(symbol)
        data = ticker.history(period=period, interval=interval)
        return data
    except Exception as e:
        logger.warning(f"ไม่สามารถดึงข้อมูลในอดีตของ {symbol}: {e}")
        return None

async def async_fetch_technical_levels(symbol: str):
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(executor, calculate_technical_levels, symbol)
    except Exception as e:
        logger.error(f"Error calculating technical levels for {symbol}: {e}")
        return None

def calculate_technical_levels(symbol: str):
    data = fetch_historical_data_blocking(symbol, period="3mo", interval="1d")
    if data is None or data.empty:
        return None

    try:
        last_day = data.iloc[-1]
        p_point = (last_day['High'] + last_day['Low'] + last_day['Close']) / 3
        s1_pivot = (2 * p_point) - last_day['High']
        r1_pivot = (2 * p_point) - last_day['Low']
        
        recent_high = data['High'].iloc[-20:].max()
        recent_low = data['Low'].iloc[-20:].min()
        diff = recent_high - recent_low
        
        s1_fib = recent_high - 0.382 * diff
        s2_fib = recent_high - 0.618 * diff
        r1_fib = recent_low + 0.382 * diff
        r2_fib = recent_low + 0.618 * diff

        closes = data['Close'].tolist()
        if not closes:
            return None
        mean_price = statistics.mean(closes)
        std_price = statistics.pstdev(closes)
        
        s_std = round(mean_price - 1.5 * std_price, 2)
        r_std = round(mean_price + 1.5 * std_price, 2)
        
        return {
            "pivot_s1": round(s1_pivot, 2),
            "pivot_r1": round(r1_pivot, 2),
            "fib_s1": round(s1_fib, 2),
            "fib_s2": round(s2_fib, 2),
            "fib_r1": round(r1_fib, 2),
            "fib_r2": round(r2_fib, 2),
            "std_s": s_std,
            "std_r": r_std
        }
    except Exception as e:
        logger.warning(f"ไม่สามารถคำนวณแนวรับแนวต้าน {symbol}: {e}")
        return None

# --- Custom Views and Modals ---

class StockView(ui.View):
    def __init__(self, user_id: int, symbol: str, target_data: dict, is_approaching: bool = False):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.symbol = symbol
        self.target = target_data.get('target')
        self.trigger_type = target_data.get('trigger_type', 'below')
        self.is_approaching = is_approaching

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ คุณไม่สามารถกดปุ่มของคนอื่นได้", ephemeral=True)
            return False
        return True

    @ui.button(label="🔄 เช็คราคาใหม่", style=discord.ButtonStyle.primary)
    async def check_price(self, interaction: Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        price = await async_fetch_price(self.symbol)
        if price is None:
            await interaction.followup.send(f"❌ ไม่สามารถดึงราคาของ **{self.symbol}** ได้ กรุณาตรวจสอบชื่อหุ้นอีกครั้ง", ephemeral=True)
            return
        
        status = "📈 สูงกว่าหรือเท่ากับเป้าหมาย" if price >= self.target else "📉 ต่ำกว่าเป้าหมาย"
        levels = await async_fetch_technical_levels(self.symbol)
        
        embed = discord.Embed(
            title=f"หุ้น {self.symbol}",
            color=0x3498db,
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        embed.add_field(name="ราคาปัจจุบัน", value=f"**{price}** บาท", inline=True)
        embed.add_field(name="ราคาเป้าหมาย", value=f"**{self.target}** บาท", inline=True)
        embed.add_field(name="ประเภทการแจ้งเตือน", value=f"{'เมื่อราคาต่ำกว่า/เท่ากับเป้าหมาย' if self.trigger_type == 'below' else 'เมื่อราคาสูงกว่า/เท่ากับเป้าหมาย'}", inline=False)
        
        if levels:
            support_levels = f"**Pivot:** {levels['pivot_s1']} บาท\n**Fibonacci:** {levels['fib_s1']} / {levels['fib_s2']} บาท\n**ค่าเฉลี่ย:** {levels['std_s']} บาท"
            resistance_levels = f"**Pivot:** {levels['pivot_r1']} บาท\n**Fibonacci:** {levels['fib_r1']} / {levels['fib_r2']} บาท\n**ค่าเฉลี่ย:** {levels['std_r']} บาท"
            embed.add_field(name="แนวรับ", value=support_levels, inline=False)
            embed.add_field(name="แนวต้าน", value=resistance_levels, inline=False)
        
        embed.set_footer(text=f"{status} | ข้อมูลจาก yfinance")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @ui.button(label="✏️ แก้ไขเป้าหมาย", style=discord.ButtonStyle.secondary)
    async def edit_target(self, interaction: Interaction, button: ui.Button):
        await interaction.response.send_modal(EditTargetModal(self.user_id, self.symbol))

    @ui.button(label="❌ ลบเป้าหมาย", style=discord.ButtonStyle.danger)
    async def delete_target(self, interaction: Interaction, button: ui.Button):
        if self.user_id in user_targets and self.symbol in user_targets[self.user_id]:
            if (self.user_id, self.symbol) in user_messages:
                try:
                    old_msg = user_messages[(self.user_id, self.symbol)]
                    await old_msg.delete()
                    del user_messages[(self.user_id, self.symbol)]
                except discord.NotFound:
                    pass
                except Exception as e:
                    logger.error(f"Error deleting old message for {self.symbol}: {e}")

            del user_targets[self.user_id][self.symbol]
            await interaction.response.send_message(f"🗑️ ลบเป้าหมายหุ้น **{self.symbol}** เรียบร้อยแล้ว", ephemeral=True)
        else:
            await interaction.response.send_message("❌ ไม่พบเป้าหมายที่คุณตั้งไว้", ephemeral=True)

    @ui.button(label="📊 แนวรับ/แนวต้าน", style=discord.ButtonStyle.success)
    async def support_resistance(self, interaction: Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        levels = await async_fetch_technical_levels(self.symbol)
        
        if levels is None:
            await interaction.followup.send(f"❌ ไม่สามารถคำนวณแนวรับแนวต้าน **{self.symbol}** ได้ กรุณาตรวจสอบชื่อหุ้นอีกครั้ง", ephemeral=True)
            return
        
        embed = discord.Embed(
            title=f"แนวรับ/แนวต้าน {self.symbol}",
            color=0x1abc9c,
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        support_levels = f"**Pivot:** {levels['pivot_s1']} บาท\n**Fibonacci:** {levels['fib_s1']} / {levels['fib_s2']} บาท\n**ค่าเฉลี่ย:** {levels['std_s']} บาท"
        resistance_levels = f"**Pivot:** {levels['pivot_r1']} บาท\n**Fibonacci:** {levels['fib_r1']} / {levels['fib_r2']} บาท\n**ค่าเฉลี่ย:** {levels['std_r']} บาท"
        embed.add_field(name="แนวรับ", value=support_levels, inline=False)
        embed.add_field(name="แนวต้าน", value=resistance_levels, inline=False)
        
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
        
        user_targets[self.user_id][self.symbol] = {
            'target': value, 
            'trigger_type': trigger,
            'alert_threshold_percent': user_targets[self.user_id].get(self.symbol, {}).get('alert_threshold_percent', 5.0),
            'approaching_alert_sent': False
        }
        
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
        for uid, targets in list(user_targets.items()):
            for stock, data in list(targets.items()):
                target = data.get('target')
                trigger_type = data.get('trigger_type', 'below')
                alert_threshold_percent = data.get('alert_threshold_percent', 5.0)
                approaching_alert_sent = data.get('approaching_alert_sent', False)
                
                price = await async_fetch_price(stock)
                if price is None:
                    continue
                
                # --- Check for approaching target ---
                should_notify_approaching = False
                if not approaching_alert_sent:
                    if trigger_type == 'below':
                        if target < price <= target * (1 + alert_threshold_percent / 100):
                            should_notify_approaching = True
                    elif trigger_type == 'above':
                        if target > price >= target * (1 - alert_threshold_percent / 100):
                            should_notify_approaching = True

                if should_notify_approaching:
                    try:
                        user = await self.fetch_user(uid)
                        if user is None: continue
                        
                        levels = await async_fetch_technical_levels(stock)
                        
                        embed = discord.Embed(
                            title="🔔 ราคาหุ้นใกล้ถึงเป้าหมายแล้ว!",
                            description=f"หุ้น **{stock}** กำลังเคลื่อนเข้าใกล้ราคาเป้าหมายของคุณ",
                            color=0xf39c12,
                            timestamp=datetime.datetime.now(datetime.timezone.utc)
                        )
                        embed.add_field(name="ราคาปัจจุบัน", value=f"**{price}** บาท", inline=True)
                        embed.add_field(name="ราคาเป้าหมาย", value=f"**{target}** บาท", inline=True)
                        embed.add_field(name="ประเภทการแจ้งเตือน", value=f"{'เมื่อราคาต่ำกว่า/เท่ากับ' if trigger_type == 'below' else 'เมื่อราคาสูงกว่า/เท่ากับ'}", inline=False)
                        
                        if levels:
                            embed.add_field(name="แนวรับ", value=f"**Pivot:** {levels['pivot_s1']} บาท\n**Fibonacci:** {levels['fib_s1']} / {levels['fib_s2']} บาท", inline=True)
                            embed.add_field(name="แนวต้าน", value=f"**Pivot:** {levels['pivot_r1']} บาท\n**Fibonacci:** {levels['fib_r1']} / {levels['fib_r2']} บาท", inline=True)
                        
                        view = StockView(uid, stock, data, is_approaching=True)
                        
                        sent_message = await user.send(embed=embed, view=view)

                        if sent_message:
                            user_targets[uid][stock]['approaching_alert_sent'] = True
                            user_messages[(uid, stock)] = sent_message

                    except Exception as e:
                        logger.error(f"เกิดข้อผิดพลาดในการส่งแจ้งเตือนราคาใกล้เป้าสำหรับ {stock} ถึง {uid}: {e}")

                # --- Check for target reached ---
                should_notify = False
                if trigger_type == 'below' and price <= target:
                    should_notify = True
                elif trigger_type == 'above' and price >= target:
                    should_notify = True
                
                if should_notify:
                    try:
                        user = await self.fetch_user(uid)
                        if user is None: continue
                        
                        levels = await async_fetch_technical_levels(stock)
                        
                        embed = discord.Embed(
                            title="📢 แจ้งเตือน: ราคาหุ้นถึงเป้าหมายแล้ว!",
                            color=0xe67e22,
                            timestamp=datetime.datetime.now(datetime.timezone.utc)
                        )
                        embed.add_field(name="หุ้น", value=f"**{stock}**", inline=True)
                        embed.add_field(name="ราคาปัจจุบัน", value=f"**{price}** บาท", inline=True)
                        embed.add_field(name="ราคาเป้าหมาย", value=f"**{target}** บาท", inline=True)
                        embed.add_field(name="ประเภทการแจ้งเตือน", value=f"{'เมื่อราคาต่ำกว่า/เท่ากับเป้าหมาย' if trigger_type == 'below' else 'เมื่อราคาสูงกว่า/เท่ากับเป้าหมาย'}", inline=False)
                        if levels:
                            support_levels = f"**Pivot:** {levels['pivot_s1']} บาท\n**Fibonacci:** {levels['fib_s1']} / {levels['fib_s2']} บาท\n**ค่าเฉลี่ย:** {levels['std_s']} บาท"
                            resistance_levels = f"**Pivot:** {levels['pivot_r1']} บาท\n**Fibonacci:** {levels['fib_r1']} / {levels['fib_r2']} บาท\n**ค่าเฉลี่ย:** {levels['std_r']} บาท"
                            embed.add_field(name="แนวรับ", value=support_levels, inline=False)
                            embed.add_field(name="แนวต้าน", value=resistance_levels, inline=False)
                        
                        view = StockView(uid, stock, data)
                        
                        sent_message = await user.send(embed=embed, view=view)
                        
                        if sent_message:
                            if uid in user_targets and stock in user_targets[uid]:
                                del user_targets[uid][stock]
                            
                            if (uid, stock) in user_messages:
                                del user_messages[(uid, stock)]

                    except Exception as e:
                        logger.error(f"เกิดข้อผิดพลาดในการส่งแจ้งเตือนสำหรับ {stock} ถึง {uid}: {e}")

# --- Slash Command Group ---
stock_group = app_commands.Group(name="หุ้น", description="คำสั่งสำหรับจัดการข้อมูลหุ้น")

@stock_group.command(name="ตั้ง", description="ตั้งเป้าหมายราคาหุ้น")
@app_commands.describe(
    stock="ชื่อหุ้น เช่น AAPL หรือ PTT.BK",
    target="ราคาเป้าหมาย",
    trigger_type="ประเภทการแจ้งเตือน ('below' หรือ 'above', ค่าเริ่มต้นคือ below)",
    alert_threshold_percent="เปอร์เซ็นต์ที่ต้องการให้แจ้งเตือนเมื่อราคาเข้าใกล้เป้าหมาย (ค่าเริ่มต้น 5%)"
)
@app_commands.choices(
    trigger_type=[
        app_commands.Choice(name="ต่ำกว่าหรือเท่ากับเป้าหมาย", value="below"),
        app_commands.Choice(name="สูงกว่าหรือเท่ากับเป้าหมาย", value="above")
    ]
)
async def set_target_cmd(interaction: Interaction, stock: str, target: float, trigger_type: str = 'below', alert_threshold_percent: float = 5.0):
    uid = interaction.user.id
    stock = stock.upper()
    
    if alert_threshold_percent < 0 or alert_threshold_percent > 100:
        await interaction.response.send_message("❌ เปอร์เซ็นต์การแจ้งเตือนต้องอยู่ระหว่าง 0 ถึง 100", ephemeral=True)
        return
        
    price = await async_fetch_price(stock)
    if price is None:
        await interaction.response.send_message(f"❌ ไม่พบหุ้นชื่อ **{stock}** หรือข้อมูลไม่ถูกต้อง กรุณาตรวจสอบชื่อหุ้นอีกครั้ง", ephemeral=True)
        return

    if uid not in user_targets:
        user_targets[uid] = {}

    user_targets[uid][stock] = {
        'target': target, 
        'trigger_type': trigger_type,
        'alert_threshold_percent': alert_threshold_percent,
        'approaching_alert_sent': False
    }
    
    embed = discord.Embed(
        title="✅ ตั้งเป้าหมายสำเร็จ",
        description=f"{interaction.user.mention} ตั้งเป้าหมายหุ้น **{stock}**",
        color=0x2ecc71,
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )
    embed.add_field(name="ราคาเป้าหมาย", value=f"**{target}** บาท", inline=True)
    embed.add_field(name="ประเภทการแจ้งเตือน", value=f"{'เมื่อราคาต่ำกว่า/เท่ากับเป้าหมาย' if trigger_type == 'below' else 'เมื่อราคาสูงกว่า/เท่ากับเป้าหมาย'}", inline=True)
    embed.add_field(name="แจ้งเตือนใกล้เป้าหมาย", value=f"**{alert_threshold_percent}%**", inline=False)
    embed.add_field(name="ช่องทางแจ้งเตือน", value=f"**ข้อความส่วนตัว (DM)**", inline=False)

    view = StockView(uid, stock, user_targets[uid][stock])
    await interaction.response.send_message(embed=embed, view=view)

@stock_group.command(name="ราคา", description="เช็คราคาหุ้นปัจจุบัน")
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
        levels = await async_fetch_technical_levels(stock)
        
        embed.add_field(name="ราคาเป้าหมาย", value=f"**{target}** บาท", inline=True)
        embed.add_field(name="ประเภทการแจ้งเตือน", value=f"{'เมื่อราคาต่ำกว่า/เท่ากับเป้าหมาย' if trigger_type == 'below' else 'เมื่อราคาสูงกว่า/เท่ากับเป้าหมาย'}", inline=False)
        
        if levels:
            support_levels = f"**Pivot:** {levels['pivot_s1']} บาท\n**Fibonacci:** {levels['fib_s1']} / {levels['fib_s2']} บาท\n**ค่าเฉลี่ย:** {levels['std_s']} บาท"
            resistance_levels = f"**Pivot:** {levels['pivot_r1']} บาท\n**Fibonacci:** {levels['fib_r1']} / {levels['fib_r2']} บาท\n**ค่าเฉลี่ย:** {levels['std_r']} บาท"
            embed.add_field(name="แนวรับ", value=support_levels, inline=False)
            embed.add_field(name="แนวต้าน", value=resistance_levels, inline=False)
        embed.set_footer(text=f"{status} | ข้อมูลจาก yfinance")
        view = StockView(uid, stock, target_data)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
    else:
        embed.description = f"**ราคา: {price} บาท**\n(คุณยังไม่ได้ตั้งเป้าหมายสำหรับหุ้นนี้)"
        embed.color = 0x95a5a6
        await interaction.followup.send(embed=embed, ephemeral=True)

@stock_group.command(name="รายการ", description="ดูเป้าหมายที่คุณตั้งไว้ทั้งหมด")
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
        embed.add_field(name=f"หุ้น {s}", value=f"ราคาเป้าหมาย: **{data['target']}** บาท\nแจ้งเตือนเมื่อราคา {trigger_text} เป้าหมาย\nแจ้งเตือนใกล้เป้า: **{data['alert_threshold_percent']}%**\nช่องทาง: **ข้อความส่วนตัว (DM)**", inline=False)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@stock_group.command(name="ลบ", description="ลบเป้าหมายหุ้น")
@app_commands.describe(stock="ชื่อหุ้นที่จะลบ")
async def delete_target_cmd(interaction: Interaction, stock: str):
    uid = interaction.user.id
    stock = stock.upper()
    
    if uid in user_targets and stock in user_targets[uid]:
        if (uid, stock) in user_messages:
            try:
                old_msg = user_messages[(uid, stock)]
                await old_msg.delete()
                del user_messages[(uid, stock)]
            except discord.NotFound:
                pass
            except Exception as e:
                logger.error(f"เกิดข้อผิดพลาดในการลบข้อความแจ้งเตือนเก่าสำหรับ {stock}: {e}")
        
        del user_targets[uid][stock]
        await interaction.response.send_message(f"🗑️ ลบเป้าหมายหุ้น **{stock}** เรียบร้อยแล้ว", ephemeral=True)
    else:
        await interaction.response.send_message("❌ ไม่พบเป้าหมายที่คุณตั้งไว้สำหรับหุ้นนี้", ephemeral=True)

@stock_group.command(name="แนวรับแนวต้าน", description="ดูแนวรับและแนวต้านของหุ้น (หลายมุมมอง)")
@app_commands.describe(stock="ชื่อหุ้นที่จะดูข้อมูล")
async def levels_cmd(interaction: Interaction, stock: str):
    await interaction.response.defer(ephemeral=True)
    stock = stock.upper()
    levels = await async_fetch_technical_levels(stock)
    
    if levels is None:
        await interaction.followup.send(f"❌ ไม่สามารถคำนวณแนวรับ/แนวต้าน **{stock}** ได้ กรุณาตรวจสอบชื่อหุ้นอีกครั้ง", ephemeral=True)
        return
    
    embed = discord.Embed(
        title=f"แนวรับและแนวต้าน **{stock}** (หลายมุมมอง)",
        color=0x1abc9c,
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )
    
    support_levels = f"**Pivot:** {levels['pivot_s1']} บาท\n**Fibonacci:** {levels['fib_s1']} / {levels['fib_s2']} บาท\n**ค่าเฉลี่ย:** {levels['std_s']} บาท"
    resistance_levels = f"**Pivot:** {levels['pivot_r1']} บาท\n**Fibonacci:** {levels['fib_r1']} / {levels['fib_r2']} บาท\n**ค่าเฉลี่ย:** {levels['std_r']} บาท"
    
    embed.add_field(name="แนวรับ 📉", value=support_levels, inline=False)
    embed.add_field(name="แนวต้าน 📈", value=resistance_levels, inline=False)
    
    embed.set_footer(text="คำนวณจากข้อมูลย้อนหลัง 3 เดือน")
    
    await interaction.followup.send(embed=embed, ephemeral=True)

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("❌ กรุณาตั้งค่า DISCORD_TOKEN ใน Secrets/Environment Variables")
    else:
        bot = StockBot()
        bot.tree.add_command(stock_group)
        bot.run(DISCORD_TOKEN)

