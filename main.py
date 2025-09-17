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
import requests
import numpy as np

# --- Setup Logging ---
logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("stockbot")

# --- Environment Variables ---
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = os.environ.get("GUILD_ID")
DEFAULT_CHANNEL_ID = int(os.environ.get("CHANNEL_ID", 0))
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")

# --- Global Data Storage (Consider a database for persistence) ---
user_targets = {}
user_messages = {}
# For demonstration, a mock user role system. In a real app, this would be from a database.
user_roles = {
    # Replace with a real user ID from your guild to test VIP features
    # Example: '123456789012345678': 'VIP1'
}

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
    """Calculates multiple technical levels for a given stock symbol."""
    data = fetch_historical_data_blocking(symbol, period="6mo", interval="1d")
    if data is None or data.empty:
        return None

    try:
        last_day = data.iloc[-1]
        
        # --- Pivot Point (Classic) ---
        p_point = (last_day['High'] + last_day['Low'] + last_day['Close']) / 3
        s1_pivot = (2 * p_point) - last_day['High']
        r1_pivot = (2 * p_point) - last_day['Low']
        
        # --- Fibonacci Retracement ---
        high_fib = data['High'].max()
        low_fib = data['Low'].min()
        fib_range = high_fib - low_fib
        fib_levels = {
            's1': high_fib - 0.382 * fib_range,
            's2': high_fib - 0.5 * fib_range,
            's3': high_fib - 0.618 * fib_range,
            'r1': low_fib + 0.382 * fib_range,
            'r2': low_fib + 0.5 * fib_range,
            'r3': low_fib + 0.618 * fib_range
        }
        
        # --- Average True Range (ATR) ---
        data['tr'] = np.maximum.reduce([
            data['High'] - data['Low'],
            np.abs(data['High'] - data['Close'].shift()),
            np.abs(data['Low'] - data['Close'].shift())
        ])
        atr = data['tr'].rolling(window=14).mean().iloc[-1]
        atr_s1 = last_day['Close'] - 1 * atr
        atr_r1 = last_day['Close'] + 1 * atr
        atr_s2 = last_day['Close'] - 2 * atr
        atr_r2 = last_day['Close'] + 2 * atr

        # --- Volume Profile (POC and Value Area) ---
        def calculate_volume_profile(df, num_bins=50):
            df = df.dropna(subset=['Close', 'Volume'])
            prices = df['Close'].values
            volumes = df['Volume'].values
            if not len(prices) or not len(volumes):
                return None, None
            
            hist, bin_edges = np.histogram(prices, bins=num_bins, weights=volumes)
            poc_index = np.argmax(hist)
            poc_price = (bin_edges[poc_index] + bin_edges[poc_index+1]) / 2
            
            sorted_indices = np.argsort(hist)[::-1]
            cumulative_volume = 0
            value_area_indices = []
            
            for i in sorted_indices:
                cumulative_volume += hist[i]
                value_area_indices.append(i)
                if cumulative_volume / np.sum(hist) >= 0.70:
                    break
            
            va_low = min(bin_edges[i] for i in value_area_indices)
            va_high = max(bin_edges[i+1] for i in value_area_indices)
            
            return round(poc_price, 2), (round(va_low, 2), round(va_high, 2))

        poc, va_range = calculate_volume_profile(data.iloc[-120:])
        
        return {
            "pivot_s1": round(s1_pivot, 2),
            "pivot_r1": round(r1_pivot, 2),
            "fib_s1": round(fib_levels['s1'], 2),
            "fib_r1": round(fib_levels['r1'], 2),
            "atr_s1": round(atr_s1, 2),
            "atr_r1": round(atr_r1, 2),
            "poc": poc,
            "va_low": va_range[0] if va_range else None,
            "va_high": va_range[1] if va_range else None
        }
    except Exception as e:
        logger.warning(f"ไม่สามารถคำนวณแนวรับแนวต้าน {symbol}: {e}")
        return None

async def async_fetch_news(symbol: str):
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(executor, fetch_news_blocking, symbol)
    except Exception as e:
        logger.error(f"Error fetching news for {symbol}: {e}")
        return None

