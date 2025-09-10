import os
import json
import asyncio
import discord
from discord.ext import tasks, commands
import requests
from datetime import datetime, timedelta
import logging
from typing import Optional, Dict, List
import aiohttp

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "0"))
DATA_FILE = os.environ.get("DATA_FILE", "stock_data.json")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Data structures
user_targets: Dict[int, Dict[str, Dict]] = {}
user_settings: Dict[int, Dict] = {}
last_alerts: Dict[int, Dict[str, discord.Message]] = {}
stock_cache: Dict[str, Dict] = {}
alert_history: List[Dict] = []

def load_data():
    """โหลดข้อมูลจากไฟล์"""
    global user_targets, user_settings, alert_history
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                user_targets = {int(k): v for k, v in data.get('targets', {}).items()}
                user_settings = {int(k): v for k, v in data.get('settings', {}).items()}
                alert_history = data.get('history', [])
                logger.info(f"📁 โหลดข้อมูล {len(user_targets)} users")
    except Exception as e:
        logger.error(f"❌ เกิดข้อผิดพลาดในการโหลดข้อมูล: {e}")

def save_data():
    """บันทึกข้อมูลลงไฟล์"""
    try:
        data = {
            'targets': {str(k): v for k, v in user_targets.items()},
            'settings': {str(k): v for k, v in user_settings.items()},
            'history': alert_history[-1000:]  # เก็บแค่ 1000 รายการล่าสุด
        }
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info("💾 บันทึกข้อมูลสำเร็จ")
    except Exception as e:
        logger.error(f"❌ เกิดข้อผิดพลาดในการบันทึกข้อมูล: {e}")

def get_user_settings(user_id: int) -> Dict:
    """ดึงการตั้งค่าของ user"""
    if user_id not in user_settings:
        user_settings[user_id] = {
            'check_interval': 5,  # นาที
            'notifications': True,
            'timezone': 'UTC'
        }
    return user_settings[user_id]

