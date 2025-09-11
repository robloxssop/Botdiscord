# main.py
# Discord stock alert bot (full-featured)
# - Finnhub for non-.BK (US / global)
# - yfinance for .BK (Thai)
# - Slash commands, embed, buttons, persistence, support calc
# - Author: generated assistant

import os
import json
import time
import asyncio
import logging
from typing import Optional, Dict, Any, Tuple, List

import requests
import yfinance as yf
import pandas as pd
import numpy as np
import discord
from discord.ext import tasks, commands
from discord import app_commands

# ------------------------
# Basic config / logging
# ------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("stockbot")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")  # optional (required for non-.BK)
DATA_FILE = "targets.json"
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))  # default 5 minutes

# ------------------------
# Bot init
# ------------------------
intents = discord.Intents.default()
intents.message_content = True  # not strictly required for slash, useful for fallback messages
bot = commands.Bot(command_prefix="/", intents=intents)

# ------------------------
# In-memory storage
# ------------------------
# user_targets: { user_id_str: { symbol: { "target": float, "dm": bool, "channel_id": Optional[int] } } }
user_targets: Dict[str, Dict[str, Dict[str, Any]]] = {}
# last_alerts: { user_id_str: { symbol: discord.Message } }
last_alerts: Dict[str, Dict[str, discord.Message]] = {}
# Locks for thread-safety
data_lock = asyncio.Lock()

# Simple cache for prices to reduce repeated requests in same interval
price_cache: Dict[str, Tuple[Optional[float], float]] = {}
PRICE_CACHE_TTL = 30  # seconds

# ------------------------
# Persistence helpers
# ------------------------
def load_data() -> None:
    global user_targets
    try:
        if os.path.isfile(DATA_FILE):
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                user_targets = json.load(f)
                # normalize types
                for uid, targets in list(user_targets.items()):
                    for sym, info in list(targets.items()):
                        try:
                            info["target"] = float(info.get("target"))
                            info["dm"] = bool(info.get("dm", True))
                            ch = info.get("channel_id", None)
                            info["channel_id"] = int(ch) if ch is not None else None
                        except (TypeError, ValueError):
                            logger.warning("Invalid stored target format; removing %s %s", uid, sym)
                            user_targets[uid].pop(sym, None)
            logger.info("Loaded targets for %d users", len(user_targets))
        else:
            user_targets = {}
    except Exception as e:
        logger.exception("Failed to load data: %s", e)
        user_targets = {}

def save_data() -> None:
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(user_targets, f, ensure_ascii=False, indent=2)
        logger.info("Saved targets to %s", DATA_FILE)
    except Exception as e:
        logger.exception("Failed to save data: %s", e)

# ------------------------
# Price fetchers
# ------------------------
def _cache_get(symbol: str) -> Optional[float]:
    now = time.time()
    cached = price_cache.get(symbol)
    if cached and now - cached[1] < PRICE_CACHE_TTL:
        return cached[0]
    return None

def _cache_set(symbol: str, price: Optional[float]) -> None:
    price_cache[symbol] = (price, time.time())

def get_price_finnhub(symbol: str) -> Optional[float]:
    """Query Finnhub for symbol (non-.BK). Returns price or None"""
    if not FINNHUB_API_KEY:
        return None
    cached = _cache_get(symbol)
    if cached is not None:
        return cached
    try:
        url = "https://finnhub.io/api/v1/quote"
        resp = requests.get(url, params={"symbol": symbol, "token": FINNHUB_API_KEY}, timeout=10)
        resp.raise_for_status()
        j = resp.json()
        c = j.get("c")
        price = float(c) if c is not None else None
        _cache_set(symbol, price)
        return price
    except requests.RequestException as e:
        logger.warning("Finnhub request failed for %s: %s", symbol, e)
    except Exception as e:
        logger.exception("Unexpected Finnhub error for %s: %s", symbol, e)
    _cache_set(symbol, None)
    return None

