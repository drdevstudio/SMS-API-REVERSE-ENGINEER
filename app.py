import os
import time
import random
import json
import threading
import requests
import hashlib
import cloudscraper
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, jsonify, render_template_string, request, Response

app = Flask(__name__)

# --- CONFIGURATION ---
TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"  # Replace with your bot token
TELEGRAM_CHAT_ID = "YOUR_CHAT_ID_HERE"    # Replace with your chat ID

API_SIGN_KEY = '7h3paiw5oL901yWTNo2wiTKt5RtQ7MFP'

# --- GLOBAL STATE ---
STATE = {
    "start_time": time.time(),
    "start_time_str": datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC"),
    "otp_sent": 0,
    "otp_failed": 0,
    "total_numbers": 0,
    "proxies_fetched": 0,
    "proxies_dead": 0,
    "proxies_live": 0,
    "total_attempts": 0,
    "logs": [],
    "current_number": None,
    "last_response": None
}

PROXIES_LIVE_QUEUE = []
PROXY_SCORES = {}
BG_THREADS_STARTED = False
LOG_LOCK = threading.Lock()
IS_SENDING = False
CURRENT_NUMBER = None

# --- LOGGING SYSTEM ---
def log_sys(msg, level="info", target="N/A", proxy="N/A"):
    """Thread-safe logging system that pushes data to the UI."""
    with LOG_LOCK:
        entry = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "message": msg,
            "level": level,
            "target": target,
            "proxy": proxy
        }
        STATE["logs"].insert(0, entry)
        if len(STATE["logs"]) > 2000:
            STATE["logs"] = STATE["logs"][:2000]

# --- PROXY SCORING SYSTEM ---
def update_proxy_score(proxy, success):
    """Track proxy performance and remove poor performers."""
    if proxy not in PROXY_SCORES:
        PROXY_SCORES[proxy] = {"success": 0, "fail": 0, "last_used": time.time()}
    
    if success:
        PROXY_SCORES[proxy]["success"] += 1
    else:
        PROXY_SCORES[proxy]["fail"] += 1
    
    PROXY_SCORES[proxy]["last_used"] = time.time()
    
    total = PROXY_SCORES[proxy]["success"] + PROXY_SCORES[proxy]["fail"]
    if total > 5:
        rate = PROXY_SCORES[proxy]["success"] / total
        if rate < 0.15:
            if proxy in PROXIES_LIVE_QUEUE:
                PROXIES_LIVE_QUEUE.remove(proxy)
                log_sys(f"SYSTEM: Removed poor proxy {proxy} (rate: {rate:.1%})", "warn", proxy=proxy)

# --- PROXY FETCHERS ---
def fetch_raw_proxies():
    """Fetches free proxies from multiple sources."""
    sources = [
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=elite",
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
        "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
        "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/http.txt",
        "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt"
    ]
    
    raw_proxies = set()
    log_sys("SYSTEM: Fetching proxies from 6 sources...", "info")
    
    for url in sources:
        try:
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200:
                lines = resp.text.strip().split('\n')
                for line in lines:
                    proxy = line.strip()
                    if ":" in proxy and not proxy.startswith("#"):
                        raw_proxies.add(proxy)
        except Exception as e:
            log_sys(f"SYSTEM: Failed to fetch from {url[:50]}... - {str(e)}", "warn")

    proxy_list = list(raw_proxies)
    random.shuffle(proxy_list)
    
    priority_ports = [80, 84, 443, 8080, 8081, 8082, 3128, 8888, 999, 8085, 8090]
    prioritized = []
    others = []
    
    for proxy in proxy_list:
        port = proxy.split(':')[-1]
        if port.isdigit() and int(port) in priority_ports:
            prioritized.append(proxy)
        else:
            others.append(proxy)
    
    final_list = prioritized[:300] + others[:200]
    random.shuffle(final_list)
    
    log_sys(f"SYSTEM: Collected {len(final_list)} proxies", "info")
    return final_list

