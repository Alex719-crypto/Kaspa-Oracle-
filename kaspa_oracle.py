"""
╔══════════════════════════════════════════════════════════════╗
║     🍏 KASPA WHALE ORACLE v4.0 — For Whop                   ║
║     5D Apple Visualization + MEXC + Whale Tracking          ║
╠══════════════════════════════════════════════════════════════╣
║  Features:                                                   ║
║    • Real-time KAS price from MEXC                          ║
║    • Whale detection from Kaspa blockchain                  ║
║    • 5-minute price predictions                             ║
║    • 5D Apple signal visualization 🍏🍎                      ║
╚══════════════════════════════════════════════════════════════╝
"""

import asyncio
import time
import threading
import json
from datetime import datetime, timezone
from uuid import uuid4
from collections import deque

import aiohttp
from flask import Flask, Response, jsonify

# ═══════════════════════════════════════════════════════════════
# ⚙️ CONFIGURATION
# ═══════════════════════════════════════════════════════════════

# MEXC API (no auth needed for public data)
MEXC_BASE = "https://api.mexc.com/api/v3"
KAS_SYMBOL = "KASUSDT"

# Kaspa Blockchain API
KASPA_API = "https://api.kaspa.org"

# Whale thresholds
KAS_WHALE_THRESHOLD = 500_000      # 500K KAS = whale
SOMPI_PER_KAS = 100_000_000
WHALE_THRESHOLD_SOMPI = KAS_WHALE_THRESHOLD * SOMPI_PER_KAS
MEGA_WHALE = 5_000_000             # 5M KAS
BIG_WHALE = 1_000_000              # 1M KAS

# Scan interval
SCAN_INTERVAL = 30  # seconds

# ═══════════════════════════════════════════════════════════════
# 📊 DATA STORAGE
# ═══════════════════════════════════════════════════════════════

MAX_HISTORY = 100
price_history = deque(maxlen=MAX_HISTORY)
whale_history = deque(maxlen=50)
predictions_log = {}
accuracy_stats = {"total": 0, "correct": 0, "pct": 0.0}

# Current state
current_state = {
    "price": 0.0,
    "price_change_24h": 0.0,
    "volume_24h": 0.0,
    "whale_alert": None,
    "prediction": None,
    "signal_5d": "🍏🍏🍎🍎🍎",  # neutral start
    "signal_score": 0,
    "last_update": None,
}

# ═══════════════════════════════════════════════════════════════
# 🍏 5D APPLE SIGNAL CALCULATOR
# ═══════════════════════════════════════════════════════════════

def calculate_5d_signal(whale_data, price_momentum, prediction_conf):
    """
    Calculate 5D Apple Signal
    
    Score from -5 (strong sell) to +5 (strong buy)
    
    Factors:
    - Whale activity (buy/sell pressure)
    - Price momentum
    - Prediction confidence
    """
    score = 0
    
    # Factor 1: Whale activity
    if whale_data:
        whale_kas = whale_data.get("kas_amount", 0)
        if whale_kas >= MEGA_WHALE:
            score += 2  # Mega whale = strong signal
        elif whale_kas >= BIG_WHALE:
            score += 1.5
        elif whale_kas >= KAS_WHALE_THRESHOLD:
            score += 1
    
    # Factor 2: Price momentum (last few candles)
    if price_momentum > 1.0:
        score += 1.5
    elif price_momentum > 0.3:
        score += 1
    elif price_momentum < -1.0:
        score -= 1.5
    elif price_momentum < -0.3:
        score -= 1
    
    # Factor 3: Prediction confidence
    if prediction_conf:
        if prediction_conf.get("direction") == "UP":
            score += prediction_conf.get("confidence", 0) / 50  # max +2
        elif prediction_conf.get("direction") == "DOWN":
            score -= prediction_conf.get("confidence", 0) / 50
    
    # Clamp to -5 to +5
    score = max(-5, min(5, score))
    
    # Convert to 5D apples
    # Score: -5 to +5 → 0 to 5 green apples
    green_count = int(round((score + 5) / 2))  # 0-5 green apples
    green_count = max(0, min(5, green_count))
    red_count = 5 - green_count
    
    apples = "🍏" * green_count + "🍎" * red_count
    
    return {
        "apples": apples,
        "score": round(score, 2),
        "green": green_count,
        "red": red_count,
        "signal": "STRONG BUY" if green_count >= 4 else "BUY" if green_count == 3 else "NEUTRAL" if green_count == 2 else "SELL" if green_count == 1 else "STRONG SELL"
    }