def get_price_yf(symbol: str) -> Optional[float]:
    """Use yfinance for .BK symbols. Robust against missing data."""
    cached = _cache_get(symbol)
    if cached is not None:
        return cached
    symbol_u = symbol.strip().upper()
    try:
        tk = yf.Ticker(symbol_u)
        hist = tk.history(period="2d", interval="1d", actions=False)
        if hist is not None and not hist.empty and "Close" in hist.columns:
            price = float(hist["Close"].iloc[-1])
            _cache_set(symbol, price)
            return price
        # fallback: try fast_info if available
        try:
            fi = tk.fast_info
            last_price = None
            if isinstance(fi, dict):
                last_price = fi.get("last_price") or fi.get("lastPrice") or fi.get("lastPrice")
            else:
                last_price = getattr(fi, "last_price", None) or getattr(fi, "lastPrice", None)
            if last_price:
                price = float(last_price)
                _cache_set(symbol, price)
                return price
        except Exception:
            pass
    except Exception as e:
        # yfinance throws when symbol invalid (e.g., NVDA.BK). Log and return None
        logger.debug("yfinance error for %s: %s", symbol_u, e)
    _cache_set(symbol, None)
    return None

def get_stock_price(symbol: str) -> Optional[float]:
    """Main price getter. If symbol endswith .BK -> yfinance; else Finnhub"""
    s = symbol.strip().upper()
    if s.endswith(".BK"):
        return get_price_yf(s)
    return get_price_finnhub(s)

# ------------------------
# Utility helpers
# ------------------------
def normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper()

# Format a friendly short price string
def fmt_price(p: Optional[float]) -> str:
    if p is None:
        return "N/A"
    try:
        return f"{p:,.4f}" if abs(p) < 1 else f"{p:,.2f}"
    except Exception:
        return str(p)

