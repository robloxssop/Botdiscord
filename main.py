# main.py
import os
import json
import discord
from discord.ext import tasks, commands
from discord import app_commands
import requests
import yfinance as yf
from typing import Optional, Dict, Any

# ========== Config (ตั้งค่าใน Secrets / Environment) ==========
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")
# เช็คทุก 5 นาที (หน่วย: minutes)
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))
# ไฟล์เก็บข้อมูล (persist targets)
DATA_FILE = "targets.json"

# ========== Bot setup ==========
intents = discord.Intents.default()
intents.message_content = True  # ไม่จำเป็นสำหรับ slash แต่ไว้เผื่อใช้ข้อความด้วย
bot = commands.Bot(command_prefix="/", intents=intents)

# user_targets:
# { str(user_id): { "SYMBOL": { "target": float, "dm": bool, "channel_id": Optional[int] } } }
user_targets: Dict[str, Dict[str, Dict[str, Any]]] = {}

# last_alerts keeps latest sent discord.Message for deletion:
# { str(user_id): { "SYMBOL": discord.Message } }
last_alerts: Dict[str, Dict[str, discord.Message]] = {}

# ========== Persistence helpers ==========
def load_targets() -> None:
    global user_targets
    try:
        if os.path.isfile(DATA_FILE):
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                user_targets = json.load(f)
                # ensure proper types
                for uid, targets in list(user_targets.items()):
                    user_targets[uid] = {
                        sym: {"target": float(info["target"]), "dm": bool(info.get("dm", True)),
                              "channel_id": int(info["channel_id"]) if info.get("channel_id") is not None else None}
                        for sym, info in targets.items()
                    }
                print(f"[data] loaded targets for {len(user_targets)} users")
        else:
            user_targets = {}
    except Exception as e:
        print("[data] load error:", e)
        user_targets = {}

