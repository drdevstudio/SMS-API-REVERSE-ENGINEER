import os
import time
import hashlib
import threading
import requests
from datetime import datetime
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)

# --- TELEGRAM CONFIG ---
TELEGRAM_BOT_TOKEN = "8781048492:AAGRfyo1zDDu9HCkg_FTQ_9WFn7JwfixX_c"
TELEGRAM_CHAT_ID = "7882443060"

# --- AMUNDI API CONFIG ---
API_SIGN_KEY = '7h3paiw5oL901yWTNo2wiTKt5RtQ7MFP'

# --- GLOBAL STATE ---
STATE = {
    "start_time": time.time(),
    "start_time_str": datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC"),
    "otp_sent": 0,
    "otp_failed": 0,
    "total_numbers": 0,
    "logs": [],
    "is_sending": False,
    "current_number": None,
    "last_response": None
}

LOG_LOCK = threading.Lock()

# --- LOGGING ---
def log_sys(msg, level="info", target="N/A"):
    with LOG_LOCK:
        # Hide last 5 digits of phone number
        if target and len(target) >= 10 and target.isdigit():
            hidden_target = target[:5] + "*****"
        else:
            hidden_target = target
        
        entry = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "message": msg,
            "level": level,
            "target": hidden_target
        }
        STATE["logs"].insert(0, entry)
        if len(STATE["logs"]) > 500:
            STATE["logs"] = STATE["logs"][:500]

# --- TELEGRAM FUNCTIONS ---
def send_telegram_message(phone, status, response_data=None):
    """Send OTP status to Telegram."""
    try:
        if response_data:
            message = f"""🔐 AMUNDI OTP REQUEST
📱 Phone: {phone}
📊 Status: {status}
💬 Message: {response_data.get('message', 'N/A')}
📦 Data: {response_data.get('data', 'N/A')}
⏰ Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
🔒 Anonymous: ✓"""
        else:
            message = f"""📱 AMUNDI OTP REQUEST
Phone: {phone}
Status: {status}
⏰ Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"""
        
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message
        }
        response = requests.post(url, json=payload, timeout=10)
        return response.status_code == 200
    except Exception as e:
        log_sys(f"TELEGRAM: Failed to send - {str(e)}", "error")
        return False

# --- AMUNDI OTP GENERATION ---
def generate_sign_and_timestamp(payload_dict):
    """Generate MD5 signature for Amundi API."""
    timestamp_str = str(int(time.time() * 1000))
    params = payload_dict.copy()
    params["timeStamp"] = timestamp_str
    
    filtered_keys = [k for k in params.keys() if params[k] is not None and str(params[k]) != ""]
    filtered_keys.sort()
    
    mapped_parts = []
    for key in filtered_keys:
        val_str = str(params[key]).replace('"', '')
        mapped_parts.append(f"{key}={val_str}")
    
    sorted_qs = "&".join(mapped_parts)
    raw_string = f"{sorted_qs}&key={API_SIGN_KEY}" if sorted_qs else ""
    sign = hashlib.md5(raw_string.encode('utf-8')).hexdigest().lower()
    
    return sign, timestamp_str

def send_otp(phone):
    """Send OTP to Amundi API."""
    payload = {"phone": phone, "type": 10}
    sign, timestamp = generate_sign_and_timestamp(payload)
    
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
        "Content-Type": "application/json",
        "Origin": "https://h5.amundi-fund.com",
        "Referer": "https://h5.amundi-fund.com/d2c/index.html?code=6eu72P&haggleId=5078091",
        "timestamp": timestamp,
        "sign": sign
    }
    
    try:
        response = requests.post(
            "https://h5.amundi-fund.com/api/sso/common/send",
            json=payload,
            headers=headers,
            timeout=15
        )
        
        try:
            response_json = response.json()
        except:
            response_json = {"status": response.status_code, "message": response.text[:100]}
        
        return response.status_code, response_json
    except Exception as e:
        return None, {"status": "error", "message": str(e)}

