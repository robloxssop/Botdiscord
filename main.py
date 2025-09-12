"""
main.py
Discord Stock Alert Bot (single-file)
- Commands: /set, /check, /all (slash commands)
- Messages: ภาษาไทย (ยกเว้นชื่อคำสั่ง)
- Supports: US stocks and Thai (.BK)
- Persistence: targets.json
- Auto-check interval: 5 minutes (configurable)
"""

import discord
from discord import app_commands, ui
from discord.ext import tasks
import yfinance as yf
import json
import os
import logging
from typing import Optional, Dict, Any
from datetime import datetime, timezone

# -----------------------
# Basic configuration
# -----------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("stockbot")

# ใส่ DISCORD_TOKEN ใน environment variable หรือแก้ค่า default ตรงนี้
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", None)  # ถ้าว่าง บอทจะไม่รัน
# ตรวจสอบก่อนรัน
if not DISCORD_TOKEN:
    logger.warning("DISCORD_TOKEN ไม่ได้ถูกตั้งค่าใน environment variable")

# ไฟล์เก็บเป้าหมาย
DATA_FILE = "targets.json"

# ความถี่การตรวจ (เปลี่ยนเป็น seconds/minutes ตามต้องการ)
CHECK_INTERVAL_MINUTES = 5

# -----------------------
# Discord client + tree
# -----------------------
intents = discord.Intents.default()
# ไม่ต้องเปิด message_content ถ้าเราใช้เฉพาะ slash commands
intents.message_content = True  # บางฟีเจอร์ interactive ต้องใช้ True ในบางเวอร์ชัน
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# -----------------------
# In-memory structure
# -----------------------
# รูปแบบ targets:
# {
#   "<user_id>": {
#       "<SYMBOL>": {
#           "target": float,
#           "dm": bool,
#           "channel_id": int or None,
#           "last_msg": {"channel_id": int, "message_id": int, "timestamp": "iso"} or None,
#           "created_at": "iso"
#       },
#       ...
#   },
#   ...
# }
targets: Dict[str, Dict[str, Dict[str, Any]]] = {}

# -----------------------
# Persistence helpers
# -----------------------
def load_targets():
    global targets
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                targets = json.load(f)
            logger.info("โหลดข้อมูลเป้าหมายจาก %s เรียบร้อยแล้ว", DATA_FILE)
        except (IOError, json.JSONDecodeError) as e:
            logger.error("โหลดไฟล์ targets ผิดพลาด: %s", e)
            targets = {}
    else:
        targets = {}
        logger.info("ไฟล์ targets.json ยังไม่มี - เริ่มด้วยข้อมูลเปล่า")

def save_targets():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(targets, f, indent=2, ensure_ascii=False)
        logger.debug("บันทึก targets ลงไฟล์เรียบร้อย")
    except IOError as e:
        logger.error("บันทึก targets ผิดพลาด: %s", e)

# -----------------------
# Utility helpers
# -----------------------
def normalize_symbol(symbol: str) -> str:
    """
    ทำให้สัญลักษณ์เป็นตัวพิมพ์ใหญ่ และ trim whitespace
    ถ้าเป็นหุ้นไทยผู้ใช้ต้องเติม .BK เอง (ตามที่ผู้ใช้ต้องการ)
    """
    return symbol.strip().upper()

def safe_float(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()

# -----------------------
# Market data helpers
# -----------------------
def get_price(symbol: str) -> Optional[float]:
    """ดึงราคาปิดล่าสุด (intraday close) โดยใช้ yfinance"""
    try:
        ticker = yf.Ticker(symbol)
        # หากต้องการความแม่นสูงสุด อาจเพิ่มพารามิเตอร์หรือ fallback
        data = ticker.history(period="1d", interval="1d")
        if data is None or data.empty:
            logger.debug("yfinance: ไม่มีข้อมูลสำหรับ %s", symbol)
            return None
        last_close = data["Close"].iloc[-1]
        return float(last_close)
    except Exception as e:
        logger.error("get_price error for %s: %s", symbol, e, exc_info=True)
        return None

def fetch_history(symbol: str, days: int = 60):
    """ดึง history ของหุ้นเป็น DataFrame"""
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=f"{days}d")
        if df is None or df.empty:
            return None
        return df
    except Exception as e:
        logger.error("fetch_history error for %s: %s", symbol, e, exc_info=True)
        return None