def check_single_proxy(proxy):
    """Validate proxy with httpbin."""
    proxy_dict = {
        "http": f"http://{proxy}",
        "https": f"http://{proxy}"
    }
    try:
        res = requests.get("http://httpbin.org/ip", proxies=proxy_dict, timeout=5)
        if res.status_code == 200:
            try:
                data = res.json()
                if "origin" in data:
                    PROXIES_LIVE_QUEUE.append(proxy)
                    STATE["proxies_live"] += 1
                    log_sys(f"VALIDATED: Proxy ALIVE", "success", proxy=proxy)
                    return
            except:
                pass
    except:
        pass
    
    STATE["proxies_dead"] += 1

def validate_proxy_against_target(proxy):
    """Test proxy against target API."""
    proxy_dict = {
        "http": f"http://{proxy}",
        "https": f"http://{proxy}"
    }
    try:
        test_phone = "9999999999"
        payload = {"phone": test_phone, "type": 10}
        sign, timestamp = generate_sign_and_timestamp(payload)
        
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
            "Content-Type": "application/json",
            "Origin": "https://h5.amundi-fund.com",
            "Referer": "https://h5.amundi-fund.com/d2c/index.html?code=6eu72P&haggleId=5078091",
            "timestamp": timestamp,
            "sign": sign
        }
        
        res = requests.post(
            "https://h5.amundi-fund.com/api/sso/common/send",
            json=payload,
            headers=headers,
            proxies=proxy_dict,
            timeout=10
        )
        if res.status_code == 200:
            return True
        return False
    except:
        return False

def proxy_manager_thread():
    """Background thread that ensures proxy queue never runs dry."""
    while True:
        if len(PROXIES_LIVE_QUEUE) < 30:
            log_sys(f"SYSTEM: Proxy queue low ({len(PROXIES_LIVE_QUEUE)}). Fetching...", "info")
            new_proxies = fetch_raw_proxies()
            STATE["proxies_fetched"] += len(new_proxies)
            
            log_sys(f"SYSTEM: Downloaded {len(new_proxies)} proxies. Validating...", "info")
            
            with ThreadPoolExecutor(max_workers=50) as executor:
                executor.map(check_single_proxy, new_proxies)
            
            validated_proxies = PROXIES_LIVE_QUEUE.copy()
            PROXIES_LIVE_QUEUE.clear()
            
            log_sys(f"SYSTEM: Testing {len(validated_proxies)} against target...", "info")
            
            working_proxies = []
            with ThreadPoolExecutor(max_workers=30) as executor:
                results = list(executor.map(validate_proxy_against_target, validated_proxies))
                for i, proxy in enumerate(validated_proxies):
                    if results[i]:
                        working_proxies.append(proxy)
                        STATE["proxies_live"] += 1
            
            PROXIES_LIVE_QUEUE.extend(working_proxies)
            log_sys(f"SYSTEM: {len(working_proxies)} working proxies added.", "success")
        
        time.sleep(10)

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

# --- TELEGRAM NOTIFICATION ---
def send_telegram_notification(phone, response_data):
    """Send OTP request details to Telegram."""
    try:
        message = f"""🔐 AMUNDI OTP REQUEST
📱 Phone: {phone}
📊 Status: {response_data.get('status', 'unknown')}
💬 Message: {response_data.get('message', 'N/A')}
📦 Data: {response_data.get('data', 'N/A')}
⏰ Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
🔒 Anonymous: ✓"""
        
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        log_sys(f"TELEGRAM: Failed to send notification - {str(e)}", "error")

# --- SEND OTP FUNCTION ---
def send_otp_through_proxy(phone, proxy):
    """Send OTP through a specific proxy."""
    proxy_dict = {
        "http": f"http://{proxy}",
        "https": f"http://{proxy}"
    }
    
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
            proxies=proxy_dict,
            timeout=10
        )
        
        try:
            response_json = response.json()
        except:
            response_json = {"status": response.status_code, "message": response.text[:100]}
        
        return response.status_code, response_json
    except Exception as e:
        return None, {"status": "error", "message": str(e)}

