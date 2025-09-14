import os
import yfinance as yf
import requests
import time
import logging
import json
import threading
from flask import Flask, redirect, url_for, render_template_string, request, session, jsonify
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
import discord
from discord.ext import commands

# --- Setup Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("stock_alert_bot")

# --- Environment Variables (Secrets) ---
DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.environ.get("DISCORD_REDIRECT_URI")
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")

if not all([DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, DISCORD_REDIRECT_URI, DISCORD_BOT_TOKEN]):
    logger.error("❌ กรุณาตั้งค่า DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, DISCORD_REDIRECT_URI และ DISCORD_BOT_TOKEN ใน Environment Variables ให้ครบถ้วน")

# --- Flask App Setup ---
app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24)

login_manager = LoginManager()
login_manager.init_app(app)

# --- Database & User Management ---
class User(UserMixin):
    def __init__(self, user_id, username):
        self.id = user_id
        self.username = username
        self.targets = {}

    def get_id(self):
        return self.id

class Database:
    def __init__(self, file_path='discord_users.json'):
        self.file_path = file_path
        self.data = self.load_data()

    def load_data(self):
        try:
            with open(self.file_path, 'r') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def save_data(self):
        with open(self.file_path, 'w') as f:
            json.dump(self.data, f, indent=4)

    def get_user(self, user_id):
        user_data = self.data.get(str(user_id))
        if user_data:
            user = User(user_data['id'], user_data['username'])
            user.targets = user_data['targets']
            return user
        return None

    def add_user(self, user):
        self.data[str(user.id)] = {'id': user.id, 'username': user.username, 'targets': {}}
        self.save_data()

    def update_user_targets(self, user_id, targets):
        if str(user_id) in self.data:
            self.data[str(user_id)]['targets'] = targets
            self.save_data()

db = Database()

@login_manager.user_loader
def load_user(user_id):
    return db.get_user(user_id)

# --- Discord OAuth2 Logic ---
DISCORD_API_BASE = 'https://discord.com/api/v10'

@app.route('/login')
def login_discord():
    return redirect(f"https://discord.com/oauth2/authorize?client_id={DISCORD_CLIENT_ID}&redirect_uri={DISCORD_REDIRECT_URI}&response_type=code&scope=identify")

@app.route('/callback')
def callback():
    code = request.args.get('code')
    if not code:
        return "Authorization failed.", 400

    token_url = f"{DISCORD_API_BASE}/oauth2/token"
    data = {
        'client_id': DISCORD_CLIENT_ID,
        'client_secret': DISCORD_CLIENT_SECRET,
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': DISCORD_REDIRECT_URI,
        'scope': 'identify'
    }
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    r = requests.post(token_url, data=data, headers=headers)
    
    if r.status_code != 200:
        logger.error(f"Failed to get token: {r.text}")
        return "Failed to get access token.", 500

    token_info = r.json()
    access_token = token_info.get('access_token')

    user_url = f"{DISCORD_API_BASE}/users/@me"
    user_headers = {'Authorization': f'Bearer {access_token}'}
    user_r = requests.get(user_url, headers=user_headers)

    if user_r.status_code != 200:
        logger.error(f"Failed to get user info: {user_r.text}")
        return "Failed to get user info.", 500

    user_info = user_r.json()
    user_id = user_info['id']
    username = user_info['username']

    user = db.get_user(user_id)
    if not user:
        user = User(user_id, username)
        db.add_user(user)

    login_user(user)
    return redirect(url_for('dashboard'))

# --- Web Application Routes ---
@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return render_template_string("""
        <!doctype html>
        <html lang="th">
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1, shrink-to-fit=no">
            <title>ล็อกอินด้วย Discord</title>
            <style>
                body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; text-align: center; padding-top: 50px; background-color: #f0f2f5; color: #333; }
                .container { max-width: 450px; margin: auto; padding: 40px; border-radius: 12px; background-color: white; box-shadow: 0 10px 20px rgba(0,0,0,0.05); }
                h1 { color: #5865F2; margin-bottom: 20px; }
                .btn { padding: 12px 20px; border: none; border-radius: 8px; cursor: pointer; font-size: 16px; transition: background-color 0.3s; }
                .btn-discord { background-color: #5865F2; color: white; text-decoration: none; }
                .btn-discord:hover { background-color: #4B55C4; }
            </style>
        </head>
        <body>
            <div class="container">
                <h1>เข้าสู่ระบบด้วย Discord</h1>
                <a href="/login" class="btn btn-discord">เข้าสู่ระบบด้วย Discord</a>
            </div>
        </body>
        </html>
    """)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