# -----------------------
# Advanced Support/Resistance calculation
# -----------------------
def calc_support(symbol: str) -> Optional[float]:
    """
    คำนวณแนวรับขั้นสูง (weighted lows + trend + volatility + candle gap)
    คืนค่า None หากข้อมูลไม่พอ
    """
    df = fetch_history(symbol, days=90)
    if df is None or len(df) < 20:
        return None
    try:
        low5 = df["Low"][-5:].min()
        low10 = df["Low"][-10:].min()
        low20 = df["Low"][-20:].min()
        ma20 = float(df["Close"][-20:].mean())
        ma50 = float(df["Close"][-50:].mean()) if len(df) >= 50 else ma20
        std20 = float(df["Close"][-20:].std())
        current = float(df["Close"].iloc[-1])

        vol_factor = std20 / current if current != 0 else 0.0
        trend_factor = 0.01 if ma20 > ma50 else -0.01

        # ตรวจสอบแท่งเทียนล่าสุดว่ามีการร่วงหนักหรือไม่ (gap down / big red candle)
        last_open = float(df["Open"].iloc[-1])
        last_close = float(df["Close"].iloc[-1])
        last_drop_pct = (last_close - last_open) / last_open if last_open != 0 else 0.0
        gap_factor = 0.02 if last_drop_pct < -0.03 else (0.01 if last_drop_pct < -0.015 else 0.0)

        weighted_low = (0.45 * low5) + (0.35 * low10) + (0.2 * low20)

        # base support แล้วปรับตาม volatility & trend & gap
        support = weighted_low * (1 - (vol_factor * 0.5) + trend_factor - gap_factor)

        # safety floor: ไม่ให้แนวรับเกินความเป็นจริง เช่นต่ำกว่า 0
        support = max(support, 0.0)
        return round(support, 2)
    except Exception as e:
        logger.error("calc_support error for %s: %s", symbol, e, exc_info=True)
        return None

def calc_resistance(symbol: str) -> Optional[float]:
    """
    คำนวณแนวต้านขั้นสูง (weighted highs + trend + volatility + gap up)
    """
    df = fetch_history(symbol, days=90)
    if df is None or len(df) < 20:
        return None
    try:
        high5 = df["High"][-5:].max()
        high10 = df["High"][-10:].max()
        high20 = df["High"][-20:].max()
        ma20 = float(df["Close"][-20:].mean())
        ma50 = float(df["Close"][-50:].mean()) if len(df) >= 50 else ma20
        std20 = float(df["Close"][-20:].std())
        current = float(df["Close"].iloc[-1])

        vol_factor = std20 / current if current != 0 else 0.0
        trend_factor = 0.01 if ma20 > ma50 else -0.01

        last_open = float(df["Open"].iloc[-1])
        last_close = float(df["Close"].iloc[-1])
        last_gain_pct = (last_close - last_open) / last_open if last_open != 0 else 0.0
        gap_factor = 0.02 if last_gain_pct > 0.03 else (0.01 if last_gain_pct > 0.015 else 0.0)

        weighted_high = (0.45 * high5) + (0.35 * high10) + (0.2 * high20)

        resistance = weighted_high * (1 + (vol_factor * 0.5) + trend_factor + gap_factor)
        resistance = max(resistance, 0.0)
        return round(resistance, 2)
    except Exception as e:
        logger.error("calc_resistance error for %s: %s", symbol, e, exc_info=True)
        return None

# -----------------------
# Message / Embed builders (สวยและอ่านง่าย)
# -----------------------
def build_target_embed(user_name: str, symbol: str, price: Optional[float], target: float,
                       support: Optional[float], resistance: Optional[float]) -> discord.Embed:
    """
    สร้าง Embed ภาษาไทยสวยงามสำหรับแสดงข้อมูลหุ้นของผู้ใช้
    """
    title = f"📈 การแจ้งเตือนเป้าหมายสำหรับ {symbol}"
    description_lines = []
    description_lines.append(f"ผู้ตั้งเป้า: **{user_name}**")
    if price is not None:
        description_lines.append(f"💰 ราคาปัจจุบัน: **{price:.2f}**")
    else:
        description_lines.append("💰 ราคาปัจจุบัน: **ไม่พร้อมใช้งาน**")
    description_lines.append(f"🎯 เป้าหมายที่ตั้งไว้: **{target}**")
    if support is not None:
        description_lines.append(f"📉 แนวรับ (โดยประมาณ): **{support}**")
    if resistance is not None:
        description_lines.append(f"📈 แนวต้าน (โดยประมาณ): **{resistance}**")
    description_lines.append(f"⏱️ เวลาแจ้งเตือน: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    embed = discord.Embed(title=title,
                          description="\n".join(description_lines),
                          color=discord.Color.blue())
    embed.set_footer(text="Stock Alert Bot • ใช้ข้อมูลจาก Yahoo Finance (yfinance)")
    return embed