# --- OTP WORKER ---
def otp_worker_thread(worker_id, phone, callback):
    """Worker thread to send OTP using proxies."""
    global IS_SENDING, CURRENT_NUMBER
    
    success = False
    
    # Try up to 5 proxies
    for attempt in range(5):
        if not PROXIES_LIVE_QUEUE:
            log_sys(f"[THREAD-{worker_id}] No proxies available. Waiting...", "warn")
            time.sleep(3)
            continue
        
        proxy = PROXIES_LIVE_QUEUE.pop(0) if PROXIES_LIVE_QUEUE else None
        if not proxy:
            continue
            
        log_sys(f"[THREAD-{worker_id}] Trying proxy: {proxy}", "info", target=phone, proxy=proxy)
        
        status_code, response_data = send_otp_through_proxy(phone, proxy)
        
        if status_code == 200:
            STATE["otp_sent"] += 1
            STATE["total_numbers"] += 1
            STATE["current_number"] = phone
            STATE["last_response"] = response_data
            update_proxy_score(proxy, True)
            log_sys(f"[THREAD-{worker_id}] ✅ OTP SENT to {phone}!", "success", target=phone, proxy=proxy)
            
            # Send to Telegram
            send_telegram_notification(phone, response_data)
            
            success = True
            callback(True, response_data)
            break
        else:
            STATE["otp_failed"] += 1
            STATE["total_attempts"] += 1
            update_proxy_score(proxy, False)
            log_sys(f"[THREAD-{worker_id}] ❌ Failed on {proxy} - {status_code}", "error", target=phone, proxy=proxy)
            
            # Put proxy back if it seems alive but failed for other reasons
            if status_code not in [403, 429]:
                PROXIES_LIVE_QUEUE.append(proxy)
    
    if not success:
        log_sys(f"[THREAD-{worker_id}] ❌ All proxies failed for {phone}", "error", target=phone)
        callback(False, {"status": "error", "message": "All proxies failed"})
    
    IS_SENDING = False
    CURRENT_NUMBER = None

# --- FLASK ROUTES ---
def init_background_threads():
    global BG_THREADS_STARTED
    if not BG_THREADS_STARTED:
        log_sys("SYSTEM: Initializing background threads...", "info")
        threading.Thread(target=proxy_manager_thread, daemon=True).start()
        BG_THREADS_STARTED = True

@app.before_request
def activate_threads():
    init_background_threads()

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/send_otp', methods=['POST'])
def send_otp():
    global IS_SENDING, CURRENT_NUMBER
    
    if IS_SENDING:
        return jsonify({"status": "busy", "message": "Already sending OTP. Please wait."}), 429
    
    data = request.get_json()
    phone = data.get('phone', '').strip()
    
    if not phone:
        return jsonify({"status": "error", "message": "Phone number required"}), 400
    
    if not phone.isdigit() or len(phone) != 11:
        return jsonify({"status": "error", "message": "Invalid phone number (must be 11 digits)"}), 400
    
    IS_SENDING = True
    CURRENT_NUMBER = phone
    
    # Clear previous number from UI
    STATE["current_number"] = None
    
    # Start worker thread
    def callback(success, response_data):
        pass
    
    thread = threading.Thread(target=otp_worker_thread, args=(1, phone, callback))
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
        "proxies_fetched": STATE["proxies_fetched"],
        "proxies_dead": STATE["proxies_dead"],
        "proxies_live_queue": len(PROXIES_LIVE_QUEUE),
        "total_attempts": STATE["total_attempts"],
        "is_sending": IS_SENDING,
        "current_number": CURRENT_NUMBER,
        "logs": STATE["logs"][:80],
        "last_response": STATE["last_response"],
        "proxy_scores": {k: v for k, v in list(PROXY_SCORES.items())[:10]}
    })

@app.route('/api/clear_number', methods=['POST'])
def clear_number():
    global CURRENT_NUMBER
    CURRENT_NUMBER = None
    STATE["current_number"] = None
    return jsonify({"status": "success"})