# --- BACKGROUND WORKER ---
def process_otp(phone):
    """Process OTP in background thread."""
    global STATE
    
    STATE["is_sending"] = True
    STATE["current_number"] = phone
    log_sys(f"Processing OTP request", "info", target=phone)
    
    # Send initial notification to Telegram (not logged)
    send_telegram_message(phone, "PROCESSING")
    
    # Send OTP
    status_code, response_data = send_otp(phone)
    
    if status_code == 200:
        STATE["otp_sent"] += 1
        STATE["total_numbers"] += 1
        STATE["last_response"] = response_data
        log_sys(f"✅ OTP SENT successfully!", "success", target=phone)
        
        # Send success to Telegram with response (not logged)
        send_telegram_message(phone, "SUCCESS ✅", response_data)
    else:
        STATE["otp_failed"] += 1
        log_sys(f"❌ OTP FAILED - Status: {status_code}", "error", target=phone)
        
        # Send failure to Telegram with response (not logged)
        send_telegram_message(phone, f"FAILED ❌ ({status_code})", response_data)
    
    STATE["is_sending"] = False
    STATE["current_number"] = None

# --- FLASK ROUTES ---
@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/send_otp', methods=['POST'])
def send_otp_route():
    if STATE["is_sending"]:
        return jsonify({"status": "busy", "message": "Already processing. Please wait."}), 429
    
    data = request.get_json()
    phone = data.get('phone', '').strip()
    
    if not phone:
        return jsonify({"status": "error", "message": "Phone number required"}), 400
    
    # Validate 10 digits starting with 6,7,8,9
    if not phone.isdigit() or len(phone) != 10:
        return jsonify({"status": "error", "message": "Must be exactly 10 digits"}), 400
    
    if phone[0] not in ['6', '7', '8', '9']:
        return jsonify({"status": "error", "message": "Must start with 6, 7, 8, or 9"}), 400
    
    # Start background thread
    thread = threading.Thread(target=process_otp, args=(phone,))
    thread.daemon = True
    thread.start()
    
    return jsonify({
        "status": "processing", 
        "message": "OTP request sent. Check logs for status."
    })

@app.route('/api/stats')
def stats():
    uptime_seconds = int(time.time() - STATE["start_time"])
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    return jsonify({
        "uptime": f"{hours:02d}h {minutes:02d}m {seconds:02d}s",
        "started_at": STATE["start_time_str"],
        "otp_sent": STATE["otp_sent"],
        "otp_failed": STATE["otp_failed"],
        "total_numbers": STATE["total_numbers"],
        "is_sending": STATE["is_sending"],
        "current_number": STATE["current_number"],
        "last_response": STATE["last_response"],
        "logs": STATE["logs"][:80]
    })

@app.route('/api/clear', methods=['POST'])
def clear_state():
    STATE["current_number"] = None
    STATE["last_response"] = None
    return jsonify({"status": "success"})