# ═══════════════════════════════════════════════════════════════
# 💰 MEXC PRICE FETCHER
# ═══════════════════════════════════════════════════════════════

async def fetch_mexc_price(session):
    """Fetch KAS/USDT price from MEXC"""
    try:
        # Get ticker
        url = f"{MEXC_BASE}/ticker/24hr?symbol={KAS_SYMBOL}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                data = await r.json()
                return {
                    "price": float(data.get("lastPrice", 0)),
                    "change_24h": float(data.get("priceChangePercent", 0)),
                    "high_24h": float(data.get("highPrice", 0)),
                    "low_24h": float(data.get("lowPrice", 0)),
                    "volume_24h": float(data.get("volume", 0)),
                    "quote_volume": float(data.get("quoteVolume", 0)),
                }
    except Exception as e:
        print(f"MEXC Error: {e}")
    return None


async def fetch_mexc_klines(session, interval="1m", limit=30):
    """Fetch recent candles from MEXC"""
    try:
        url = f"{MEXC_BASE}/klines?symbol={KAS_SYMBOL}&interval={interval}&limit={limit}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                data = await r.json()
                candles = []
                for k in data:
                    candles.append({
                        "time": k[0],
                        "open": float(k[1]),
                        "high": float(k[2]),
                        "low": float(k[3]),
                        "close": float(k[4]),
                        "volume": float(k[5]),
                    })
                return candles
    except Exception as e:
        print(f"MEXC Klines Error: {e}")
    return []

# ═══════════════════════════════════════════════════════════════
# 🐋 KASPA WHALE TRACKER
# ═══════════════════════════════════════════════════════════════

async def fetch_kaspa_whales(session):
    """Fetch whale transactions from Kaspa blockchain"""
    try:
        url = f"{KASPA_API}/blocks?includeTransactions=true&limit=20"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status == 200:
                data = await r.json()
                blocks = data if isinstance(data, list) else data.get("blocks", [])
                
                whale_txs = []
                total_whale_kas = 0
                max_kas = 0
                
                for block in blocks:
                    for tx in block.get("transactions", []):
                        tx_id = tx.get("transactionId", "")[:16]
                        for out in tx.get("outputs", []):
                            try:
                                sompi = int(out.get("amount", 0))
                                kas_val = sompi / SOMPI_PER_KAS
                                
                                if sompi >= WHALE_THRESHOLD_SOMPI:
                                    whale_txs.append({
                                        "tx_id": tx_id,
                                        "amount": kas_val,
                                        "address": out.get("scriptPublicKeyAddress", "")[:20] + "..."
                                    })
                                    total_whale_kas += kas_val
                                    max_kas = max(max_kas, kas_val)
                            except (TypeError, ValueError):
                                continue
                
                if whale_txs:
                    tier = "MEGA" if max_kas >= MEGA_WHALE else "BIG" if max_kas >= BIG_WHALE else "WHALE"
                    return {
                        "detected": True,
                        "count": len(whale_txs),
                        "kas_amount": round(max_kas, 2),
                        "total_volume": round(total_whale_kas, 2),
                        "tier": tier,
                        "transactions": whale_txs[:5],
                        "timestamp": datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
                    }
                
                return {"detected": False, "count": 0, "kas_amount": 0, "tier": "NONE"}
                
    except Exception as e:
        print(f"Kaspa API Error: {e}")
    return {"detected": False, "count": 0, "kas_amount": 0, "tier": "NONE", "error": str(e)}