def fetch_news_blocking(symbol: str):
    """Blocking function to fetch a stock's latest news."""
    if not FINNHUB_API_KEY:
        logger.error("FINNHUB_API_KEY is not set.")
        return None
    to_date = datetime.date.today()
    from_date = to_date - datetime.timedelta(days=7)
    url = f"https://finnhub.io/api/v1/company-news?symbol={symbol}&from={from_date}&to={to_date}&token={FINNHUB_API_KEY}"
    try:
        response = requests.get(url)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as err:
        if err.response.status_code == 429:
            logger.warning("Finnhub API rate limit exceeded.")
        elif err.response.status_code == 401:
            logger.error("Invalid Finnhub API key.")
        else:
            logger.error(f"HTTP Error for news fetching: {err}")
        return None
    except Exception as e:
        logger.error(f"An error occurred while fetching news for {symbol}: {e}")
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
            embed.add_field(name="แนวรับ", value=f"**Pivot:** {levels.get('pivot_s1', 'N/A')} บาท\n**ATR:** {levels.get('atr_s1', 'N/A')} บาท\n**Fibonacci:** {levels.get('fib_s1', 'N/A')} บาท\n**Value Area:** {levels.get('va_low', 'N/A')} บาท", inline=True)
            embed.add_field(name="แนวต้าน", value=f"**Pivot:** {levels.get('pivot_r1', 'N/A')} บาท\n**ATR:** {levels.get('atr_r1', 'N/A')} บาท\n**Fibonacci:** {levels.get('fib_r1', 'N/A')} บาท\n**Value Area:** {levels.get('va_high', 'N/A')} บาท", inline=True)
            embed.add_field(name="Point of Control (POC)", value=f"**{levels.get('poc', 'N/A')}** บาท", inline=False)
        
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
        embed.add_field(name="แนวรับ 📉", value=f"**Pivot:** {levels.get('pivot_s1', 'N/A')} บาท\n**ATR:** {levels.get('atr_s1', 'N/A')} บาท\n**Fibonacci:** {levels.get('fib_s1', 'N/A')} บาท\n**Value Area:** {levels.get('va_low', 'N/A')} บาท", inline=True)
        embed.add_field(name="แนวต้าน 📈", value=f"**Pivot:** {levels.get('pivot_r1', 'N/A')} บาท\n**ATR:** {levels.get('atr_r1', 'N/A')} บาท\n**Fibonacci:** {levels.get('fib_r1', 'N/A')} บาท\n**Value Area:** {levels.get('va_high', 'N/A')} บาท", inline=True)
        embed.add_field(name="Point of Control (POC)", value=f"**{levels.get('poc', 'N/A')}** บาท", inline=False)
        
        embed.set_footer(text="คำนวณจากข้อมูลย้อนหลัง 6 เดือน")
        await interaction.followup.send(embed=embed, ephemeral=True)