@app.route('/dashboard')
@login_required
def dashboard():
    targets = db.get_user(current_user.id).targets
    return render_template_string("""
        <!doctype html>
        <html lang="th">
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1, shrink-to-fit=no">
            <title>Dashboard</title>
            <style>
                body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; padding: 20px; background-color: #f0f2f5; color: #333; }
                .container { max-width: 800px; margin: auto; padding: 40px; border-radius: 12px; background-color: white; box-shadow: 0 10px 20px rgba(0,0,0,0.05); }
                .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 30px; }
                h2 { color: #5865F2; }
                .target-form { margin-top: 20px; padding: 30px; border: 1px solid #ddd; border-radius: 12px; background-color: #fafafa; }
                .form-group { margin-bottom: 15px; }
                .form-group input, .form-group select { width: 100%; padding: 12px; border: 1px solid #ccc; border-radius: 8px; box-sizing: border-box; }
                .btn { padding: 12px 20px; border: none; border-radius: 8px; cursor: pointer; font-size: 16px; transition: background-color 0.3s; }
                .btn-success { background-color: #28a745; color: white; }
                .btn-danger { background-color: #dc3545; color: white; }
                .logout-btn { background-color: #5865F2; color: white; padding: 10px 20px; text-decoration: none; border-radius: 8px; }
                .target-list { margin-top: 30px; }
                .target-item { display: flex; justify-content: space-between; align-items: center; padding: 15px; margin-bottom: 10px; border: 1px solid #eee; border-radius: 8px; background-color: #fcfcfc; }
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h2>สวัสดี, {{ current_user.username }}</h2>
                    <a href="{{ url_for('logout') }}" class="logout-btn">ออกจากระบบ</a>
                </div>
                
                <div class="target-form">
                    <h3>ตั้งเป้าหมายหุ้นใหม่</h3>
                    <form id="set-target-form">
                        <div class="form-group">
                            <label for="symbol">ชื่อหุ้น:</label>
                            <input type="text" id="symbol" name="symbol" placeholder="เช่น AAPL หรือ PTT.BK" required>
                        </div>
                        <div class="form-group">
                            <label for="target_price">ราคาเป้าหมาย:</label>
                            <input type="number" step="0.01" id="target_price" name="target_price" placeholder="ราคาเป้าหมายเป็นตัวเลข" required>
                        </div>
                        <div class="form-group">
                            <label for="trigger_type">เงื่อนไขการแจ้งเตือน:</label>
                            <select id="trigger_type" name="trigger_type">
                                <option value="below">ราคาต่ำกว่า/เท่ากับ</option>
                                <option value="above">ราคาสูงกว่า/เท่ากับ</option>
                            </select>
                        </div>
                        <button type="submit" class="btn btn-success">ตั้งค่า</button>
                    </form>
                </div>

                <div class="target-list">
                    <h3>รายการเป้าหมายหุ้นของคุณ</h3>
                    {% if targets %}
                        {% for symbol, data in targets.items() %}
                            <div class="target-item">
                                <div>
                                    <strong>{{ symbol }}</strong>: เป้าหมายที่ **{{ data.target }}** บาท<br>
                                    เงื่อนไข: {{ 'ราคาต่ำกว่า/เท่ากับ' if data.trigger_type == 'below' else 'ราคาสูงกว่า/เท่ากับ' }}
                                </div>
                                <button class="btn btn-danger" onclick="deleteTarget('{{ symbol }}')">ลบ</button>
                            </div>
                        {% endfor %}
                    {% else %}
                        <p>คุณยังไม่ได้ตั้งเป้าหมายหุ้นใดๆ</p>
                    {% endif %}
                </div>
            </div>
            
            <script>
                document.getElementById('set-target-form').onsubmit = async (e) => {
                    e.preventDefault();
                    const formData = new FormData(e.target);
                    const data = Object.fromEntries(formData.entries());
                    const response = await fetch('/api/set_target', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(data)
                    });
                    const result = await response.json();
                    alert(result.message);
                    if (result.success) { window.location.reload(); }
                };

                async function deleteTarget(symbol) {
                    if (!confirm(`คุณต้องการลบเป้าหมายของหุ้น ${symbol} หรือไม่?`)) return;
                    const response = await fetch('/api/delete_target', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ symbol: symbol })
                    });
                    const result = await response.json();
                    alert(result.message);
                    if (result.success) { window.location.reload(); }
                }
            </script>
        </body>
        </html>
    """, targets=targets)