# ═══════════════════════════════════════════════════════════════
# 🔮 PREDICTION ENGINE
# ═══════════════════════════════════════════════════════════════

def calculate_momentum(candles):
    """Calculate price momentum from candles"""
    if len(candles) < 3:
        return 0.0
    
    recent = candles[-5:]
    first_close = recent[0]["close"]
    last_close = recent[-1]["close"]
    
    if first_close > 0:
        return round(((last_close - first_close) / first_close) * 100, 3)
    return 0.0


def predict_price(current_price, whale_data, momentum, candles):
    """Generate 5-minute price prediction"""
    
    if not current_price or current_price <= 0:
        return {
            "direction": "FLAT",
            "change_pct": 0.0,
            "confidence": 0,
            "target": current_price,
            "reasoning": "No price data"
        }
    
    reasons = []
    base_pct = 0.0
    confidence = 40
    
    # Factor 1: Whale activity
    if whale_data and whale_data.get("detected"):
        tier = whale_data.get("tier", "WHALE")
        kas_amount = whale_data.get("kas_amount", 0)
        
        if tier == "MEGA":
            base_pct += 1.5
            confidence += 25
            reasons.append(f"🐋 MEGA whale {kas_amount:,.0f} KAS")
        elif tier == "BIG":
            base_pct += 0.8
            confidence += 15
            reasons.append(f"🐋 BIG whale {kas_amount:,.0f} KAS")
        else:
            base_pct += 0.4
            confidence += 8
            reasons.append(f"🐋 Whale {kas_amount:,.0f} KAS")
    
    # Factor 2: Momentum
    if momentum > 0.5:
        base_pct += momentum * 0.3
        confidence += 10
        reasons.append(f"📈 Momentum +{momentum:.2f}%")
    elif momentum < -0.5:
        base_pct += momentum * 0.3  # negative
        confidence += 5
        reasons.append(f"📉 Momentum {momentum:.2f}%")
    
    # Factor 3: Volume analysis from candles
    if candles and len(candles) >= 5:
        recent_vol = sum(c["volume"] for c in candles[-3:]) / 3
        older_vol = sum(c["volume"] for c in candles[-6:-3]) / 3 if len(candles) >= 6 else recent_vol
        
        if older_vol > 0:
            vol_change = (recent_vol - older_vol) / older_vol
            if vol_change > 0.5:
                base_pct *= 1.2
                confidence += 8
                reasons.append("📊 Volume surge")
    
    # Determine direction
    direction = "UP" if base_pct > 0.1 else "DOWN" if base_pct < -0.1 else "FLAT"
    
    # Clamp values
    base_pct = round(max(-5, min(5, base_pct)), 2)
    confidence = max(20, min(90, confidence))
    target = round(current_price * (1 + base_pct / 100), 6)
    
    return {
        "direction": direction,
        "change_pct": base_pct,
        "confidence": confidence,
        "target": target,
        "reasoning": " | ".join(reasons) if reasons else "Low activity"
    }

# ═══════════════════════════════════════════════════════════════
# 🔄 MAIN SCANNER
# ═══════════════════════════════════════════════════════════════