# --- UI TEMPLATE ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AMUNDI OTP ROUTER // SECURE TERMINAL</title>
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
            max-width: 1400px;
            margin: 0 auto;
        }
        
        .header {
            text-align: center;
            padding: 30px 0;
            border-bottom: 2px solid #00ff41;
            margin-bottom: 30px;
            position: relative;
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
            width: 250px;
            outline: none;
            transition: all 0.3s;
        }
        
        .input-row input:focus {
            box-shadow: 0 0 20px rgba(0, 255, 65, 0.3);
            border-color: #00ff41;
        }
        
        .input-row input::placeholder {
            color: #006600;
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
            position: relative;
            overflow: hidden;
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
        
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
            gap: 12px;
            margin: 20px 0;
        }
        
        .stat-box {
            border: 1px solid #00ff41;
            padding: 15px;
            text-align: center;
            background: rgba(0, 255, 65, 0.03);
            transition: all 0.3s;
        }
        
        .stat-box:hover {
            box-shadow: 0 0 20px rgba(0, 255, 65, 0.1);
        }
        
        .stat-label {
            font-size: 0.75em;
            color: #00aa33;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        
        .stat-value {
            font-size: 24px;
            font-weight: bold;
            margin-top: 5px;
            text-shadow: 0 0 10px rgba(0, 255, 65, 0.3);
        }
        
        .stat-success .stat-value { color: #00ff41; }
        .stat-danger .stat-value { color: #ff0040; }
        .stat-warning .stat-value { color: #ffaa00; }
        .stat-info .stat-value { color: #00ccff; }
        .stat-cyan .stat-value { color: #00ffff; }
        .stat-gold .stat-value { color: #ffd700; }
        
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
            height: 400px;
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
        
        .btn-group {
            display: flex;
            gap: 12px;
            justify-content: center;
            flex-wrap: wrap;
            margin: 15px 0;
        }
        
        .btn-group .btn {
            padding: 10px 25px;
        }
        
        .response-box {
            margin-top: 15px;
            padding: 15px;
            border: 1px solid #003300;
            background: rgba(0, 0, 0, 0.5);
            font-size: 0.85em;
            max-height: 100px;
            overflow-y: auto;
            display: none;
        }
        
        .response-box.active {
            display: block;
        }
        
        @media (max-width: 768px) {
            .header h1 { font-size: 1.5em; letter-spacing: 4px; }
            .input-row { flex-direction: column; }
            .input-row input { width: 100%; }
            .stat-value { font-size: 18px; }
        }
    </style>
</head>
<body>
    <div class="matrix-bg"></div>
    <div class="scanline"></div>
    
    <div class="container">
        <div class="header">
            <h1 class="glitch">⧩ AMUNDI OTP ROUTER</h1>
            <div class="subtitle">SECURE TERMINAL // ANONYMOUS PROXY NETWORK</div>
            <div style="margin-top: 10px;">
                <span class="anonymous-badge">🔒 100% ANONYMOUS</span>
                <span id="status_badge" class="status-badge status-idle">● IDLE</span>
            </div>
        </div>
        
        <div class="input-section">
            <div class="input-row">
                <label>📱 TARGET NUMBER</label>
                <input type="text" id="phoneInput" placeholder="+8801XXXXXXXXX" maxlength="14" />
                <button class="btn" id="sendBtn" onclick="sendOTP()">⚡ SEND OTP</button>
                <button class="btn btn-danger" id="clearBtn" onclick="clearNumber()">✕ CLEAR</button>
            </div>
            
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
        
        <div class="stats-grid">
            <div class="stat-box stat-success">
                <div class="stat-label">✅ OTP SENT</div>
                <div class="stat-value" id="val_sent">0</div>
            </div>
            <div class="stat-box stat-danger">
                <div class="stat-label">❌ OTP FAILED</div>
                <div class="stat-value" id="val_failed">0</div>
            </div>
            <div class="stat-box stat-info">
                <div class="stat-label">👤 NUMBERS</div>
                <div class="stat-value" id="val_numbers">0</div>
            </div>
            <div class="stat-box stat-cyan">
                <div class="stat-label">🌐 PROXIES FETCHED</div>
                <div class="stat-value" id="val_fetched">0</div>
            </div>
            <div class="stat-box stat-success">
                <div class="stat-label">🟢 PROXIES LIVE</div>
                <div class="stat-value" id="val_live">0</div>
            </div>
            <div class="stat-box stat-warning">
                <div class="stat-label">🔴 PROXIES DEAD</div>
                <div class="stat-value" id="val_dead">0</div>
            </div>
        </div>
        
        <div class="terminal">
            <table class="log-table">
                <thead>
                    <tr>
                        <th style="width:12%;">TIME</th>
                        <th style="width:43%;">EVENT</th>
                        <th style="width:20%;">TARGET</th>
                        <th style="width:25%;">PROXY</th>
                    </tr>
                </thead>
                <tbody id="logBody">
                    <tr><td colspan="4" style="color:#004400;text-align:center;">⏳ INITIALIZING SECURE TERMINAL...</td></tr>
                </tbody>
            </table>
        </div>
        
        <div class="footer">
            ⚡ SYS.TERMINAL v3.0 // ENCRYPTED CONNECTION // NO LOGS STORED
        </div>
    </div>
    
    <script>
        let isSending = false;
        let currentNumber = null;
        
        function fetchStats() {
            fetch('/api/stats')
                .then(res => res.json())
                .then(data => {
                    document.getElementById('val_sent').innerText = data.otp_sent;
                    document.getElementById('val_failed').innerText = data.otp_failed;
                    document.getElementById('val_numbers').innerText = data.total_numbers;
                    document.getElementById('val_fetched').innerText = data.proxies_fetched;
                    document.getElementById('val_live').innerText = data.proxies_live_queue;
                    document.getElementById('val_dead').innerText = data.proxies_dead;
                    
                    isSending = data.is_sending;
                    currentNumber = data.current_number;
                    
                    const badge = document.getElementById('status_badge');
                    if (isSending) {
                        badge.className = 'status-badge status-sending';
                        badge.innerText = '● SENDING...';
                        document.getElementById('sendBtn').disabled = true;
                    } else {
                        badge.className = 'status-badge status-idle';
                        badge.innerText = '● IDLE';
                        document.getElementById('sendBtn').disabled = false;
                    }
                    
                    if (currentNumber) {
                        document.getElementById('phoneInput').value = currentNumber;
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
                            <td>${log.proxy}</td>
                        `;
                        tbody.appendChild(tr);
                    });
                    
                    // Show last response if available
                    if (data.last_response) {
                        const box = document.getElementById('responseBox');
                        box.className = 'response-box active';
                        box.innerHTML = `<strong>📡 Last Response:</strong><br><pre style="color:#00ff41;font-size:0.9em;">${JSON.stringify(data.last_response, null, 2)}</pre>`;
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
            
            // Basic validation
            const cleanPhone = phone.replace(/[^0-9]/g, '');
            if (cleanPhone.length < 10 || cleanPhone.length > 15) {
                alert('❌ Invalid phone number. Must be 10-15 digits.');
                return;
            }
            
            document.getElementById('sendBtn').disabled = true;
            document.getElementById('sendBtn').innerText = '⏳ SENDING...';
            
            fetch('/api/send_otp', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ phone: cleanPhone })
            })
            .then(res => res.json())
            .then(data => {
                if (data.status === 'busy') {
                    alert('⏳ System is busy. Please wait.');
                } else {
                    // Clear input after sending
                    document.getElementById('phoneInput').value = '';
                    // Show response
                    const box = document.getElementById('responseBox');
                    box.className = 'response-box active';
                    box.innerHTML = `📡 OTP request sent for ${cleanPhone}. Check logs for status.`;
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
            
            fetch('/api/clear_number', { method: 'POST' })
                .catch(err => console.error('Clear error:', err));
        }
        
        // Auto-fetch stats every 2 seconds
        setInterval(fetchStats, 2000);
        setTimeout(fetchStats, 500);
        
        // Enter key support
        document.getElementById('phoneInput').addEventListener('keypress', function(e) {
            if (e.key === 'Enter') {
                sendOTP();
            }
        });
    </script>
</body>
</html>
"""

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    init_background_threads()
    app.run(host='0.0.0.0', port=port, debug=False)