# ------------------------
# View / Buttons
# ------------------------
class StockAlertView(discord.ui.View):
    def __init__(self, owner_id: int, symbol: str):
        super().__init__(timeout=None)
        self.owner_id = owner_id
        self.symbol = symbol

    @discord.ui.button(label="📊 ดูราคา", style=discord.ButtonStyle.primary, custom_id="btn_view_price")
    async def view_price(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ คุณไม่มีสิทธิ์กดปุ่มนี้", ephemeral=True)
            return
        price = get_stock_price(self.symbol)
        if price is None:
            await interaction.response.send_message(f"❌ ไม่สามารถดึงราคาของ {self.symbol} ได้", ephemeral=True)
            return
        await interaction.response.send_message(f"💹 {self.symbol} ราคาปัจจุบัน: {fmt_price(price)}", ephemeral=True)

    @discord.ui.button(label="🗑️ ลบเป้าหมาย", style=discord.ButtonStyle.danger, custom_id="btn_remove_target")
    async def remove_target(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ คุณไม่มีสิทธิ์กดปุ่มนี้", ephemeral=True)
            return
        uid = str(self.owner_id)
        async with data_lock:
            if uid in user_targets and self.symbol in user_targets[uid]:
                user_targets[uid].pop(self.symbol, None)
                save_data()
                # delete previous alert message if present
                prev = last_alerts.get(uid, {}).pop(self.symbol, None)
                if prev:
                    try:
                        await prev.delete()
                    except discord.NotFound:
                        pass
                    except discord.Forbidden:
                        pass
                await interaction.response.send_message(f"✅ ลบเป้าหมาย {self.symbol} เรียบร้อย", ephemeral=True)
            else:
                await interaction.response.send_message("❌ ไม่พบเป้าหมายนี้", ephemeral=True)

    # custom button callback for "set to level" will be created dynamically by /support command when needed

# ------------------------
# Support (pivot / fib / sma / atr)
# ------------------------
def compute_support_resistance(symbol: str, lookback_days: int = 60) -> Optional[Dict[str, Any]]:
    symbol_u = normalize_symbol(symbol)
    try:
        tk = yf.Ticker(symbol_u)
        df = tk.history(period=f"{max(lookback_days,30)}d", interval="1d", actions=False)
    except Exception as e:
        logger.debug("yfinance history error for %s: %s", symbol_u, e)
        return None
    if df is None or df.empty or not set(["High", "Low", "Close"]).issubset(df.columns):
        return None

    df = df.dropna(subset=["High", "Low", "Close"])
    if df.empty:
        return None

    last = df.iloc[-1]
    last_high = float(last["High"])
    last_low = float(last["Low"])
    last_close = float(last["Close"])

    pivot = (last_high + last_low + last_close) / 3.0
    r1 = 2 * pivot - last_low
    s1 = 2 * pivot - last_high
    r2 = pivot + (last_high - last_low)
    s2 = pivot - (last_high - last_low)

    sma20 = float(df["Close"].rolling(window=20).mean().iloc[-1]) if len(df) >= 20 else None
    sma50 = float(df["Close"].rolling(window=50).mean().iloc[-1]) if len(df) >= 50 else None

    # ATR14
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr14 = float(tr.rolling(window=14).mean().iloc[-1]) if len(tr) >= 14 else None

    look_df = df.tail(lookback_days)
    swing_high = float(look_df["High"].max())
    swing_low = float(look_df["Low"].min())

    fib = {}
    if swing_high != swing_low:
        diff = swing_high - swing_low
        fib = {
            "0.0": swing_high,
            "0.382": swing_high - 0.382 * diff,
            "0.5": swing_high - 0.5 * diff,
            "0.618": swing_high - 0.618 * diff,
            "1.0": swing_low,
        }

    return {
        "symbol": symbol_u,
        "last_close": last_close,
        "pivot": pivot,
        "R1": r1,
        "S1": s1,
        "R2": r2,
        "S2": s2,
        "sma20": sma20,
        "sma50": sma50,
        "atr14": atr14,
        "swing_high": swing_high,
        "swing_low": swing_low,
        "fib": fib,
        "lookback_days": lookback_days
    }

# ------------------------
# Slash commands
# ------------------------
@bot.event
async def on_ready():
    load_data()
    try:
        await bot.tree.sync()
        logger.info("Slash commands synced")
    except Exception as e:
        logger.warning("Slash sync failed: %s", e)
    check_loop.start()
    logger.info("Bot ready. Checking every %d minutes", CHECK_INTERVAL_MINUTES)

@bot.tree.command(name="set", description="ตั้งเป้าหมายหุ้น เช่น /set AAPL 170 dm:true/false")
@app_commands.describe(symbol="เช่น AAPL หรือ PTT.BK (ถ้าเป็นหุ้นไทยต้องใส่ .BK เอง)", target="ราคาเป้าหมาย", dm="ส่ง DM หรือโพสต์ใน Channel")
async def slash_set(interaction: discord.Interaction, symbol: str, target: float, dm: bool = True):
    sym_norm = normalize_symbol(symbol)
    uid_str = str(interaction.user.id)
    channel_id = None
    if not dm and interaction.channel is not None:
        channel_id = interaction.channel.id

    async with data_lock:
        user_targets.setdefault(uid_str, {})
        user_targets[uid_str][sym_norm] = {"target": float(target), "dm": bool(dm), "channel_id": channel_id}
        save_data()

    embed = discord.Embed(
        title=f"📌 ตั้งเป้าหมาย {sym_norm}",
        description=f"เป้าหมาย: **{fmt_price(target)}**\nส่งทาง: **{'DM' if dm else 'Channel'}**",
        color=discord.Color.green()
    )
    embed.set_footer(text="หากต้องการหุ้นไทย ให้ใส่ .BK เช่น PTT.BK")

    # send confirmation: DM if dm else response in channel with view
    view = StockAlertView(interaction.user.id, sym_norm)
    if dm:
        try:
            await interaction.user.send(embed=embed, view=view)
            await interaction.response.send_message("✅ ตั้งเป้าหมายเรียบร้อย — ส่งยืนยันทาง DM แล้ว", ephemeral=True)
        except discord.Forbidden:
            # can't DM -> fallback to channel
            await interaction.response.send_message("⚠️ ไม่สามารถส่ง DM ได้ — จะแสดงยืนยันในช่องนี้แทน", ephemeral=True)
            await interaction.channel.send(embed=embed, view=view)
    else:
        await interaction.response.send_message(embed=embed, view=view)

@bot.tree.command(name="all", description="ดูเป้าหมายทั้งหมดของคุณ")
async def slash_all(interaction: discord.Interaction):
    uid_str = str(interaction.user.id)
    async with data_lock:
        targets = user_targets.get(uid_str, {})
    if not targets:
        await interaction.response.send_message("❌ คุณยังไม่ได้ตั้งเป้าหมายหุ้นใด ๆ", ephemeral=True)
        return

    embed = discord.Embed(title="🎯 เป้าหมายหุ้นของคุณ", description=f"ตั้งโดย {interaction.user.mention}", color=discord.Color.blue())
    for sym, info in targets.items():
        dm_flag = bool(info.get("dm", True))
        ch_id = info.get("channel_id")
        ch_text = f"Channel({ch_id})" if ch_id else "DM" if dm_flag else "-"
        embed.add_field(name=sym, value=f"เป้า: **{fmt_price(info['target'])}** | {ch_text}", inline=False)
    embed.set_footer(text="ใช้ /set SYMBOL TARGET dm:true/false เพื่อตั้งเป้าหมาย")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="remove", description="ลบเป้าหมายของคุณ")
@app_commands.describe(symbol="symbol ที่จะลบ เช่น AAPL หรือ PTT.BK")
async def slash_remove(interaction: discord.Interaction, symbol: str):
    sym_norm = normalize_symbol(symbol)
    uid_str = str(interaction.user.id)
    async with data_lock:
        if uid_str in user_targets and sym_norm in user_targets[uid_str]:
            user_targets[uid_str].pop(sym_norm, None)
            save_data()
            prev = last_alerts.get(uid_str, {}).pop(sym_norm, None)
            if prev:
                try:
                    await prev.delete()
                except discord.NotFound:
                    pass
                except discord.Forbidden:
                    pass
            await interaction.response.send_message(f"🗑️ ลบ {sym_norm} เรียบร้อย", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ ไม่พบ {sym_norm} ในเป้าหมายของคุณ", ephemeral=True)

@bot.tree.command(name="check", description="เช็คราคาหุ้นปัจจุบัน")
@app_commands.describe(symbol="เช่น AAPL หรือ PTT.BK")
async def slash_check(interaction: discord.Interaction, symbol: str):
    sym_norm = normalize_symbol(symbol)
    await interaction.response.defer(ephemeral=True)
    price = get_stock_price(sym_norm)
    if price is None:
        await interaction.followup.send(f"❌ ไม่สามารถดึงราคาของ {sym_norm} ได้", ephemeral=True)
    else:
        await interaction.followup.send(f"💹 {sym_norm} = {fmt_price(price)}", ephemeral=True)

@bot.tree.command(name="support", description="คำนวณแนวรับ/แนวต้าน (Pivot, SMA, ATR, Fibonacci)")
@app_commands.describe(symbol="เช่น AAPL หรือ PTT.BK", lookback="จำนวนวันที่ใช้หา swing high/low")
async def slash_support(interaction: discord.Interaction, symbol: str, lookback: int = 60):
    sym_norm = normalize_symbol(symbol)
    await interaction.response.defer()
    data = compute_support_resistance(sym_norm, lookback_days=lookback)
    if not data:
        await interaction.followup.send(f"❌ ไม่พบข้อมูลราคา {sym_norm}", ephemeral=True)
        return

    embed = discord.Embed(title=f"🧭 แนวรับ-แนวต้าน: {data['symbol']}", description=f"ราคาปิดล่าสุด: **{fmt_price(data['last_close'])}**", color=discord.Color.blurple())
    embed.add_field(name="Pivot (Classic)", value=f"P = {fmt_price(data['pivot'])}\nR1 = {fmt_price(data['R1'])}\nS1 = {fmt_price(data['S1'])}", inline=False)
    embed.add_field(name="R2 / S2", value=f"R2 = {fmt_price(data['R2'])}\nS2 = {fmt_price(data['S2'])}", inline=False)
    sma_text = f"SMA20 = {fmt_price(data['sma20'])}\nSMA50 = {fmt_price(data['sma50'])}"
    atr_text = f"ATR14 = {fmt_price(data['atr14'])}"
    embed.add_field(name="Moving averages", value=f"{sma_text}\n{atr_text}", inline=False)
    if data.get("fib"):
        fib_lines = "\n".join([f"{k}: {fmt_price(v)}" for k, v in data["fib"].items()])
        embed.add_field(name="Fibonacci (swing high->low)", value=fib_lines, inline=False)
    embed.set_footer(text="กดปุ่มด้านล่างเพื่อตั้งเป้าจากระดับที่ต้องการ")

    # create a view with dynamic buttons for fib levels + pivot + s1/s2
    view = discord.ui.View(timeout=None)
    # add a button to set to S1, Pivot, R1, fib_0.618 etc.
    async def make_set_callback(level_value: float):
        async def callback(interaction2: discord.Interaction):
            uid_str = str(interaction2.user.id)
            # ensure owner sets only their own targets
            sym = sym_norm
            async with data_lock:
                user_targets.setdefault(uid_str, {})
                user_targets[uid_str][sym] = {"target": float(level_value), "dm": True, "channel_id": None}
                save_data()
            await interaction2.response.send_message(f"✅ ตั้งเป้า {sym} = {fmt_price(level_value)} ให้บัญชีของคุณแล้ว (DM)", ephemeral=True)
        return callback

    # standard levels to show as buttons (limit to reasonable number)
    levels: List[Tuple[str, float]] = []
    levels.append(("S1", data["S1"]))
    levels.append(("Pivot", data["pivot"]))
    levels.append(("R1", data["R1"]))
    # add fib keys sorted by closeness to last_close
    fib_map = data.get("fib", {})
    for k, v in fib_map.items():
        levels.append((f"Fib {k}", v))

    # attach buttons (max 5 to avoid UI clutter)
    for i, (label, val) in enumerate(levels[:5]):
        if val is None:
            continue
        # create button and callback
        custom_id = f"set_{label}_{int(time.time())}_{i}"
        btn = discord.ui.Button(label=f"Set {label} {fmt_price(val)}", style=discord.ButtonStyle.secondary)
        # assign a callback
        callback_coro = make_set_callback(val)
        btn.callback = callback_coro  # type: ignore
        view.add_item(btn)

    # also add a generic "Set custom" button that opens ephemeral prompt (simpler: instruct user)
    view.add_item(discord.ui.Button(label="Manual /set", style=discord.ButtonStyle.link, url="https://discord.com/"))  # placeholder

    await interaction.followup.send(embed=embed, view=view)

@bot.tree.command(name="helpme", description="แสดงคำสั่งของบอท")
async def slash_helpme(interaction: discord.Interaction):
    txt = (
        "**คำสั่งหลัก**:\n"
        "/set SYMBOL TARGET dm:true/false — ตั้งเป้าหมาย (ถ้า dm=false จะโพสต์ในช่องที่รันคำสั่ง)\n"
        "/all — ดูเป้าหมายของคุณ\n"
        "/remove SYMBOL — ลบเป้าหมาย\n"
        "/check SYMBOL — เช็คราคาปัจจุบัน\n"
        "/support SYMBOL [lookback] — คำนวณแนวรับ/แนวต้าน\n\n"
        "หมายเหตุ: หากต้องการติดตามหุ้นไทย ให้ใส่ `.BK` เอง เช่น `PTT.BK`. โค้ดจะไม่ต่อเติม `.BK` ให้อัตโนมัติ."
    )
    await interaction.response.send_message(txt, ephemeral=True)

# ------------------------
# Background checking loop
# ------------------------
@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def check_loop():
    logger.info("Running check loop for targets (users=%d)", len(user_targets))
    # shallow copy keys to avoid mutation during loop
    async with data_lock:
        local_targets = {uid: dict(targets) for uid, targets in user_targets.items()}  # deep copy top-level
    for uid_str, targets_map in local_targets.items():
        try:
            user_id = int(uid_str)
        except (TypeError, ValueError):
            continue
        # fetch user for mention and DM
        try:
            user = await bot.fetch_user(user_id)
        except discord.NotFound:
            logger.warning("User %s not found", uid_str)
            continue
        except Exception as e:
            logger.warning("Failed to fetch user %s: %s", uid_str, e)
            continue

        for sym, info in list(targets_map.items()):
            try:
                target_price = float(info.get("target"))
            except (TypeError, ValueError):
                continue
            dm_flag = bool(info.get("dm", True))
            ch_id = info.get("channel_id")
            price = get_stock_price(sym)
            if price is None:
                continue

            # alert condition: price <= target (as requested)
            if price <= target_price:
                # remove previous message if any
                prev_msg = last_alerts.get(uid_str, {}).get(sym)
                if prev_msg:
                    try:
                        await prev_msg.delete()
                    except discord.NotFound:
                        pass
                    except discord.Forbidden:
                        pass
                    except Exception as e:
                        logger.debug("Failed deleting prev message: %s", e)

                # build embed
                embed = discord.Embed(
                    title=f"🔔 แจ้งเตือนหุ้น {sym}",
                    description=f"{user.mention}\nราคาปัจจุบัน **{fmt_price(price)}** ต่ำกว่าหรือเท่ากับเป้า **{fmt_price(target_price)}**",
                    color=discord.Color.red()
                )
                embed.set_footer(text=f"แจ้งเตือนอัตโนมัติ ทุก {CHECK_INTERVAL_MINUTES} นาที")
                view = StockAlertView(user_id, sym)

                sent_msg = None
                # send DM or channel
                if dm_flag:
                    try:
                        sent_msg = await user.send(embed=embed, view=view)
                    except discord.Forbidden:
                        # fallback to channel if provided
                        if ch_id:
                            ch = bot.get_channel(ch_id)
                            if ch and isinstance(ch, discord.TextChannel):
                                try:
                                    sent_msg = await ch.send(content=f"{user.mention}", embed=embed, view=view)
                                except Exception as e:
                                    logger.warning("Failed sending fallback channel msg: %s", e)
                        else:
                            logger.warning("Cannot DM user %s and no fallback channel", uid_str)
                else:
                    # send to stored channel if possible else try DM
                    if ch_id:
                        ch = bot.get_channel(ch_id)
                        if ch and isinstance(ch, discord.TextChannel):
                            try:
                                sent_msg = await ch.send(content=f"{user.mention}", embed=embed, view=view)
                            except Exception as e:
                                logger.warning("Failed send to channel %s: %s", ch_id, e)
                                try:
                                    sent_msg = await user.send(embed=embed, view=view)
                                except Exception as e2:
                                    logger.warning("Fallback DM failed: %s", e2)
                        else:
                            try:
                                sent_msg = await user.send(embed=embed, view=view)
                            except Exception as e:
                                logger.warning("Fallback DM failed: %s", e)
                    else:
                        try:
                            sent_msg = await user.send(embed=embed, view=view)
                        except Exception as e:
                            logger.warning("Failed send DM: %s", e)

                # store last alert
                if sent_msg:
                    last_alerts.setdefault(uid_str, {})[sym] = sent_msg

# ------------------------
# Run
# ------------------------
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN not set in environment")
        raise SystemExit("DISCORD_TOKEN required")
    load_data()
    bot.run(DISCORD_TOKEN)ORD_TOKEN)
