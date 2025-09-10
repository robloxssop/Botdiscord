import os
import discord
from discord.ext import commands, tasks
import requests
import yfinance as yf

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)

# user_targets: {user_id: {symbol: target_price}}
user_targets = {}
# last_alerts: {user_id: {symbol: message_obj}}
last_alerts = {}

# ============ ฟังก์ชันเช็คราคา ============
def get_stock_price(symbol: str) -> float | None:
    if symbol.upper().endswith(".BK"):  # หุ้นไทย
        try:
            ticker = yf.Ticker(symbol)
            price = ticker.history(period="1d")["Close"].iloc[-1]
            return float(price)
        except Exception as e:
            print(f"yfinance error: {e}")
            return None
    else:  # หุ้นต่างประเทศ
        url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={FINNHUB_API_KEY}"
        try:
            r = requests.get(url, timeout=10)
            data = r.json()
            return float(data.get("c", 0)) if data.get("c") else None
        except Exception as e:
            print(f"Finnhub error: {e}")
            return None

def format_symbol(symbol: str) -> str:
    s = symbol.upper()
    if len(s) <= 4 and not s.endswith(".BK"):  # เดาว่าเป็นหุ้นไทย
        return s + ".BK"
    return s

# ============ ปุ่มกด ============
class StockAlertView(discord.ui.View):
    def __init__(self, user_id: int, symbol: str):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.symbol = symbol

    @discord.ui.button(label="📊 ดูราคา", style=discord.ButtonStyle.primary)
    async def check_price(self, interaction: discord.Interaction, button: discord.ui.Button):
        price = get_stock_price(self.symbol)
        if price:
            await interaction.response.send_message(f"💹 {self.symbol} ราคาปัจจุบัน: {price:.2f}", ephemeral=True)
        else:
            await interaction.response.send_message("❌ ไม่สามารถดึงราคาได้", ephemeral=True)

    @discord.ui.button(label="🗑️ ลบเป้าหมาย", style=discord.ButtonStyle.danger)
    async def remove_target(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.user_id in user_targets and self.symbol in user_targets[self.user_id]:
            del user_targets[self.user_id][self.symbol]
            await interaction.response.send_message(f"✅ ลบเป้าหมาย {self.symbol} เรียบร้อย", ephemeral=True)
        else:
            await interaction.response.send_message("❌ ไม่มีเป้าหมายนี้", ephemeral=True)

# ============ Command ============
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    check_prices.start()

@bot.command()
async def set(ctx, stock: str, target: float):
    """ตั้งเป้าหมายหุ้น เช่น /set PTT 35"""
    symbol = format_symbol(stock)
    user_id = ctx.author.id

    if user_id not in user_targets:
        user_targets[user_id] = {}
    user_targets[user_id][symbol] = target

    await ctx.send(f"📌 {ctx.author.mention} ตั้งเป้า {symbol} ที่ {target}")

@bot.command()
async def all(ctx):
    """ดูเป้าหมายทั้งหมดของตัวเองแบบ Embed"""
    user_id = ctx.author.id
    targets = user_targets.get(user_id, {})
    if not targets:
        await ctx.send("❌ คุณยังไม่ได้ตั้งเป้า")
        return

    embed = discord.Embed(
        title="🎯 เป้าหมายหุ้นของคุณ",
        description=f"ตั้งโดย {ctx.author.mention}",
        color=discord.Color.green()
    )

    for symbol, target in targets.items():
        embed.add_field(
            name=symbol,
            value=f"📌 ราคาเป้าหมาย: **{target}**",
            inline=False
        )

    embed.set_footer(text="บอทติดตามหุ้น 24/7")
    embed.set_thumbnail(url="https://cdn-icons-png.flaticon.com/512/2331/2331943.png")
    await ctx.send(embed=embed)

@bot.command()
async def remove(ctx, stock: str):
    """ลบเป้าหมายหุ้น เช่น /remove PTT"""
    symbol = format_symbol(stock)
    user_id = ctx.author.id
    if user_id in user_targets and symbol in user_targets[user_id]:
        del user_targets[user_id][symbol]
        await ctx.send(f"🗑️ ลบ {symbol} เรียบร้อย")
    else:
        await ctx.send("❌ ไม่พบหุ้นนี้ในเป้าหมายของคุณ")

@bot.command()
async def helpme(ctx):
    """คำสั่งทั้งหมด"""
    help_text = """
📖 คำสั่งบอทหุ้น
/set [หุ้น] [ราคา] → ตั้งเป้าหมาย
/all → ดูเป้าหมายทั้งหมด
/remove [หุ้น] → ลบเป้าหมาย
/helpme → ดูคำสั่งทั้งหมด
    """
    await ctx.send(help_text)

# ============ Loop เช็คราคา ============
@tasks.loop(minutes=5)  # แจ้งเตือนทุก 5 นาที
async def check_prices():
    for user_id, targets in user_targets.items():
        user = await bot.fetch_user(user_id)
        for symbol, target in targets.items():
            price = get_stock_price(symbol)
            if price is None:
                continue

            if price <= target:
                # ลบข้อความเก่า
                if user_id in last_alerts and symbol in last_alerts[user_id]:
                    try:
                        await last_alerts[user_id][symbol].delete()
                    except:
                        pass

                # สร้าง Embed แจ้งเตือน พร้อม mention + emoji 🔔
                embed = discord.Embed(
                    title=f"🔔 แจ้งเตือนหุ้น {symbol}",
                    description=(
                        f"{user.mention}\n"
                        f"ราคาปัจจุบัน **{price:.2f}** ต่ำกว่าหรือเท่ากับเป้า **{target}**"
                    ),
                    color=discord.Color.red()
                )
                embed.set_footer(text="ระบบแจ้งเตือนอัตโนมัติทุก 5 นาที")
                embed.set_thumbnail(url="https://cdn-icons-png.flaticon.com/512/2331/2331930.png")

                msg = await user.send(embed=embed, view=StockAlertView(user_id, symbol))

                # เก็บข้อความล่าสุด
                if user_id not in last_alerts:
                    last_alerts[user_id] = {}
                last_alerts[user_id][symbol] = msg

# ============ Run ============
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