# --- API Endpoints ---
@app.route('/api/set_target', methods=['POST'])
@login_required
def api_set_target():
    data = request.json
    symbol = data.get('symbol', '').upper()
    target_price = data.get('target_price')
    trigger_type = data.get('trigger_type', 'below')
    
    try:
        target_price = float(target_price)
    except (ValueError, TypeError):
        return jsonify(success=False, message="❌ กรุณากรอกราคาเป็นตัวเลขที่ถูกต้อง"), 400

    user = db.get_user(current_user.id)
    user.targets[symbol] = {
        'target': target_price,
        'trigger_type': trigger_type,
        'notified': False
    }
    db.update_user_targets(current_user.id, user.targets)
    
    return jsonify(success=True, message=f"✅ ตั้งเป้าหมายสำหรับ **{symbol}** ที่ **{target_price}** บาทเรียบร้อยแล้ว")

@app.route('/api/delete_target', methods=['POST'])
@login_required
def api_delete_target():
    data = request.json
    symbol = data.get('symbol', '').upper()
    user = db.get_user(current_user.id)
    
    if symbol in user.targets:
        del user.targets[symbol]
        db.update_user_targets(current_user.id, user.targets)
        return jsonify(success=True, message=f"🗑️ ลบเป้าหมายสำหรับ **{symbol}** เรียบร้อยแล้ว")
    else:
        return jsonify(success=False, message="❌ ไม่พบเป้าหมายที่คุณตั้งไว้"), 404

# --- Stock Check and Discord Notify Logic ---
def fetch_price_blocking(symbol: str):
    try:
        ticker = yf.Ticker(symbol)
        data = ticker.history(period="1d", interval="1m")
        if data.empty:
            return None
        return float(data["Close"].iloc[-1])
    except Exception as e:
        logger.warning(f"ไม่สามารถดึงราคาหุ้น {symbol}: {e}")
        return None

async def send_discord_notification(user_id: str, message: str):
    intents = discord.Intents.default()
    bot = commands.Bot(command_prefix="!", intents=intents)

    @bot.event
    async def on_ready():
        user = await bot.fetch_user(user_id)
        if user:
            await user.send(message)
            logger.info(f"Notification sent to Discord user {user.name} ({user_id})")
        await bot.close()

    await bot.start(DISCORD_BOT_TOKEN)

def run_stock_checker():
    while True:
        logger.info("เริ่มตรวจสอบราคาหุ้น...")
        all_users_data = db.load_data()
        
        for user_id_str, user_data in all_users_data.items():
            user_id = int(user_id_str)
            targets_to_check = list(user_data.get('targets', {}).items())
            
            for symbol, data in targets_to_check:
                target = data.get('target')
                trigger_type = data.get('trigger_type', 'below')
                # ลบส่วนนี้ออกเพื่อให้แจ้งเตือนซ้ำ: notified = data.get('notified', False)
                
                current_price = fetch_price_blocking(symbol)
                if current_price is None:
                    continue
                
                should_notify = False
                if (trigger_type == 'below' and current_price <= target) or \
                   (trigger_type == 'above' and current_price >= target):
                    should_notify = True
                
                if should_notify:
                    message = f"📢 แจ้งเตือนหุ้นถึงเป้าหมาย!\n" \
                              f"หุ้น: {symbol}\n" \
                              f"ราคาปัจจุบัน: {current_price} บาท\n" \
                              f"ราคาเป้าหมาย: {target} บาท"
                    
                    try:
                        import asyncio
                        asyncio.run(send_discord_notification(user_id, message))
                    except Exception as e:
                        logger.error(f"Error sending Discord notification: {e}")
                    
                    # ลบส่วนนี้ออกเพื่อไม่ให้มีการบันทึกสถานะ notified:
                    # user_data['targets'][symbol]['notified'] = True
                    # db.save_data()
        
        time.sleep(60) # เปลี่ยนจาก 300 เป็น 60

# --- Main Entry Point ---
if __name__ == "__main__":
    # Start the stock checker in a separate thread
    Thread(target=run_stock_checker, daemon=True).start()
    
    # Run the web server
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