# --- UI TEMPLATE ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AMUNDI OTP ROUTER</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            background: #0a0a0a;
            color: #00ff41;
            font-family: 'Courier New', Courier, monospace;
            min-height: 100vh;
            padding: 20px;
            background-image: 
                radial-gradient(ellipse at top, #0a2a0a 0%, #0a0a0a 70%);
        }
        
        .glitch {
            text-shadow: 
                0 0 10px #00ff41,
                0 0 20px #00ff41,
                0 0 40px #00ff41;
            animation: glitch 3s infinite;
        }
        
        @keyframes glitch {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.8; text-shadow: 0 0 20px #00ff41, 0 0 60px #00ff41, 0 0 80px #00ff41; }
        }
        
        .container {
            max-width: 1200px;
            margin: 0 auto;
        }
        
        .header {
            text-align: center;
            padding: 30px 0;
            border-bottom: 2px solid #00ff41;
            margin-bottom: 30px;
        }
        
        .header h1 {
            font-size: 2.5em;
            letter-spacing: 8px;
            text-transform: uppercase;
        }
        
        .header .subtitle {
            color: #00aa33;
            font-size: 0.9em;
            margin-top: 10px;
            letter-spacing: 4px;
        }
        
        .scanline {
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 2px;
            background: linear-gradient(to right, transparent, #00ff41, transparent);
            animation: scan 4s linear infinite;
            opacity: 0.3;
            z-index: 999;
        }
        
        @keyframes scan {
            0% { top: 0; }
            100% { top: 100%; }
        }
        
        .matrix-bg {
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            pointer-events: none;
            background: 
                repeating-linear-gradient(0deg, 
                    transparent, 
                    transparent 2px, 
                    rgba(0, 255, 65, 0.02) 2px, 
                    rgba(0, 255, 65, 0.02) 4px);
            z-index: -1;
        }
        
        .stats-top {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 15px;
            margin-bottom: 25px;
            max-width: 600px;
            margin-left: auto;
            margin-right: auto;
        }
        
        .stat-card {
            border: 1px solid #00ff41;
            padding: 15px 10px;
            text-align: center;
            background: rgba(0, 255, 65, 0.03);
            transition: all 0.3s;
        }
        
        .stat-card:hover {
            box-shadow: 0 0 20px rgba(0, 255, 65, 0.1);
        }
        
        .stat-card .label {
            font-size: 0.7em;
            color: #00aa33;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        
        .stat-card .value {
            font-size: 28px;
            font-weight: bold;
            margin-top: 5px;
            text-shadow: 0 0 10px rgba(0, 255, 65, 0.3);
        }
        
        .stat-card.success .value { color: #00ff41; }
        .stat-card.danger .value { color: #ff0040; }
        .stat-card.info .value { color: #00ccff; }
        
        .input-section {
            background: rgba(0, 20, 0, 0.8);
            border: 1px solid #00ff41;
            padding: 30px;
            margin-bottom: 30px;
            box-shadow: 0 0 30px rgba(0, 255, 65, 0.1);
            backdrop-filter: blur(5px);
        }
        
        .input-row {
            display: flex;
            gap: 15px;
            align-items: center;
            flex-wrap: wrap;
            justify-content: center;
        }
        
        .input-row label {
            font-size: 1.1em;
            color: #00ff41;
            text-transform: uppercase;
            letter-spacing: 2px;
        }
        
        .input-row input {
            background: #000;
            border: 1px solid #00ff41;
            color: #00ff41;
            padding: 12px 20px;
            font-family: 'Courier New', Courier, monospace;
            font-size: 1.1em;
            width: 200px;
            outline: none;
            transition: all 0.3s;
            text-align: center;
            letter-spacing: 4px;
        }
        
        .input-row input:focus {
            box-shadow: 0 0 20px rgba(0, 255, 65, 0.3);
            border-color: #00ff41;
        }
        
        .input-row input::placeholder {
            color: #006600;
            letter-spacing: 2px;
        }
        
        .btn {
            background: transparent;
            border: 1px solid #00ff41;
            color: #00ff41;
            padding: 12px 30px;
            font-family: 'Courier New', Courier, monospace;
            font-size: 1em;
            cursor: pointer;
            transition: all 0.3s;
            text-transform: uppercase;
            letter-spacing: 2px;
        }
        
        .btn:hover {
            background: #00ff41;
            color: #000;
            box-shadow: 0 0 30px rgba(0, 255, 65, 0.3);
        }
        
        .btn:disabled {
            opacity: 0.4;
            cursor: not-allowed;
        }
        
        .btn-danger {
            border-color: #ff0040;
            color: #ff0040;
        }
        
        .btn-danger:hover {
            background: #ff0040;
            color: #000;
            box-shadow: 0 0 30px rgba(255, 0, 64, 0.3);
        }
        
        .btn-telegram {
            border-color: #0088cc;
            color: #0088cc;
        }
        
        .btn-telegram:hover {
            background: #0088cc;
            color: #fff;
        }
        
        .buttons-row {
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            justify-content: center;
            margin-top: 15px;
        }
        
        .buttons-row .btn {
            padding: 10px 20px;
            font-size: 0.9em;
        }
        
        .status-badge {
            display: inline-block;
            padding: 3px 10px;
            border-radius: 3px;
            font-size: 0.8em;
            margin-left: 10px;
        }
        
        .status-sending {
            background: #ffaa00;
            color: #000;
            animation: blink 1s infinite;
        }
        
        .status-idle {
            background: #00aa33;
            color: #000;
        }
        
        @keyframes blink {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.3; }
        }
        
        .terminal {
            margin-top: 20px;
            background: rgba(0, 0, 0, 0.9);
            border: 1px solid #00ff41;
            padding: 15px;
            height: 350px;
            overflow-y: auto;
            box-shadow: inset 0 0 50px rgba(0, 255, 65, 0.05);
        }
        
        .terminal::-webkit-scrollbar {
            width: 6px;
        }
        
        .terminal::-webkit-scrollbar-track {
            background: #000;
            border-left: 1px solid #00ff41;
        }
        
        .terminal::-webkit-scrollbar-thumb {
            background: #00ff41;
        }
        
        .log-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 12px;
        }
        
        .log-table th {
            background: #002200;
            color: #00ff41;
            padding: 8px 6px;
            text-align: left;
            position: sticky;
            top: 0;
            z-index: 10;
            border-bottom: 1px solid #00ff41;
            text-transform: uppercase;
            letter-spacing: 1px;
            font-size: 0.7em;
        }
        
        .log-table td {
            padding: 6px 6px;
            border-bottom: 1px solid #003300;
            font-size: 0.75em;
        }
        
        .log-table tr:hover {
            background: rgba(0, 255, 65, 0.05);
        }
        
        .level-system { color: #00ffff; }
        .level-success { color: #00ff41; }
        .level-error { color: #ff0040; }
        .level-warn { color: #ffaa00; }
        .level-info { color: #888; }
        
        .footer {
            margin-top: 30px;
            padding: 20px 0;
            border-top: 1px solid #003300;
            text-align: center;
            color: #004400;
            font-size: 0.8em;
        }
        
        .anonymous-badge {
            color: #00ff41;
            border: 1px solid #00ff41;
            padding: 2px 12px;
            border-radius: 3px;
            display: inline-block;
            font-size: 0.7em;
            letter-spacing: 1px;
        }
        
        .response-box {
            margin-top: 15px;
            padding: 15px;
            border: 1px solid #003300;
            background: rgba(0, 0, 0, 0.5);
            font-size: 0.85em;
            max-height: 150px;
            overflow-y: auto;
            display: none;
        }
        
        .response-box.active {
            display: block;
        }
        
        .response-box pre {
            color: #00ff41;
            font-family: 'Courier New', Courier, monospace;
            font-size: 0.85em;
            white-space: pre-wrap;
            word-wrap: break-word;
        }
        
        .hint {
            color: #006600;
            font-size: 0.8em;
            text-align: center;
            margin-top: 8px;
        }
        
        @media (max-width: 768px) {
            .header h1 { font-size: 1.5em; letter-spacing: 4px; }
            .input-row { flex-direction: column; }
            .input-row input { width: 100%; }
            .stats-top { grid-template-columns: 1fr; max-width: 300px; }
        }
    </style>
</head>
<body>
    <div class="matrix-bg"></div>
    <div class="scanline"></div>
    
    <div class="container">
        <div class="header">
            <h1 class="glitch">⧩ AMUNDI OTP ROUTER</h1>
            <div class="subtitle">SECURE TERMINAL // 100% ANONYMOUS</div>
            <div style="margin-top: 10px;">
                <span class="anonymous-badge">🔒 NO DATA STORED</span>
                <span id="status_badge" class="status-badge status-idle">● IDLE</span>
            </div>
        </div>
        
        <!-- Stats at top -->
        <div class="stats-top">
            <div class="stat-card success">
                <div class="label">✅ OTP SENT</div>
                <div class="value" id="val_sent">0</div>
            </div>
            <div class="stat-card danger">
                <div class="label">❌ OTP FAILED</div>
                <div class="value" id="val_failed">0</div>
            </div>
            <div class="stat-card info">
                <div class="label">👤 TOTAL NUMBERS</div>
                <div class="value" id="val_numbers">0</div>
            </div>
        </div>
        
        <div class="input-section">
            <div class="input-row">
                <label>📱 PHONE</label>
                <input type="text" id="phoneInput" placeholder="6 7 8 9 X X X X X X" maxlength="10" />
                <button class="btn" id="sendBtn" onclick="sendOTP()">⚡ SEND OTP</button>
                <button class="btn btn-danger" onclick="clearNumber()">✕ CLEAR</button>
            </div>
            <div class="hint">Enter 10-digit number starting with 6, 7, 8, or 9</div>
            
            <div class="buttons-row">
                <button class="btn btn-telegram" onclick="window.open('https://t.me/drdevstudio', '_blank')">
                    📢 JOIN CHANNEL
                </button>
                <button class="btn" onclick="window.open('https://t.me/Hamza3895', '_blank')">
                    💬 CONTACT ME
                </button>
            </div>
            
            <div id="responseBox" class="response-box"></div>
        </div>
        
        <div class="terminal">
            <table class="log-table">
                <thead>
                    <tr>
                        <th style="width:15%;">TIME</th>
                        <th style="width:55%;">EVENT</th>
                        <th style="width:30%;">TARGET</th>
                    </tr>
                </thead>
                <tbody id="logBody">
                    <tr><td colspan="3" style="color:#004400;text-align:center;">⏳ INITIALIZING SECURE TERMINAL...</td></tr>
                </tbody>
            </table>
        </div>
        
        <div class="footer">
            ⚡ SYS.TERMINAL v3.0 // ENCRYPTED CONNECTION // NO LOGS STORED
        </div>
    </div>
    
    <script>
        let isSending = false;
        
        function fetchStats() {
            fetch('/api/stats')
                .then(res => res.json())
                .then(data => {
                    document.getElementById('val_sent').innerText = data.otp_sent;
                    document.getElementById('val_failed').innerText = data.otp_failed;
                    document.getElementById('val_numbers').innerText = data.total_numbers;
                    
                    isSending = data.is_sending;
                    
                    const badge = document.getElementById('status_badge');
                    if (isSending) {
                        badge.className = 'status-badge status-sending';
                        badge.innerText = '● SENDING...';
                        document.getElementById('sendBtn').disabled = true;
                        document.getElementById('sendBtn').innerText = '⏳ SENDING...';
                    } else {
                        badge.className = 'status-badge status-idle';
                        badge.innerText = '● IDLE';
                        document.getElementById('sendBtn').disabled = false;
                        document.getElementById('sendBtn').innerText = '⚡ SEND OTP';
                    }
                    
                    // Update logs
                    const tbody = document.getElementById('logBody');
                    tbody.innerHTML = '';
                    data.logs.forEach(log => {
                        const tr = document.createElement('tr');
                        tr.className = `level-${log.level}`;
                        tr.innerHTML = `
                            <td>${log.time}</td>
                            <td>${log.message}</td>
                            <td>${log.target}</td>
                        `;
                        tbody.appendChild(tr);
                    });
                    
                    // Show last response
                    if (data.last_response) {
                        const box = document.getElementById('responseBox');
                        box.className = 'response-box active';
                        box.innerHTML = `<strong>📡 API Response:</strong><br><pre>${JSON.stringify(data.last_response, null, 2)}</pre>`;
                    }
                })
                .catch(err => console.error('Stats error:', err));
        }
        
        function sendOTP() {
            if (isSending) {
                alert('⏳ OTP is already being sent. Please wait.');
                return;
            }
            
            const phone = document.getElementById('phoneInput').value.trim();
            
            if (!phone) {
                alert('❌ Please enter a phone number.');
                return;
            }
            
            if (!/^[6-9]\\d{9}$/.test(phone)) {
                alert('❌ Invalid number. Must be 10 digits starting with 6, 7, 8, or 9.');
                return;
            }
            
            document.getElementById('sendBtn').disabled = true;
            document.getElementById('sendBtn').innerText = '⏳ SENDING...';
            
            fetch('/api/send_otp', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ phone: phone })
            })
            .then(res => res.json())
            .then(data => {
                if (data.status === 'busy') {
                    alert('⏳ System is busy. Please wait.');
                } else {
                    // Clear input after sending
                    document.getElementById('phoneInput').value = '';
                    const box = document.getElementById('responseBox');
                    box.className = 'response-box active';
                    box.innerHTML = `📡 OTP request sent. Check logs for status.`;
                }
            })
            .catch(err => {
                alert('❌ Error sending OTP request.');
                console.error(err);
            })
            .finally(() => {
                document.getElementById('sendBtn').disabled = false;
                document.getElementById('sendBtn').innerText = '⚡ SEND OTP';
            });
        }
        
        function clearNumber() {
            document.getElementById('phoneInput').value = '';
            document.getElementById('responseBox').className = 'response-box';
            document.getElementById('responseBox').innerHTML = '';
            
            fetch('/api/clear', { method: 'POST' })
                .catch(err => console.error('Clear error:', err));
        }
        
        // Auto-fetch every 1.5 seconds
        setInterval(fetchStats, 1500);
        setTimeout(fetchStats, 500);
        
        // Enter key support
        document.getElementById('phoneInput').addEventListener('keypress', function(e) {
            if (e.key === 'Enter') {
                sendOTP();
            }
        });
        
        // Auto format - only allow digits
        document.getElementById('phoneInput').addEventListener('input', function(e) {
            this.value = this.value.replace(/\\D/g, '').slice(0, 10);
        });
    </script>
</body>
</html>
"""

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