async def run_scan():
    """Main scan function - runs every SCAN_INTERVAL seconds"""
    global current_state
    
    async with aiohttp.ClientSession() as session:
        # Fetch all data concurrently
        results = await asyncio.gather(
            fetch_mexc_price(session),
            fetch_mexc_klines(session),
            fetch_kaspa_whales(session),
            return_exceptions=True
        )
    
    mexc_data = results[0] if not isinstance(results[0], Exception) else None
    candles = results[1] if not isinstance(results[1], Exception) else []
    whale_data = results[2] if not isinstance(results[2], Exception) else {"detected": False}
    
    # Update price
    price = mexc_data["price"] if mexc_data else current_state["price"]
    
    # Calculate momentum
    momentum = calculate_momentum(candles)
    
    # Generate prediction
    prediction = predict_price(price, whale_data, momentum, candles)
    
    # Calculate 5D signal
    signal_5d = calculate_5d_signal(whale_data if whale_data.get("detected") else None, momentum, prediction)
    
    # Record price history
    price_history.append({
        "ts": time.time(),
        "price": price,
        "momentum": momentum,
        "whale": whale_data.get("detected", False)
    })
    
    # Record whale if detected
    if whale_data.get("detected"):
        whale_history.append({
            "ts": time.time(),
            "data": whale_data
        })
    
    # Update current state
    current_state = {
        "price": price,
        "price_change_24h": mexc_data.get("change_24h", 0) if mexc_data else 0,
        "volume_24h": mexc_data.get("volume_24h", 0) if mexc_data else 0,
        "high_24h": mexc_data.get("high_24h", 0) if mexc_data else 0,
        "low_24h": mexc_data.get("low_24h", 0) if mexc_data else 0,
        "whale_alert": whale_data if whale_data.get("detected") else None,
        "prediction": prediction,
        "signal_5d": signal_5d,
        "momentum": momentum,
        "candles": candles[-30:],  # last 30 candles for chart
        "last_update": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }
    
    # Log
    whale_str = f"🐋 {whale_data['tier']} {whale_data['kas_amount']:,.0f} KAS" if whale_data.get("detected") else "🌊 No whales"
    print(f"📊 KAS ${price:.5f} | {signal_5d['apples']} | {whale_str} | 🔮 {prediction['direction']} {prediction['change_pct']:+.2f}%")
    
    return current_state


async def scanner_loop():
    """Background scanner loop"""
    while True:
        try:
            await run_scan()
        except Exception as e:
            print(f"❌ Scanner error: {e}")
        await asyncio.sleep(SCAN_INTERVAL)

# ═══════════════════════════════════════════════════════════════
# 🌐 FLASK WEB APP
# ═══════════════════════════════════════════════════════════════

app = Flask(__name__)