class EditTargetModal(ui.Modal, title="แก้ไขเป้าหมายหุ้น"):
    new_target = ui.TextInput(label="ราคาเป้าหมายใหม่", style=discord.TextStyle.short, placeholder="กรุณาใส่ราคาเป้าหมายเป็นตัวเลข")
    new_trigger_type = ui.TextInput(label="เงื่อนไข (ราคาต่ำกว่า/ราคาสูงกว่า)", style=discord.TextStyle.short, default="ต่ำกว่า")

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
        
        trigger_map = {'ต่ำกว่า': 'below', 'ราคาสูงกว่า': 'above'}
        trigger = trigger_map.get(self.new_trigger_type.value.lower().replace('ราคา', ''), None)
        
        if not trigger:
            await interaction.response.send_message("❌ เงื่อนไขไม่ถูกต้อง กรุณาใช้ 'ราคาต่ำกว่า' หรือ 'ราคาสูงกว่า'", ephemeral=True)
            return
        
        if self.user_id not in user_targets:
            user_targets[self.user_id] = {}
        
        user_targets[self.user_id][self.symbol] = {
            'target': value,
            'trigger_type': trigger,
            'alert_threshold_percent': user_targets[self.user_id].get(self.symbol, {}).get('alert_threshold_percent', 5.0)
        }
        
        await interaction.response.send_message(f"✅ ตั้งเป้าหมายใหม่สำหรับ **{self.symbol}** ที่ **{value}** บาท (แจ้งเตือนเมื่อราคา{self.new_trigger_type.value}) เรียบร้อยแล้ว", ephemeral=True)

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
        logger.info(f"บอท {self.user.name} พร้อมใช้งานแล้ว - กำลังทำงาน")

    @tasks.loop(seconds=60)
    async def auto_check(self):
        now = datetime.datetime.now()
        current_minute = now.minute

        for uid, targets in list(user_targets.items()):
            user_role = user_roles.get(str(uid), 'regular')
            
            # Check for VIP1 users every minute
            if user_role == 'VIP1':
                await self.run_user_check(uid, targets)
                logger.info(f"Checking VIP user {uid} at {now.strftime('%H:%M:%S')}")
            # Check for regular users every 5 minutes
            elif current_minute % 5 == 0:
                await self.run_user_check(uid, targets)
                logger.info(f"Checking regular user {uid} at {now.strftime('%H:%M:%S')}")

    async def run_user_check(self, uid, targets):
        for stock, data in list(targets.items()):
            target = data.get('target')
            trigger_type = data.get('trigger_type', 'below')
            alert_threshold_percent = data.get('alert_threshold_percent', 5.0)
            
            price = await async_fetch_price(stock)
            if price is None:
                continue
            
            # --- Check for approaching target ---
            should_notify_approaching = False
            if trigger_type == 'below':
                if target < price <= target * (1 + alert_threshold_percent / 100):
                    should_notify_approaching = True
            elif trigger_type == 'above':
                if target > price >= target * (1 - alert_threshold_percent / 100):
                    should_notify_approaching = True
            
            if should_notify_approaching:
                try:
                    user = await self.fetch_user(uid)
                    if user is None:
                        continue
                        
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
                        embed.add_field(name="แนวรับ", value=f"**Pivot:** {levels.get('pivot_s1', 'N/A')} บาท\n**ATR:** {levels.get('atr_s1', 'N/A')} บาท\n**Fibonacci:** {levels.get('fib_s1', 'N/A')} บาท\n**Value Area:** {levels.get('va_low', 'N/A')} บาท", inline=True)
                        embed.add_field(name="แนวต้าน", value=f"**Pivot:** {levels.get('pivot_r1', 'N/A')} บาท\n**ATR:** {levels.get('atr_r1', 'N/A')} บาท\n**Fibonacci:** {levels.get('fib_r1', 'N/A')} บาท\n**Value Area:** {levels.get('va_high', 'N/A')} บาท", inline=True)
                        embed.add_field(name="Point of Control (POC)", value=f"**{levels.get('poc', 'N/A')}** บาท", inline=False)

                    view = StockView(uid, stock, data, is_approaching=True)
                    await user.send(embed=embed, view=view)
                        
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
                    if user is None:
                        continue
                        
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
                        embed.add_field(name="แนวรับ", value=f"**Pivot:** {levels.get('pivot_s1', 'N/A')} บาท\n**ATR:** {levels.get('atr_s1', 'N/A')} บาท\n**Fibonacci:** {levels.get('fib_s1', 'N/A')} บาท\n**Value Area:** {levels.get('va_low', 'N/A')} บาท", inline=True)
                        embed.add_field(name="แนวต้าน", value=f"**Pivot:** {levels.get('pivot_r1', 'N/A')} บาท\n**ATR:** {levels.get('atr_r1', 'N/A')} บาท\n**Fibonacci:** {levels.get('fib_r1', 'N/A')} บาท\n**Value Area:** {levels.get('va_high', 'N/A')} บาท", inline=True)
                        embed.add_field(name="Point of Control (POC)", value=f"**{levels.get('poc', 'N/A')}** บาท", inline=False)

                    view = StockView(uid, stock, data)
                    await user.send(embed=embed, view=view)
                except Exception as e:
                    logger.error(f"เกิดข้อผิดพลาดในการส่งแจ้งเตือนสำหรับ {stock} ถึง {uid}: {e}")

# --- Slash Command Group ---
stock_group = app_commands.Group(name="หุ้น", description="คำสั่งสำหรับจัดการข้อมูลหุ้น")