def build_check_embed(user_name: str, symbol: str, price: Optional[float], support: Optional[float],
                      resistance: Optional[float], target: Optional[float]) -> discord.Embed:
    title = f"🔎 ข้อมูลหุ้น {symbol}"
    desc = []
    if price is None:
        desc.append("⚠️ ไม่สามารถดึงราคาปัจจุบันได้")
    else:
        desc.append(f"💰 ราคาปัจจุบัน: **{price:.2f}**")
    if target is not None:
        desc.append(f"🎯 เป้าหมายของคุณ: **{target}**")
    if support is not None:
        desc.append(f"📉 แนวรับโดยประมาณ: **{support}**")
    if resistance is not None:
        desc.append(f"📈 แนวต้านโดยประมาณ: **{resistance}**")
    desc.append(f"ข้อมูลเมื่อ: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    embed = discord.Embed(title=title, description="\n".join(desc), color=discord.Color.dark_gold())
    return embed

# -----------------------
# UI: Modal for set-new-target (เมื่อกดปุ่ม 'ตั้งเป้าใหม่')
# -----------------------
class NewTargetModal(ui.Modal, title="ตั้งเป้าหมายใหม่"):
    stock_field = ui.TextInput(label="สัญลักษณ์หุ้น (เช่น AAPL หรือ PTT.BK)", placeholder="AAPL", required=True)
    target_field = ui.TextInput(label="ราคาเป้าหมาย", placeholder="170", required=True)

    def __init__(self, user_id: str):
        super().__init__()
        self.user_id = user_id  # เก็บไว้ทำงานหลัง submit

    async def on_submit(self, interaction: discord.Interaction):
        # เก็บค่าเมื่อ user submit modal
        symbol = normalize_symbol(self.stock_field.value)
        try:
            target_val = float(self.target_field.value)
        except ValueError:
            await interaction.response.send_message("❌ ค่าราคาเป้าหมายไม่ถูกต้อง โปรดระบุเป็นตัวเลข", ephemeral=True)
            return

        # ถ้า user ไม่มี record ให้สร้าง
        if self.user_id not in targets:
            targets[self.user_id] = {}
        # channel id เก็บจาก interaction.channel ถ้าเป็น DM จะเป็น DM channel
        channel_id = interaction.channel.id if interaction.channel is not None else None
        targets[self.user_id][symbol] = {
            "target": target_val,
            "dm": True if isinstance(interaction.channel, discord.DMChannel) else False,
            "channel_id": channel_id,
            "last_msg": None,
            "created_at": iso_now()
        }
        save_targets()
        await interaction.response.send_message(f"✅ ตั้งเป้าหมายหุ้น {symbol} ที่ {target_val} เรียบร้อยแล้ว (ผ่าน modal)", ephemeral=True)

# -----------------------
# View (Buttons) - ปรับให้สวยและปลอดภัย
# -----------------------
class StockView(ui.View):
    def __init__(self, user_id: str, symbol: str):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.symbol = symbol

    @ui.button(label="📊 เช็คราคา", style=discord.ButtonStyle.primary, custom_id="check_price")
    async def check_price_button(self, interaction: discord.Interaction, button: ui.Button):
        # ให้แสดง Embed ของหุ้นนี้เท่านั้น (ephemeral)
        price = get_price(self.symbol)
        support = calc_support(self.symbol)
        resistance = calc_resistance(self.symbol)
        # target ของ user ถ้ามี
        user_targets = targets.get(str(self.user_id), {})
        target_val = user_targets.get(self.symbol, {}).get("target")
        embed = build_check_embed(interaction.user.display_name, self.symbol, price, support, resistance, target_val)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @ui.button(label="🎯 ตั้งเป้าใหม่", style=discord.ButtonStyle.secondary, custom_id="set_new")
    async def set_new_button(self, interaction: discord.Interaction, button: ui.Button):
        # เปิด modal ให้ผู้ใช้ตั้งเป้าหมายใหม่
        if str(interaction.user.id) != str(self.user_id):
            await interaction.response.send_message("⚠️ ปุ่มนี้ใช้ได้เฉพาะผู้ตั้งเป้าหมายเท่านั้น", ephemeral=True)
            return
        modal = NewTargetModal(self.user_id)
        await interaction.response.send_modal(modal)

    @ui.button(label="❌ ลบเป้าหมาย", style=discord.ButtonStyle.danger, custom_id="delete_target")
    async def delete_button(self, interaction: discord.Interaction, button: ui.Button):
        if str(interaction.user.id) != str(self.user_id):
            await interaction.response.send_message("⚠️ ปุ่มนี้ใช้ได้เฉพาะผู้ตั้งเป้าหมายเท่านั้น", ephemeral=True)
            return
        user_targets = targets.get(str(self.user_id), {})
        if self.symbol in user_targets:
            del user_targets[self.symbol]
            save_targets()
            await interaction.response.send_message(f"🗑️ ลบเป้าหมายของ `{self.symbol}` เรียบร้อยแล้ว", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ ไม่พบเป้าหมายนี้ในบัญชีของคุณ", ephemeral=True)

# -----------------------
# Slash commands (English names) - responses in Thai
# -----------------------
@tree.command(name="set", description="Set a stock target")
@app_commands.describe(stock="Stock symbol, e.g. AAPL or PTT.BK", target="Target price (number)", dm="Send DM? (true => DM, false => post in this channel)")
async def cmd_set(interaction: discord.Interaction, stock: str, target: float, dm: bool = True):
    """
    จัดเก็บเป้าหมายของผู้ใช้
    - หาก dm=True => บอทจะส่งแจ้งเตือนไปทาง DM
    - หาก dm=False => บอทจะโพสต์แจ้งเตือนใน Channel ที่สั่งคำสั่ง (recorded channel)
    """
    await interaction.response.defer(ephemeral=True)
    user_id = str(interaction.user.id)
    symbol = normalize_symbol(stock)

    # หากเป็นหุ้นไทย ให้บอกผู้ใช้ว่ายังไงต้องเติม .BK ถ้ายังไม่ได้ใส่ (ไม่บังคับ แต่แนะนำ)
    if symbol.endswith(".BK") is False and any(ch.isalpha() for ch in symbol) and symbol.isalpha():
        # It's likely US symbol; do nothing. For Thai, user must include .BK.
        pass

    # สร้างโครงถ้ายังไม่มี
    if user_id not in targets:
        targets[user_id] = {}

    # เก็บช่อง (channel_id) เพื่อโพสต์เมื่อ dm=False
    channel_id = None
    if not dm:
        # หากเรียกใน DM, channel จะเป็น DMChannel ซึ่งไม่เหมาะโพสต์ต่อ ให้ fallback เป็น None
        if interaction.channel is not None and not isinstance(interaction.channel, discord.DMChannel):
            channel_id = interaction.channel.id
        else:
            # ถ้าเรียกจาก DM และตั้ง dm=False ให้บอกผู้ใช้ว่าต้องเรียกจาก Server channel
            await interaction.followup.send("❗ หากต้องการโพสต์ใน Channel กรุณารันคำสั่งในห้องแชทของเซิร์ฟเวอร์ (ไม่ใช่ DM) หรือตั้ง dm=true", ephemeral=True)
            return

    targets[user_id][symbol] = {
        "target": float(target),
        "dm": bool(dm),
        "channel_id": channel_id,
        "last_msg": None,
        "created_at": iso_now()
    }

    save_targets()
    # ตอบกลับเป็นภาษาไทย (ephemeral ให้เฉพาะผู้ใช้เห็น)
    await interaction.followup.send(f"✅ ตั้งเป้าหมายหุ้น `{symbol}` ที่ราคา {target} เรียบร้อยแล้ว (ส่งผ่าน {'DM' if dm else 'ช่องแชท'})", ephemeral=True)

@tree.command(name="check", description="Check your saved targets")
async def cmd_check(interaction: discord.Interaction):
    """แสดงเป้าหมายหุ้นของผู้ใช้เป็น Embed ภาษาไทย"""
    user_id = str(interaction.user.id)
    if user_id not in targets or not targets[user_id]:
        await interaction.response.send_message("ℹ️ คุณยังไม่ได้ตั้งเป้าหมายหุ้นใด ๆ", ephemeral=True)
        return

    embed = discord.Embed(title="📊 เป้าหมายหุ้นของคุณ", color=discord.Color.blue())
    for symbol, info in targets[user_id].items():
        price = get_price(symbol)
        support = calc_support(symbol)
        resistance = calc_resistance(symbol)
        price_text = f"{price:.2f}" if price is not None else "ไม่พร้อมใช้งาน"
        support_text = f"{support}" if support is not None else "-"
        resistance_text = f"{resistance}" if resistance is not None else "-"
        field_value = f"🎯 เป้าหมาย: **{info['target']}**\n💰 ปัจจุบัน: **{price_text}**\n📉 แนวรับ: **{support_text}** | 📈 แนวต้าน: **{resistance_text}**"
        embed.add_field(name=symbol, value=field_value, inline=False)

    # ส่งแบบ ephemeral เพื่อเฉพาะผู้ใช้เห็น
    # ใส่ปุ่มชุดสำหรับรายการตัวอย่าง (ใช้ symbol และ user_id ของรายการสุดท้ายเพื่อ view)
    # แต่เราจะแนบ view เฉพาะเมื่อมีตัวเดียวหรือเมื่อผู้ใช้คลิกบน message
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="all", description="View all saved targets (admin-style)")
async def cmd_all(interaction: discord.Interaction):
    """
    แสดงเป้าหมายทั้งหมดแบบสรุป (ephemeral)
    เหมาะสำหรับเจ้าของบอท/แอดมิน จะเห็นรายชื่อผู้ตั้งและเป้าหมาย
    """
    if not targets:
        await interaction.response.send_message("ℹ️ ยังไม่มีการตั้งเป้าหมายหุ้นใด ๆ", ephemeral=True)
        return

    embed = discord.Embed(title="📢 รายการเป้าหมายทั้งหมด", color=discord.Color.green())
    for user_id, user_targets in targets.items():
        # พยายามดึง display name ของ user ถ้ามี
        try:
            user = client.get_user(int(user_id)) or await client.fetch_user(int(user_id))
            display = user.display_name
        except Exception:
            display = f"User {user_id}"
        summary_lines = []
        for sym, info in user_targets.items():
            dm_flag = "DM" if info.get("dm") else "Channel"
            summary_lines.append(f"{sym} @ {info.get('target')} ({dm_flag})")
        embed.add_field(name=f"{display}", value="\n".join(summary_lines)[:1024], inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)

# -----------------------
# Auto-check loop: ตรวจทุก ๆ CHECK_INTERVAL_MINUTES
# -----------------------
@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def auto_check_loop():
    logger.debug("Auto check loop running - users=%d", len(targets))
    # ทำสำเนา keys เพื่อป้องกัน modification ระหว่าง loop
    for user_id in list(targets.keys()):
        user_targets = targets.get(user_id, {})
        # หากไม่มีรายการข้าม
        if not user_targets:
            continue
        # พยายาม fetch user object
        try:
            user = client.get_user(int(user_id)) or await client.fetch_user(int(user_id))
        except Exception as e:
            logger.warning("ไม่สามารถดึง user %s: %s", user_id, e)
            continue

        for symbol, info in list(user_targets.items()):
            try:
                price = get_price(symbol)
                # ข้ามถ้าไม่สามารถดึงราคา
                if price is None:
                    logger.debug("ราคา %s ไม่พร้อมสำหรับ user %s", symbol, user_id)
                    continue

                target_val = safe_float(info.get("target"))
                if target_val is None:
                    logger.warning("Target invalid for %s/%s", user_id, symbol)
                    continue

                # เงื่อนไขการแจ้ง: แจ้งเมื่อราคาต่ำหรือเท่ากับเป้าหมาย (ตามที่ร้องขอ)
                if price <= target_val:
                    support = calc_support(symbol)
                    resistance = calc_resistance(symbol)

                    embed = build_target_embed(user.display_name, symbol, price, target_val, support, resistance)

                    view = StockView(user_id, symbol)

                    # ถ้ามีข้อความเก่าที่เราส่งไว้ก่อนหน้านี้ ให้ลบก่อน (cleanup)
                    last_msg = info.get("last_msg")
                    if last_msg and isinstance(last_msg, dict):
                        old_ch = last_msg.get("channel_id")
                        old_mid = last_msg.get("message_id")
                        try:
                            if old_ch and old_mid:
                                ch_obj = client.get_channel(int(old_ch)) or await client.fetch_channel(int(old_ch))
                                if ch_obj:
                                    old_msg = await ch_obj.fetch_message(int(old_mid))
                                    if old_msg:
                                        await old_msg.delete()
                        except Exception:
                            # ล้มเหลวในการลบเก่า (อาจถูกลบไปแล้ว) — ไม่ต้องหยุดการแจ้งเตือน
                            logger.debug("ไม่สามารถลบข้อความเก่าของ %s %s ได้", user_id, symbol)

                    # ส่งแจ้งเตือน: ถ้า user ตั้ง dm=True => ส่ง DM, ถ้า dm=False => ส่งใน channel_id ที่บันทึกไว้
                    sent_message = None
                    if info.get("dm", True):
                        try:
                            dm_ch = await user.create_dm()
                            sent_message = await dm_ch.send(embed=embed, view=view)
                        except Exception as e:
                            logger.warning("ไม่สามารถส่ง DM ให้ user %s: %s", user_id, e)
                            # หากส่ง DM ไม่ได้ อาจ fallback ไปโพสต์ใน channel ถ้ามี channel_id บันทึกไว้
                            channel_id = info.get("channel_id")
                            if channel_id:
                                try:
                                    ch_obj = client.get_channel(int(channel_id)) or await client.fetch_channel(int(channel_id))
                                    if ch_obj:
                                        sent_message = await ch_obj.send(content=f"<@{user_id}>", embed=embed, view=view)
                                except Exception as e2:
                                    logger.error("fallback post to channel failed: %s", e2)
                    else:
                        # ส่งใน channel ที่บันทึกไว้ (channel_id) — ถ้าไม่มี ให้ข้ามและแจ้ง log
                        channel_id = info.get("channel_id")
                        if channel_id:
                            try:
                                ch_obj = client.get_channel(int(channel_id)) or await client.fetch_channel(int(channel_id))
                                if ch_obj:
                                    sent_message = await ch_obj.send(content=f"<@{user_id}>", embed=embed, view=view)
                            except Exception as e:
                                logger.error("ไม่สามารถส่ง notification ใน channel %s: %s", channel_id, e)
                        else:
                            logger.info("user %s ตั้ง dm=False แต่ไม่ได้บันทึก channel_id; ข้ามการส่ง", user_id)

                    # บันทึก last_msg (channel_id, message_id, timestamp)
                    if sent_message is not None:
                        try:
                            targets[user_id][symbol]["last_msg"] = {
                                "channel_id": sent_message.channel.id,
                                "message_id": sent_message.id,
                                "timestamp": iso_now()
                            }
                            save_targets()
                        except Exception as e:
                            logger.error("บันทึก last_msg ผิดพลาด: %s", e)

            except Exception as ex:
                logger.error("Auto-check inner error for %s %s: %s", user_id, symbol, ex, exc_info=True)

# -----------------------
# Ready event
# -----------------------
@client.event
async def on_ready():
    logger.info("Logged in as %s — syncing commands...", client.user)
    load_targets()
    try:
        await tree.sync()
    except Exception as e:
        logger.warning("Command tree sync warning: %s", e)
    # เริ่ม loop แจ้งเตือน
    if not auto_check_loop.is_running():
        auto_check_loop.start()
    logger.info("Bot พร้อมใช้งาน — ตรวจสอบทุก %d นาที", CHECK_INTERVAL_MINUTES)

# -----------------------
# Run bot
# -----------------------
if __name__ == "__main__":
    if DISCORD_TOKEN:
        client.run(DISCORD_TOKEN)
    else:
        logger.error("DISCORD_TOKEN ยังไม่ถูกตั้งค่า — โปรดตั้งค่า environment variable แล้วรันอีกครั้ง")