DASHBOARD_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🍏 Kaspa Whale Oracle</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=Inter:wght@400;500;600&display=swap');
        
        * { box-sizing: border-box; margin: 0; padding: 0; }
        
        body {
            background: linear-gradient(135deg, #0a0a0f 0%, #1a1a2e 100%);
            min-height: 100vh;
            color: #fff;
            font-family: 'Inter', sans-serif;
        }
        
        .container {
            max-width: 1400px;
            margin: 0 auto;
            padding: 20px;
        }
        
        /* Header */
        .header {
            text-align: center;
            padding: 30px 0;
            border-bottom: 1px solid rgba(34, 197, 94, 0.2);
            margin-bottom: 30px;
        }
        
        .header h1 {
            font-family: 'Orbitron', sans-serif;
            font-size: 36px;
            font-weight: 900;
            background: linear-gradient(135deg, #22c55e, #3b82f6);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 10px;
        }
        
        .header .subtitle {
            color: #6b7280;
            font-size: 14px;
        }
        
        /* 5D Signal Box */
        .signal-box {
            background: linear-gradient(135deg, #111827, #1f2937);
            border: 2px solid rgba(34, 197, 94, 0.3);
            border-radius: 20px;
            padding: 30px;
            text-align: center;
            margin-bottom: 30px;
            box-shadow: 0 0 40px rgba(34, 197, 94, 0.1);
        }
        
        .signal-box h2 {
            font-family: 'Orbitron', sans-serif;
            color: #9ca3af;
            font-size: 14px;
            text-transform: uppercase;
            letter-spacing: 3px;
            margin-bottom: 15px;
        }
        
        .apples {
            font-size: 64px;
            letter-spacing: 10px;
            margin: 20px 0;
            filter: drop-shadow(0 0 20px rgba(34, 197, 94, 0.5));
        }
        
        .signal-label {
            font-family: 'Orbitron', sans-serif;
            font-size: 24px;
            font-weight: 700;
            margin-top: 15px;
        }
        
        .signal-label.buy { color: #22c55e; }
        .signal-label.sell { color: #ef4444; }
        .signal-label.neutral { color: #f59e0b; }
        
        /* Stats Grid */
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        
        .stat-card {
            background: rgba(17, 24, 39, 0.8);
            border: 1px solid rgba(75, 85, 99, 0.3);
            border-radius: 16px;
            padding: 24px;
            transition: all 0.3s;
        }
        
        .stat-card:hover {
            border-color: rgba(34, 197, 94, 0.5);
            transform: translateY(-2px);
        }
        
        .stat-card .label {
            color: #6b7280;
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 8px;
        }
        
        .stat-card .value {
            font-family: 'Orbitron', sans-serif;
            font-size: 28px;
            font-weight: 700;
            color: #fff;
        }
        
        .stat-card .value.green { color: #22c55e; }
        .stat-card .value.red { color: #ef4444; }
        .stat-card .value.yellow { color: #f59e0b; }
        
        .stat-card .sub {
            color: #4b5563;
            font-size: 12px;
            margin-top: 5px;
        }
        
        /* Whale Alert */
        .whale-alert {
            background: linear-gradient(135deg, #1e3a5f, #111827);
            border: 2px solid #3b82f6;
            border-radius: 16px;
            padding: 24px;
            margin-bottom: 30px;
            display: none;
        }
        
        .whale-alert.active {
            display: block;
            animation: pulse-border 2s infinite;
        }
        
        @keyframes pulse-border {
            0%, 100% { border-color: #3b82f6; box-shadow: 0 0 20px rgba(59, 130, 246, 0.3); }
            50% { border-color: #22c55e; box-shadow: 0 0 30px rgba(34, 197, 94, 0.5); }
        }
        
        .whale-alert h3 {
            font-family: 'Orbitron', sans-serif;
            font-size: 18px;
            color: #22c55e;
            margin-bottom: 15px;
        }
        
        .whale-details {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 15px;
        }
        
        /* Prediction Box */
        .prediction-box {
            background: linear-gradient(135deg, #064e3b, #111827);
            border: 1px solid rgba(34, 197, 94, 0.3);
            border-radius: 16px;
            padding: 24px;
            margin-bottom: 30px;
        }
        
        .prediction-box h3 {
            font-family: 'Orbitron', sans-serif;
            color: #9ca3af;
            font-size: 14px;
            margin-bottom: 15px;
        }
        
        .prediction-direction {
            font-family: 'Orbitron', sans-serif;
            font-size: 36px;
            font-weight: 900;
        }
        
        .prediction-direction.up { color: #22c55e; }
        .prediction-direction.down { color: #ef4444; }
        .prediction-direction.flat { color: #f59e0b; }
        
        /* Chart Container */
        .chart-container {
            background: rgba(17, 24, 39, 0.8);
            border: 1px solid rgba(75, 85, 99, 0.3);
            border-radius: 16px;
            padding: 20px;
            margin-bottom: 30px;
        }
        
        .chart-container h3 {
            font-family: 'Orbitron', sans-serif;
            color: #9ca3af;
            font-size: 14px;
            margin-bottom: 15px;
        }
        
        #price-chart {
            width: 100%;
            height: 300px;
            background: #0a0a0f;
            border-radius: 8px;
        }
        
        /* Footer */
        .footer {
            text-align: center;
            padding: 20px;
            color: #374151;
            font-size: 12px;
            border-top: 1px solid rgba(75, 85, 99, 0.2);
            margin-top: 30px;
        }
        
        .live-dot {
            display: inline-block;
            width: 8px;
            height: 8px;
            background: #22c55e;
            border-radius: 50%;
            margin-right: 5px;
            animation: blink 1s infinite;
        }
        
        @keyframes blink {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.3; }
        }
        
        /* Responsive */
        @media (max-width: 768px) {
            .header h1 { font-size: 24px; }
            .apples { font-size: 48px; }
            .stat-card .value { font-size: 22px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <header class="header">
            <h1>🍏 KASPA WHALE ORACLE</h1>
            <p class="subtitle">Real-time whale tracking • 5D Signal • Price predictions</p>
        </header>
        
        <!-- 5D Signal -->
        <div class="signal-box">
            <h2>5D Trading Signal</h2>
            <div class="apples" id="apples">🍏🍏🍎🍎🍎</div>
            <div class="signal-label neutral" id="signal-label">LOADING...</div>
            <div style="color: #6b7280; margin-top: 10px; font-size: 14px;">
                Score: <span id="signal-score">0</span>/5
            </div>
        </div>
        
        <!-- Stats Grid -->
        <div class="stats-grid">
            <div class="stat-card">
                <div class="label">💰 KAS Price</div>
                <div class="value" id="price">\$0.00000</div>
                <div class="sub">MEXC Real-time</div>
            </div>
            
            <div class="stat-card">
                <div class="label">📈 24h Change</div>
                <div class="value" id="change-24h">0.00%</div>
                <div class="sub">Last 24 hours</div>
            </div>
            
            <div class="stat-card">
                <div class="label">📊 24h Volume</div>
                <div class="value" id="volume-24h">0</div>
                <div class="sub">KAS traded</div>
            </div>
            
            <div class="stat-card">
                <div class="label">🎯 Momentum</div>
                <div class="value" id="momentum">0.00%</div>
                <div class="sub">Short-term trend</div>
            </div>
        </div>
        
        <!-- Whale Alert -->
        <div class="whale-alert" id="whale-alert">
            <h3>🐋 WHALE DETECTED!</h3>
            <div class="whale-details">
                <div>
                    <div class="label">Amount</div>
                    <div class="value green" id="whale-amount">0 KAS</div>
                </div>
                <div>
                    <div class="label">Tier</div>
                    <div class="value yellow" id="whale-tier">--</div>
                </div>
                <div>
                    <div class="label">Transactions</div>
                    <div class="value" id="whale-count">0</div>
                </div>
                <div>
                    <div class="label">Detected</div>
                    <div class="value" id="whale-time">--</div>
                </div>
            </div>
        </div>
        
        <!-- Prediction -->
        <div class="prediction-box">
            <h3>🔮 5-MINUTE PREDICTION</h3>
            <div style="display: flex; align-items: center; gap: 20px; flex-wrap: wrap;">
                <div class="prediction-direction up" id="pred-direction">⬆️ UP</div>
                <div>
                    <div style="font-size: 24px; font-weight: bold;" id="pred-change">+0.00%</div>
                    <div style="color: #6b7280; font-size: 12px;">Expected change</div>
                </div>
                <div>
                    <div style="font-size: 24px; font-weight: bold;" id="pred-target">\$0.00000</div>
                    <div style="color: #6b7280; font-size: 12px;">Target price</div>
                </div>
                <div>
                    <div style="font-size: 24px; font-weight: bold;" id="pred-conf">0%</div>
                    <div style="color: #6b7280; font-size: 12px;">Confidence</div>
                </div>
            </div>
            <div style="color: #6b7280; margin-top: 15px; font-size: 13px;" id="pred-reasoning">
                Loading analysis...
            </div>
        </div>
        
        <!-- Chart -->
        <div class="chart-container">
            <h3>📊 PRICE CHART (30 min)</h3>
            <canvas id="price-chart"></canvas>
        </div>
        
        <footer class="footer">
            <p><span class="live-dot"></span> LIVE • Updates every 30 seconds</p>
            <p style="margin-top: 10px;">⚠️ Not financial advice. DYOR. • Powered by MEXC & Kaspa API</p>
            <p style="margin-top: 5px;" id="last-update">Last update: --</p>
        </footer>
    </div>
    
    <script>
        // Simple chart drawing
        function drawChart(candles) {
            const canvas = document.getElementById('price-chart');
            const ctx = canvas.getContext('2d');
            
            // Set canvas size
            canvas.width = canvas.offsetWidth * 2;
            canvas.height = 600;
            ctx.scale(2, 2);
            
            const width = canvas.offsetWidth;
            const height = 300;
            
            // Clear
            ctx.fillStyle = '#0a0a0f';
            ctx.fillRect(0, 0, width, height);
            
            if (!candles || candles.length < 2) {
                ctx.fillStyle = '#374151';
                ctx.font = '14px Inter';
                ctx.textAlign = 'center';
                ctx.fillText('Waiting for data...', width/2, height/2);
                return;
            }
            
            // Calculate min/max
            const prices = candles.flatMap(c => [c.high, c.low]);
            const minP = Math.min(...prices) * 0.9995;
            const maxP = Math.max(...prices) * 1.0005;
            const range = maxP - minP || 0.00001;
            
            const pad = 40;
            const chartWidth = width - pad * 2;
            const chartHeight = height - pad * 2;
            
            function toX(i) { return pad + (i / (candles.length - 1)) * chartWidth; }
            function toY(p) { return pad + (1 - (p - minP) / range) * chartHeight; }
            
            // Draw grid
            ctx.strokeStyle = 'rgba(75, 85, 99, 0.2)';
            ctx.lineWidth = 1;
            for (let i = 0; i <= 5; i++) {
                const y = pad + (chartHeight / 5) * i;
                ctx.beginPath();
                ctx.moveTo(pad, y);
                ctx.lineTo(width - pad, y);
                ctx.stroke();
                
                const price = maxP - (range / 5) * i;
                ctx.fillStyle = '#4b5563';
                ctx.font = '10px Inter';
                ctx.textAlign = 'right';
                ctx.fillText('$' + price.toFixed(5), pad - 5, y + 3);
            }
            
            // Draw candles
            const candleWidth = Math.max(4, chartWidth / candles.length * 0.7);
            
            candles.forEach((c, i) => {
                const x = toX(i);
                const isUp = c.close >= c.open;
                
                // Wick
                ctx.strokeStyle = isUp ? '#22c55e' : '#ef4444';
                ctx.lineWidth = 1;
                ctx.beginPath();
                ctx.moveTo(x, toY(c.high));
                ctx.lineTo(x, toY(c.low));
                ctx.stroke();
                
                // Body
                const bodyTop = toY(Math.max(c.open, c.close));
                const bodyBottom = toY(Math.min(c.open, c.close));
                const bodyHeight = Math.max(bodyBottom - bodyTop, 1);
                
                ctx.fillStyle = isUp ? '#22c55e' : '#ef4444';
                ctx.fillRect(x - candleWidth/2, bodyTop, candleWidth, bodyHeight);
            });
            
            // Current price line
            const lastPrice = candles[candles.length - 1].close;
            ctx.strokeStyle = '#3b82f6';
            ctx.lineWidth = 1;
            ctx.setLineDash([5, 5]);
            ctx.beginPath();
            ctx.moveTo(pad, toY(lastPrice));
            ctx.lineTo(width - pad, toY(lastPrice));
            ctx.stroke();
            ctx.setLineDash([]);
            
            ctx.fillStyle = '#3b82f6';
            ctx.font = 'bold 11px Inter';
            ctx.textAlign = 'left';
            ctx.fillText('$' + lastPrice.toFixed(5), width - pad + 5, toY(lastPrice) + 3);
        }
        
        // Update UI
        async function updateData() {
            try {
                const res = await fetch('/api/data');
                const d = await res.json();
                
                if (d.error) {
                    console.error(d.error);
                    return;
                }
                
                // Price
                document.getElementById('price').textContent = '$' + d.price.toFixed(5);
                
                // 24h change
                const changeEl = document.getElementById('change-24h');
                changeEl.textContent = (d.price_change_24h >= 0 ? '+' : '') + d.price_change_24h.toFixed(2) + '%';
                changeEl.className = 'value ' + (d.price_change_24h >= 0 ? 'green' : 'red');
                
                // Volume
                document.getElementById('volume-24h').textContent = Number(d.volume_24h).toLocaleString(undefined, {maximumFractionDigits: 0});
                
                // Momentum
                const momEl = document.getElementById('momentum');
                momEl.textContent = (d.momentum >= 0 ? '+' : '') + d.momentum.toFixed(2) + '%';
                momEl.className = 'value ' + (d.momentum >= 0 ? 'green' : 'red');
                
                // 5D Signal
                if (d.signal_5d) {
                    document.getElementById('apples').textContent = d.signal_5d.apples;
                    document.getElementById('signal-score').textContent = d.signal_5d.green;
                    
                    const labelEl = document.getElementById('signal-label');
                    labelEl.textContent = d.signal_5d.signal;
                    labelEl.className = 'signal-label ' + 
                        (d.signal_5d.green >= 3 ? 'buy' : d.signal_5d.green <= 1 ? 'sell' : 'neutral');
                }
                
                // Whale alert
                const whaleEl = document.getElementById('whale-alert');
                if (d.whale_alert) {
                    whaleEl.classList.add('active');
                    document.getElementById('whale-amount').textContent = Number(d.whale_alert.kas_amount).toLocaleString() + ' KAS';
                    document.getElementById('whale-tier').textContent = d.whale_alert.tier;
                    document.getElementById('whale-count').textContent = d.whale_alert.count;
                    document.getElementById('whale-time').textContent = d.whale_alert.timestamp;
                } else {
                    whaleEl.classList.remove('active');
                }
                
                // Prediction
                if (d.prediction) {
                    const p = d.prediction;
                    const dirEl = document.getElementById('pred-direction');
                    const arrow = p.direction === 'UP' ? '⬆️' : p.direction === 'DOWN' ? '⬇️' : '➡️';
                    dirEl.textContent = arrow + ' ' + p.direction;
                    dirEl.className = 'prediction-direction ' + p.direction.toLowerCase();
                    
                    document.getElementById('pred-change').textContent = (p.change_pct >= 0 ? '+' : '') + p.change_pct.toFixed(2) + '%';
                    document.getElementById('pred-target').textContent = '$' + p.target.toFixed(5);
                    document.getElementById('pred-conf').textContent = p.confidence + '%';
                    document.getElementById('pred-reasoning').textContent = p.reasoning;
                }
                
                // Chart
                if (d.candles && d.candles.length > 0) {
                    drawChart(d.candles);
                }
                
                // Last update
                document.getElementById('last-update').textContent = 'Last update: ' + d.last_update;
                
            } catch (e) {
                console.error('Update error:', e);
            }
        }
        
        // Initial load and interval
        updateData();
        setInterval(updateData, 5000);
        
        // Resize chart on window resize
        window.addEventListener('resize', () => {
            fetch('/api/data').then(r => r.json()).then(d => {
                if (d.candles) drawChart(d.candles);
            });
        });
    </script>
</body>
</html>'''


@app.route("/")
def index():
    return Response(DASHBOARD_HTML, mimetype="text/html")


@app.route("/api/data")
def api_data():
    """API endpoint for dashboard data"""
    return jsonify(current_state)


@app.route("/api/whales")
def api_whales():
    """API endpoint for whale history"""
    return jsonify(list(whale_history))


@app.route("/api/health")
def api_health():
    """Health check endpoint"""
    return jsonify({
        "status": "ok",
        "last_update": current_state.get("last_update"),
        "price": current_state.get("price", 0)
    })


# ═══════════════════════════════════════════════════════════════
# 🚀 STARTUP
# ═══════════════════════════════════════════════════════════════

def start_scanner():
    """Start the background scanner in a separate thread"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(scanner_loop())


if __name__ == "__main__":
    print("=" * 60)
    print("🍏 KASPA WHALE ORACLE v4.0")
    print("=" * 60)
    print("📊 Dashboard: http://localhost:5000")
    print("📡 API: http://localhost:5000/api/data")
    print("🐋 Whales: http://localhost:5000/api/whales")
    print("=" * 60)
    
    # Start scanner in background thread
    scanner_thread = threading.Thread(target=start_scanner, daemon=True)
    scanner_thread.start()
    
    # Run Flask
    app.run(host="0.0.0.0", port=5000, debug=False)