@stock_group.command(name="ตั้ง", description="ตั้งเป้าหมายราคาหุ้น")
@app_commands.describe(
    หุ้น="ชื่อหุ้น เช่น AAPL หรือ PTT.BK",
    ราคาเป้าหมาย="ราคาที่ต้องการให้บอทแจ้งเตือน",
    เงื่อนไข="เลือกว่าจะให้แจ้งเตือนเมื่อราคาต่ำกว่าหรือสูงกว่าเป้าหมาย (ค่าเริ่มต้น: ต่ำกว่า)",
    แจ้งเตือนล่วงหน้า="เปอร์เซ็นต์ที่ต้องการให้บอทแจ้งเตือนเมื่อราคาเข้าใกล้เป้าหมาย (เช่น 5 หมายถึง 5%)"
)
@app_commands.choices(
    เงื่อนไข=[
        app_commands.Choice(name="ราคาต่ำกว่า", value="below"),
        app_commands.Choice(name="ราคาสูงกว่า", value="above")
    ]
)
async def set_target_cmd(interaction: Interaction, หุ้น: str, ราคาเป้าหมาย: float, เงื่อนไข: str = 'below', แจ้งเตือนล่วงหน้า: float = 5.0):
    uid = interaction.user.id
    stock = หุ้น.upper()
    
    if แจ้งเตือนล่วงหน้า < 0 or แจ้งเตือนล่วงหน้า > 100:
        await interaction.response.send_message("❌ เปอร์เซ็นต์การแจ้งเตือนต้องอยู่ระหว่าง 0 ถึง 100", ephemeral=True)
        return
        
    price = await async_fetch_price(stock)
    if price is None:
        await interaction.response.send_message(f"❌ ไม่พบหุ้นชื่อ **{stock}** หรือข้อมูลไม่ถูกต้อง กรุณาตรวจสอบชื่อหุ้นอีกครั้ง", ephemeral=True)
        return

    if uid not in user_targets:
        user_targets[uid] = {}
        
    user_targets[uid][stock] = {
        'target': ราคาเป้าหมาย,
        'trigger_type': เงื่อนไข,
        'alert_threshold_percent': แจ้งเตือนล่วงหน้า
    }

    trigger_text_map = {'below': 'ราคาต่ำกว่าหรือเท่ากับเป้าหมาย', 'above': 'ราคาสูงกว่าหรือเท่ากับเป้าหมาย'}
    embed = discord.Embed(
        title="✅ ตั้งเป้าหมายสำเร็จ",
        description=f"{interaction.user.mention} ตั้งเป้าหมายหุ้น **{stock}**",
        color=0x2ecc71,
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )
    embed.add_field(name="ราคาเป้าหมาย", value=f"**{ราคาเป้าหมาย}** บาท", inline=True)
    embed.add_field(name="เงื่อนไข", value=trigger_text_map[เงื่อนไข], inline=True)
    embed.add_field(name="แจ้งเตือนล่วงหน้า", value=f"**{แจ้งเตือนล่วงหน้า}%**", inline=False)
    embed.add_field(name="ช่องทางแจ้งเตือน", value=f"**ข้อความส่วนตัว (DM)**", inline=False)

    view = StockView(uid, stock, user_targets[uid][stock])
    await interaction.response.send_message(embed=embed, view=view)

@stock_group.command(name="ราคา", description="เช็คราคาหุ้นปัจจุบัน")
@app_commands.describe(หุ้น="ชื่อหุ้น เช่น AAPL หรือ PTT.BK")
async def check_stock_cmd(interaction: Interaction, หุ้น: str):
    await interaction.response.defer(ephemeral=True)
    stock = หุ้น.upper()
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
            embed.add_field(name="แนวรับ", value=f"**Pivot:** {levels.get('pivot_s1', 'N/A')} บาท\n**ATR:** {levels.get('atr_s1', 'N/A')} บาท\n**Fibonacci:** {levels.get('fib_s1', 'N/A')} บาท\n**Value Area:** {levels.get('va_low', 'N/A')} บาท", inline=True)
            embed.add_field(name="แนวต้าน", value=f"**Pivot:** {levels.get('pivot_r1', 'N/A')} บาท\n**ATR:** {levels.get('atr_r1', 'N/A')} บาท\n**Fibonacci:** {levels.get('fib_r1', 'N/A')} บาท\n**Value Area:** {levels.get('va_high', 'N/A')} บาท", inline=True)
            embed.add_field(name="Point of Control (POC)", value=f"**{levels.get('poc', 'N/A')}** บาท", inline=False)
            
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
        embed.add_field(
            name=f"หุ้น {s}",
            value=f"ราคาเป้าหมาย: **{data['target']}** บาท\nเงื่อนไข: **{trigger_text}** เป้าหมาย\nแจ้งเตือนล่วงหน้า: **{data['alert_threshold_percent']}%**\nช่องทาง: **ข้อความส่วนตัว (DM)**",
            inline=False
        )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@stock_group.command(name="ลบ", description="ลบเป้าหมายหุ้น")