async def get_stock_price(symbol: str) -> Optional[Dict]:
    """ดึงราคาหุ้นจาก API พร้อม cache"""
    try:
        # Check cache (5 นาที)
        if symbol in stock_cache:
            cache_time = stock_cache[symbol].get('timestamp', 0)
            if datetime.now().timestamp() - cache_time < 300:  # 5 minutes
                return stock_cache[symbol]

        async with aiohttp.ClientSession() as session:
            url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={FINNHUB_API_KEY}"
            async with session.get(url, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('c') is not None:
                        result = {
                            'symbol': symbol,
                            'current': data.get('c'),
                            'high': data.get('h'),
                            'low': data.get('l'),
                            'open': data.get('o'),
                            'previous_close': data.get('pc'),
                            'change': data.get('c', 0) - data.get('pc', 0),
                            'change_percent': ((data.get('c', 0) - data.get('pc', 0)) / data.get('pc', 1)) * 100,
                            'timestamp': datetime.now().timestamp()
                        }
                        stock_cache[symbol] = result
                        return result
    except Exception as e:
        logger.error(f"❌ API error for {symbol}: {e}")
    return None

def create_stock_embed(stock_data: Dict, target: Optional[float] = None) -> discord.Embed:
    """สร้าง embed สำหรับแสดงข้อมูลหุ้น"""
    symbol = stock_data['symbol']
    current = stock_data['current']
    change = stock_data['change']
    change_percent = stock_data['change_percent']
    
    # สีตามการเปลี่ยนแปลง
    color = discord.Color.green() if change >= 0 else discord.Color.red()
    
    embed = discord.Embed(
        title=f"📈 {symbol}",
        color=color,
        timestamp=datetime.now()
    )
    
    embed.add_field(name="💰 ราคาปัจจุบัน", value=f"${current:.2f}", inline=True)
    
    change_emoji = "📈" if change >= 0 else "📉"
    embed.add_field(
        name=f"{change_emoji} การเปลี่ยนแปลง", 
        value=f"{change:+.2f} ({change_percent:+.2f}%)", 
        inline=True
    )
    
    if target:
        target_emoji = "⚠️" if current <= target else "✅"
        embed.add_field(name=f"{target_emoji} เป้าหมาย", value=f"${target:.2f}", inline=True)
    
    embed.add_field(name="📊 เปิด", value=f"${stock_data['open']:.2f}", inline=True)
    embed.add_field(name="🔺 สูงสุด", value=f"${stock_data['high']:.2f}", inline=True)
    embed.add_field(name="🔻 ต่ำสุด", value=f"${stock_data['low']:.2f}", inline=True)
    
    return embed

@tasks.loop(minutes=1)
async def check_stocks():
    """ตรวจสอบหุ้นตามช่วงเวลาที่กำหนด"""
    current_time = datetime.now()
    
    # ตรวจสอบเฉพาะช่วงเวลาเปิดตลาด (9:30-16:00 EST)
    if current_time.weekday() > 4:  # วันเสาร์-อาทิตย์
        return
        
    channel = bot.get_channel(CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        logger.warning("⚠️ Channel ไม่ถูกต้อง")
        return

    logger.info("⏳ กำลังตรวจสอบหุ้น...")
    
    for user_id, stocks in user_targets.items():
        user_settings_data = get_user_settings(user_id)
        
        if not user_settings_data.get('notifications', True):
            continue
            
        for symbol, stock_info in stocks.items():
            target = stock_info['target']
            alert_type = stock_info.get('type', 'below')  # below, above, both
            
            stock_data = await get_stock_price(symbol)
            if not stock_data:
                continue
                
            current_price = stock_data['current']
            should_alert = False
            
            # ตรวจสอบเงื่อนไขแจ้งเตือน
            if alert_type == 'below' and current_price <= target:
                should_alert = True
                alert_msg = f"📉 ต่ำกว่าหรือเท่ากับ"
            elif alert_type == 'above' and current_price >= target:
                should_alert = True
                alert_msg = f"📈 สูงกว่าหรือเท่ากับ"
            elif alert_type == 'both':
                if abs(current_price - target) / target <= 0.02:  # ภายใน 2%
                    should_alert = True
                    alert_msg = f"🎯 ใกล้เคียง"
            
            if should_alert:
                try:
                    user = await bot.fetch_user(user_id)
                    
                    # ลบข้อความเก่า
                    if user_id in last_alerts and symbol in last_alerts[user_id]:
                        try:
                            await last_alerts[user_id][symbol].delete()
                        except discord.NotFound:
                            pass
                    
                    # สร้าง embed
                    embed = create_stock_embed(stock_data, target)
                    embed.title = f"⚠️ แจ้งเตือนหุ้น {symbol}"
                    embed.description = f"{user.mention} หุ้น {symbol} {alert_msg} เป้าหมาย ${target:.2f}"
                    
                    msg = await channel.send(embed=embed)
                    
                    # เก็บข้อความล่าสุด
                    if user_id not in last_alerts:
                        last_alerts[user_id] = {}
                    last_alerts[user_id][symbol] = msg
                    
                    # บันทึกประวัติ
                    alert_history.append({
                        'user_id': user_id,
                        'symbol': symbol,
                        'price': current_price,
                        'target': target,
                        'type': alert_type,
                        'timestamp': datetime.now().isoformat()
                    })
                    
                except Exception as e:
                    logger.error(f"❌ ส่งแจ้งเตือนไม่สำเร็จ: {e}")

@bot.command(name="set", aliases=['s'])
async def set_target(ctx, symbol: str, target: float, alert_type: str = "below"):
    """ตั้งเป้าหมายหุ้น: !set AAPL 150 below"""
    symbol = symbol.upper()
    
    if alert_type not in ['below', 'above', 'both']:
        await ctx.send("❌ ประเภทการแจ้งเตือน: `below`, `above`, หรือ `both`")
        return
    
    if ctx.author.id not in user_targets:
        user_targets[ctx.author.id] = {}
    
    user_targets[ctx.author.id][symbol] = {
        'target': target,
        'type': alert_type,
        'created': datetime.now().isoformat()
    }
    
    save_data()
    
    embed = discord.Embed(
        title="✅ ตั้งเป้าหมายสำเร็จ",
        description=f"หุ้น **{symbol}** เป้าหมาย **${target:.2f}** ({alert_type})",
        color=discord.Color.green()
    )
    await ctx.send(embed=embed)

@bot.command(name="all", aliases=['list', 'l'])
async def show_targets(ctx):
    """แสดงเป้าหมายทั้งหมด"""
    targets = user_targets.get(ctx.author.id, {})
    if not targets:
        await ctx.send("❌ คุณยังไม่ได้ตั้งเป้าหมายหุ้นเลย")
        return
    
    embed = discord.Embed(
        title=f"📊 เป้าหมายหุ้นของ {ctx.author.display_name}",
        color=discord.Color.blue()
    )
    
    for symbol, info in targets.items():
        target = info['target']
        alert_type = info['type']
        created = datetime.fromisoformat(info['created']).strftime('%d/%m/%Y')
        embed.add_field(
            name=f"📈 {symbol}",
            value=f"เป้า: ${target:.2f}\nแจ้ง: {alert_type}\nตั้งเมื่อ: {created}",
            inline=True
        )
    
    await ctx.send(embed=embed)

@bot.command(name="check", aliases=['c'])
async def check_stock(ctx, symbol: str):
    """ตรวจสอบราคาหุ้น: !check AAPL"""
    symbol = symbol.upper()
    
    async with ctx.typing():
        stock_data = await get_stock_price(symbol)
        
    if not stock_data:
        await ctx.send(f"❌ ไม่สามารถดึงข้อมูล {symbol}")
        return
    
    target = None
    if ctx.author.id in user_targets and symbol in user_targets[ctx.author.id]:
        target = user_targets[ctx.author.id][symbol]['target']
    
    embed = create_stock_embed(stock_data, target)
    await ctx.send(embed=embed)

@bot.command(name="remove", aliases=['rm', 'del'])
async def remove_target(ctx, symbol: str):
    """ลบเป้าหมายหุ้น: !remove AAPL"""
    symbol = symbol.upper()
    
    if ctx.author.id in user_targets and symbol in user_targets[ctx.author.id]:
        del user_targets[ctx.author.id][symbol]
        save_data()
        
        embed = discord.Embed(
            title="🗑️ ลบเป้าหมายแล้ว",
            description=f"ลบเป้าหมาย **{symbol}** เรียบร้อยแล้ว",
            color=discord.Color.orange()
        )
        await ctx.send(embed=embed)
    else:
        await ctx.send("❌ คุณยังไม่ได้ตั้งเป้าหมายสำหรับหุ้นนี้")

@bot.command(name="settings")
async def user_settings_cmd(ctx, setting: str = None, value: str = None):
    """ตั้งค่าผู้ใช้: !settings notifications on"""
    user_id = ctx.author.id
    settings = get_user_settings(user_id)
    
    if not setting:
        embed = discord.Embed(title="⚙️ การตั้งค่า", color=discord.Color.blue())
        for key, val in settings.items():
            embed.add_field(name=key, value=val, inline=True)
        await ctx.send(embed=embed)
        return
    
    if setting == "notifications":
        if value in ['on', 'true', '1']:
            settings['notifications'] = True
            await ctx.send("🔔 เปิดการแจ้งเตือนแล้ว")
        elif value in ['off', 'false', '0']:
            settings['notifications'] = False
            await ctx.send("🔕 ปิดการแจ้งเตือนแล้ว")
        else:
            await ctx.send("❌ ใช้ on/off")
    
    save_data()

@bot.command(name="history", aliases=['h'])
async def show_history(ctx, limit: int = 10):
    """แสดงประวัติการแจ้งเตือน"""
    user_alerts = [a for a in alert_history if a['user_id'] == ctx.author.id]
    
    if not user_alerts:
        await ctx.send("❌ ไม่มีประวัติการแจ้งเตือน")
        return
    
    embed = discord.Embed(title="📋 ประวัติการแจ้งเตือน", color=discord.Color.purple())
    
    for alert in user_alerts[-limit:]:
        timestamp = datetime.fromisoformat(alert['timestamp']).strftime('%d/%m %H:%M')
        embed.add_field(
            name=f"{alert['symbol']} - {timestamp}",
            value=f"ราคา: ${alert['price']:.2f}\nเป้า: ${alert['target']:.2f}",
            inline=True
        )
    
    await ctx.send(embed=embed)

@bot.command(name="help", aliases=['h'])
async def show_help(ctx):
    """แสดงคำสั่งทั้งหมด"""
    embed = discord.Embed(
        title="🤖 คำสั่ง Stock Alert Bot",
        description="Bot สำหรับติดตามราคาหุ้น",
        color=discord.Color.gold()
    )
    
    commands_list = [
        ("!set <symbol> <price> [type]", "ตั้งเป้าหมายหุ้น (type: below/above/both)"),
        ("!check <symbol>", "ตรวจสอบราคาหุ้น"),
        ("!all", "แสดงเป้าหมายทั้งหมด"),
        ("!remove <symbol>", "ลบเป้าหมายหุ้น"),
        ("!settings [key] [value]", "จัดการการตั้งค่า"),
        ("!history [limit]", "ดูประวัติการแจ้งเตือน"),
        ("!help", "แสดงความช่วยเหลือ")
    ]
    
    for cmd, desc in commands_list:
        embed.add_field(name=cmd, value=desc, inline=False)
    
    await ctx.send(embed=embed)

@bot.event
async def on_ready():
    logger.info(f"✅ {bot.user} พร้อมใช้งาน!")
    load_data()
    check_stocks.start()

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        await ctx.send("❌ ไม่พบคำสั่งนี้ ใช้ `!help` เพื่อดูคำสั่งทั้งหมด")
    else:
        logger.error(f"Command error: {error}")
        await ctx.send("❌ เกิดข้อผิดพลาด กรุณาลองใหม่อีกครั้ง")

if __name__ == "__main__":
    if DISCORD_TOKEN:
        try:
            bot.run(DISCORD_TOKEN)
        except KeyboardInterrupt:
            logger.info("🛑 หยุดการทำงาน")
            save_data()
    else:
        logger.error("❌ DISCORD_TOKEN ยังไม่ถูกตั้งค่า")