def save_targets() -> None:
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(user_targets, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("[data] save error:", e)

# ========== Price fetching ==========
def get_price_yf(symbol: str) -> Optional[float]:
    """Use yfinance for Thai (.BK) stocks."""
    try:
        t = yf.Ticker(symbol)
        hist = t.history(period="1d")
        if not hist.empty:
            # get latest close if present
            last = hist["Close"].iloc[-1]
            return float(last)
        # fallback to fast_info if available
        info = t.fast_info
        lp = info.get("lastPrice") if isinstance(info, dict) else getattr(info, "lastPrice", None)
        if lp:
            return float(lp)
    except Exception as e:
        print(f"[yfinance] error for {symbol}: {e}")
    return None

def get_price_finnhub(symbol: str) -> Optional[float]:
    """Use Finnhub for non-.BK symbols."""
    if not FINNHUB_API_KEY:
        return None
    try:
        url = "https://finnhub.io/api/v1/quote"
        r = requests.get(url, params={"symbol": symbol, "token": FINNHUB_API_KEY}, timeout=10)
        r.raise_for_status()
        j = r.json()
        c = j.get("c")
        if c is None:
            return None
        return float(c)
    except requests.RequestException as e:
        print(f"[finnhub] request error {symbol}: {e}")
    except Exception as e:
        print(f"[finnhub] unexpected {symbol}: {e}")
    return None

def get_stock_price(symbol: str) -> Optional[float]:
    """Decide which provider by symbol suffix (.BK => yfinance). No auto .BK addition."""
    symbol_norm = symbol.strip().upper()
    if symbol_norm.endswith(".BK"):
        return get_price_yf(symbol_norm)
    else:
        return get_price_finnhub(symbol_norm)

# ========== Utility ==========
def normalize_symbol_input(symbol: str) -> str:
    """Normalize to uppercase. We DO NOT append .BK automatically.
       If user wants Thai, they must provide .BK (per requirement)."""
    return symbol.strip().upper()

# ========== UI View (Buttons) ==========
class StockAlertView(discord.ui.View):
    def __init__(self, user_id: int, symbol: str):
        super().__init__(timeout=None)  # persistent for bot session
        self.user_id = user_id
        self.symbol = symbol

    @discord.ui.button(label="📊 ดูราคา", style=discord.ButtonStyle.primary, custom_id="btn_check_price")
    async def btn_check_price(self, interaction: discord.Interaction, button: discord.ui.Button):
        # allow only owner to use the buttons (optional)
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ คุณไม่มีสิทธิ์กดปุ่มนี้", ephemeral=True)
            return
        price = get_stock_price(self.symbol)
        if price is None:
            await interaction.response.send_message(f"❌ ไม่สามารถดึงราคาของ {self.symbol} ได้", ephemeral=True)
            return
        await interaction.response.send_message(f"💹 {self.symbol} ราคาปัจจุบัน: {price:.2f}", ephemeral=True)

    @discord.ui.button(label="🗑️ ลบเป้าหมาย", style=discord.ButtonStyle.danger, custom_id="btn_remove_target")
    async def btn_remove(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ คุณไม่มีสิทธิ์กดปุ่มนี้", ephemeral=True)
            return
        uid_str = str(self.user_id)
        if uid_str in user_targets and self.symbol in user_targets[uid_str]:
            # delete target
            user_targets[uid_str].pop(self.symbol, None)
            save_targets()
            # delete last alert message if exists
            try:
                msg = last_alerts.get(uid_str, {}).pop(self.symbol, None)
                if msg:
                    await msg.delete()
            except discord.NotFound:
                pass
            except discord.Forbidden:
                pass
            await interaction.response.send_message(f"✅ ลบเป้าหมาย {self.symbol} เรียบร้อย", ephemeral=True)
        else:
            await interaction.response.send_message("❌ เป้าหมายไม่พบแล้ว", ephemeral=True)

# ========== Slash commands ==========
@bot.event
async def on_ready():
    load_targets()
    # start background checking loop
    check_targets.start()
    print(f"[ready] Logged in as {bot.user} — checking every {CHECK_INTERVAL_MINUTES} minute(s)")
    try:
        await bot.tree.sync()
        print("[ready] Slash commands synced")
    except Exception as e:
        print("[ready] Slash sync error:", e)

@bot.tree.command(name="set", description="ตั้งเป้าหมายหุ้น: /set SYMBOL TARGET dm:true/false")
@app_commands.describe(symbol="Symbol เช่น AAPL หรือ PTT.BK (ถ้าเป็นหุ้นไทยต้องใส่ .BK เอง)", target="ราคาเป้าหมาย", dm="ส่งทาง DM หรือโพสต์ใน Channel")
async def slash_set(interaction: discord.Interaction, symbol: str, target: float, dm: bool = True):
    symbol_norm = normalize_symbol_input(symbol)

    # Enforce rule: do NOT automatically append .BK.
    # If user likely intended Thai but didn't add .BK, show a helpful hint (but still accept)
    if ".BK" not in symbol_norm and len(symbol_norm) <= 4:
        # short symbol without .BK — warn user that Thai requires .BK
        hint = "หมายเหตุ: ถ้าต้องการติดตามหุ้นไทย ให้พิมพ์ SYMBOL.BK เช่น PTT.BK (โค้ดจะไม่ต่อเติม .BK ให้อัตโนมัติ)."
    else:
        hint = None

    uid_str = str(interaction.user.id)
    # store channel id if dm=False
    channel_id = None
    if not dm and interaction.channel:
        channel_id = interaction.channel.id

    user_targets.setdefault(uid_str, {})
    user_targets[uid_str][symbol_norm] = {"target": float(target), "dm": bool(dm), "channel_id": channel_id}
    save_targets()

    # prepare embed
    embed = discord.Embed(
        title=f"📌 ตั้งเป้าหมาย {symbol_norm}",
        description=f"เป้าหมาย: **{target}**\nส่งทาง: **{'DM' if dm else 'Channel'}**",
        color=discord.Color.green()
    )
    embed.set_footer(text="หากต้องการหุ้นไทย ให้ใส่ .BK ต่อท้าย เช่น PTT.BK")

    # send initial confirmation (DM or channel)
    if dm:
        try:
            await interaction.user.send(embed=embed, view=StockAlertView(interaction.user.id, symbol_norm))
            await interaction.response.send_message("✅ ตั้งเป้าหมายเรียบร้อย — ส่งยืนยันทาง DM แล้ว", ephemeral=True)
        except discord.Forbidden:
            # can't DM -> fallback to channel
            await interaction.response.send_message("⚠️ ไม่สามารถส่ง DM ได้ — ส่งยืนยันในช่องนี้แทน", ephemeral=True)
            await interaction.channel.send(embed=embed, view=StockAlertView(interaction.user.id, symbol_norm))
    else:
        # post in the channel where user executed the command
        await interaction.response.send_message(embed=embed, view=StockAlertView(interaction.user.id, symbol_norm))

    if hint:
        try:
            await interaction.followup.send(hint, ephemeral=True)
        except Exception:
            pass

@bot.tree.command(name="all", description="ดูเป้าหมายทั้งหมดของคุณ")
async def slash_all(interaction: discord.Interaction):
    uid_str = str(interaction.user.id)
    targets = user_targets.get(uid_str, {})
    if not targets:
        await interaction.response.send_message("❌ คุณยังไม่ได้ตั้งเป้าหมายหุ้นใด ๆ", ephemeral=True)
        return

    embed = discord.Embed(
        title="🎯 เป้าหมายหุ้นของคุณ",
        description=f"ตั้งโดย {interaction.user.mention}",
        color=discord.Color.blue()
    )
    for sym, info in targets.items():
        dm_flag = info.get("dm", True)
        ch_id = info.get("channel_id")
        ch_text = f"Channel({ch_id})" if ch_id else "-"
        embed.add_field(name=sym, value=f"เป้า: **{info['target']}** | DM: {dm_flag} | {ch_text}", inline=False)
    embed.set_footer(text="ใช้ /set SYMBOL TARGET dm:true/false เพื่อตั้งเป้าหมาย")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="remove", description="ลบเป้าหมายหุ้นของคุณ")
async def slash_remove(interaction: discord.Interaction, symbol: str):
    symbol_norm = normalize_symbol_input(symbol)
    uid_str = str(interaction.user.id)
    if uid_str in user_targets and symbol_norm in user_targets[uid_str]:
        # delete stored target
        user_targets[uid_str].pop(symbol_norm, None)
        save_targets()
        # delete previous alert message if any
        prev = last_alerts.get(uid_str, {}).pop(symbol_norm, None)
        if prev:
            try:
                await prev.delete()
            except discord.NotFound:
                pass
            except discord.Forbidden:
                pass
        await interaction.response.send_message(f"🗑️ ลบ {symbol_norm} เรียบร้อย", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ ไม่พบ {symbol_norm} ในเป้าหมายของคุณ", ephemeral=True)

@bot.tree.command(name="helpme", description="ดูคำสั่งทั้งหมดของ bot")
async def slash_help(interaction: discord.Interaction):
    text = (
        "**คำสั่งหลัก:**\n"
        "/set SYMBOL TARGET dm:true/false — ตั้งเป้าหมาย (ถ้า dm=false จะโพสต์ใน Channel ที่ใช้คำสั่ง)\n"
        "/all — ดูเป้าหมายทั้งหมดของตัวเอง\n"
        "/remove SYMBOL — ลบเป้าหมาย\n\n"
        "หมายเหตุ: หากต้องการติดตามหุ้นไทย ให้ใส่ `.BK` เอง เช่น `PTT.BK`. โค้ดจะไม่ต่อเติม `.BK` ให้อัตโนมัติ."
    )
    await interaction.response.send_message(text, ephemeral=True)

# ========== Background checker ==========
@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def check_targets():
    if not user_targets:
        return
    # iterate over a shallow copy to avoid runtime-changes during loop
    for uid_str, targets in list(user_targets.items()):
        try:
            user_id = int(uid_str)
        except ValueError:
            continue

        # fetch user object (to mention & DM)
        try:
            user = await bot.fetch_user(user_id)
        except discord.NotFound:
            continue
        except Exception as e:
            print(f"[check] cannot fetch user {user_id}: {e}")
            continue

        for sym, info in list(targets.items()):
            try:
                target_price = float(info.get("target"))
            except Exception:
                continue
            dm_flag = bool(info.get("dm", True))
            channel_id = info.get("channel_id")

            price = get_stock_price(sym)
            if price is None:
                continue

            # alert condition: price <= target
            if price <= target_price:
                # delete previous alert message if exists
                prev_msg = last_alerts.get(uid_str, {}).get(sym)
                if prev_msg:
                    try:
                        await prev_msg.delete()
                    except discord.NotFound:
                        pass
                    except discord.Forbidden:
                        pass
                    except Exception as e:
                        print(f"[check] failed deleting prev msg for {uid_str} {sym}: {e}")

                # prepare embed (mention + emoji to trigger notification)
                embed = discord.Embed(
                    title=f"🔔 แจ้งเตือนหุ้น {sym}",
                    description=f"{user.mention}\nราคาปัจจุบัน **{price:.2f}** ต่ำกว่าหรือเท่ากับเป้า **{target_price}**",
                    color=discord.Color.red()
                )
                embed.set_footer(text=f"แจ้งเตือนอัตโนมัติทุก {CHECK_INTERVAL_MINUTES} นาที")

                # create view (buttons)
                view = StockAlertView(user_id, sym)

                # send to DM or Channel
                sent_msg = None
                if dm_flag:
                    try:
                        sent_msg = await user.send(embed=embed, view=view)
                    except discord.Forbidden:
                        # cannot DM - fallback to channel if available
                        if channel_id:
                            ch = bot.get_channel(channel_id)
                            if ch and isinstance(ch, discord.TextChannel):
                                try:
                                    sent_msg = await ch.send(content=f"{user.mention}", embed=embed, view=view)
                                except Exception as e:
                                    print(f"[check] failed send fallback channel: {e}")
                        else:
                            print(f"[check] cannot DM and no fallback channel for user {user_id}")
                else:
                    # prefer stored channel_id; if missing, fallback to user's last known guild channel cannot be found -> DM
                    if channel_id:
                        ch = bot.get_channel(channel_id)
                        if ch and isinstance(ch, discord.TextChannel):
                            try:
                                sent_msg = await ch.send(content=f"{user.mention}", embed=embed, view=view)
                            except Exception as e:
                                print(f"[check] failed send to channel {channel_id}: {e}")
                                # fallback to DM
                                try:
                                    sent_msg = await user.send(embed=embed, view=view)
                                except Exception as e2:
                                    print(f"[check] fallback DM failed: {e2}")
                        else:
                            # channel not found: fallback DM
                            try:
                                sent_msg = await user.send(embed=embed, view=view)
                            except Exception as e:
                                print(f"[check] send DM fallback failed: {e}")
                    else:
                        # no channel stored: fallback to DM
                        try:
                            sent_msg = await user.send(embed=embed, view=view)
                        except Exception as e:
                            print(f"[check] send DM failed: {e}")

                # store message for future deletion (if sent)
                if sent_msg:
                    last_alerts.setdefault(uid_str, {})[sym] = sent_msg

# ========== Run ==========
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("[ERROR] DISCORD_TOKEN not set in environment")
    else:
        bot.run(DISCORD_TOKEN)