@app_commands.describe(หุ้น="ชื่อหุ้นที่จะลบ")
async def delete_target_cmd(interaction: Interaction, หุ้น: str):
    uid = interaction.user.id
    stock = หุ้น.upper()
    
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
@app_commands.describe(หุ้น="ชื่อหุ้นที่จะดูข้อมูล")
async def levels_cmd(interaction: Interaction, หุ้น: str):
    await interaction.response.defer(ephemeral=True)
    stock = หุ้น.upper()
    levels = await async_fetch_technical_levels(stock)
    
    if levels is None:
        await interaction.followup.send(f"❌ ไม่สามารถคำนวณแนวรับ/แนวต้าน **{stock}** ได้ กรุณาตรวจสอบชื่อหุ้นอีกครั้ง", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"แนวรับและแนวต้าน **{stock}** (หลายมุมมอง)",
        color=0x1abc9c,
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )
    embed.add_field(name="แนวรับ 📉", value=f"**Pivot:** {levels.get('pivot_s1', 'N/A')} บาท\n**ATR:** {levels.get('atr_s1', 'N/A')} บาท\n**Fibonacci:** {levels.get('fib_s1', 'N/A')} บาท\n**Value Area:** {levels.get('va_low', 'N/A')} บาท", inline=True)
    embed.add_field(name="แนวต้าน 📈", value=f"**Pivot:** {levels.get('pivot_r1', 'N/A')} บาท\n**ATR:** {levels.get('atr_r1', 'N/A')} บาท\n**Fibonacci:** {levels.get('fib_r1', 'N/A')} บาท\n**Value Area:** {levels.get('va_high', 'N/A')} บาท", inline=True)
    embed.add_field(name="Point of Control (POC)", value=f"**{levels.get('poc', 'N/A')}** บาท", inline=False)
    
    embed.set_footer(text="คำนวณจากข้อมูลย้อนหลัง 6 เดือน")
    await interaction.followup.send(embed=embed, ephemeral=True)

@stock_group.command(name="ข่าว", description="ดูข่าวล่าสุดของหุ้น")
@app_commands.describe(หุ้น="ชื่อหุ้นที่ต้องการดูข่าว")
async def news_cmd(interaction: Interaction, หุ้น: str):
    await interaction.response.defer(ephemeral=True)
    stock = หุ้น.upper()
    
    if not FINNHUB_API_KEY:
        await interaction.followup.send("❌ บอทยังไม่ได้ตั้งค่า Finnhub API Key กรุณาแจ้งผู้ดูแล", ephemeral=True)
        return

    news_data = await async_fetch_news(stock)
    
    if news_data is None:
        await interaction.followup.send(f"❌ ไม่สามารถดึงข่าวของหุ้น **{stock}** ได้ อาจเป็นเพราะชื่อหุ้นไม่ถูกต้องหรือโควต้า API หมด", ephemeral=True)
        return
        
    if not news_data:
        await interaction.followup.send(f"⚠️ ไม่พบข่าวล่าสุดสำหรับหุ้น **{stock}** ในช่วงสัปดาห์ที่ผ่านมา", ephemeral=True)
        return
    
    # สร้าง Embed สำหรับแสดงข่าว
    embed = discord.Embed(
        title=f"📰 ข่าวล่าสุดสำหรับ {stock}",
        description="นี่คือข่าวที่เกี่ยวข้องกับหุ้นนี้ในรอบ 7 วันที่ผ่านมา:",
        color=0x1abc9c,
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )
    
    # แสดงข่าว 5 อันดับแรก
    for article in news_data[:5]:
        embed.add_field(
            name=f"[{article.get('headline')}]({article.get('url')})",
            value=f"_{article.get('source')}_ - {article.get('summary')}\n",
            inline=False
        )
        
    embed.set_footer(text="ข้อมูลจาก Finnhub")
    await interaction.followup.send(embed=embed, ephemeral=True)

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("❌ กรุณาตั้งค่า DISCORD_TOKEN ใน Secrets/Environment Variables")
    else:
        bot = StockBot()
        bot.tree.add_command(stock_group)
        bot.run(DISCORD_TOKEN)
