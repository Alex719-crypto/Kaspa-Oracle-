"""
╔══════════════════════════════════════════════════════════════╗
║        Kaspa Whale Oracle v3.1 — Prediction Engine           ║
╠══════════════════════════════════════════════════════════════╣
║  SECRETS NEEDED (Agentverse → Secrets tab):                  ║
║    OPEN_API_KEY — ASI-1 key (asi1.ai/developer)              ║
╚══════════════════════════════════════════════════════════════╝
"""

import asyncio
import os
import time
from datetime import datetime, timezone
from uuid import uuid4
from typing import Optional
from collections import deque
import threading
import json

import aiohttp
from flask import Flask, Response
from pydantic import Field

from uagents import Agent, Context, Model, Protocol
from uagents.setup import fund_agent_if_low
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    EndSessionContent,
    TextContent,
    chat_protocol_spec,
)
from uagents_core.contrib.protocols.payment import (
    CancelPayment,
    CommitPayment,
    CompletePayment,
    Funds,
    payment_protocol_spec,
    RejectPayment,
    RequestPayment,
)

try:
    from openai import OpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

# ═══════════════════════════════════════════════════════════════
# ⚙️  CONFIGURATION
# ═══════════════════════════════════════════════════════════════

WALLET_ADDRESS    = "fetch1jq6tmuhq34ar8jme532sgxrghg4lw05ztq33n3"
ASI_API_KEY       = os.environ.get("OPEN_API_KEY", "")

PRICE_FULL        = "0.02"
PRICE_PREMIUM     = "0.05"
PAYMENT_DEADLINE  = 180
SCAN_INTERVAL_SEC = 60.0

KAS_WHALE_THRESHOLD   = 500_000
SOMPI_PER_KAS         = 100_000_000
WHALE_THRESHOLD_SOMPI = KAS_WHALE_THRESHOLD * SOMPI_PER_KAS
MEGA_WHALE            = 5_000_000
BIG_WHALE             = 1_000_000
KAS_USD_ESTIMATE      = 0.12

KAS_API_ENDPOINTS = [
    "https://api.kaspa.org",
    "https://kaspa.aspectron.org/api",
]
CG_BASE = "https://api.coingecko.com/api/v3"

# ═══════════════════════════════════════════════════════════════
# 🔮 PREDICTION ENGINE
# ═══════════════════════════════════════════════════════════════

MAX_HISTORY    = 30
MAX_OUTCOMES   = 100
price_history: deque = deque(maxlen=MAX_HISTORY)
outcome_log:   dict  = {}
accuracy_stats = {"total": 0, "correct": 0, "pct": 0.0}


def record_price_snapshot(kas_price: float, whale_kas: float, whale_count: int):
    price_history.append({
        "ts":          time.time(),
        "price":       kas_price,
        "whale_kas":   whale_kas,
        "whale_count": whale_count,
    })


def _calc_momentum() -> float:
    if len(price_history) < 3:
        return 0.0
    recent = list(price_history)[-3:]
    p0 = recent[0]["price"]
    p1 = recent[-1]["price"]
    return round(((p1 - p0) / p0) * 100, 3) if p0 else 0.0


def _past_whale_avg(tier: str) -> Optional[float]:
    hist = list(price_history)
    if len(hist) < 7:
        return None
    results = []
    for i in range(len(hist) - 2):
        s = hist[i]
        if s["whale_kas"] < KAS_WHALE_THRESHOLD:
            continue
        h_tier = (
            "MEGA" if s["whale_kas"] >= MEGA_WHALE
            else "BIG" if s["whale_kas"] >= BIG_WHALE
            else "WHALE"
        )
        if h_tier != tier:
            continue
        p0, p1 = s["price"], hist[i + 1]["price"]
        if p0 and p1:
            results.append(((p1 - p0) / p0) * 100)
    return round(sum(results) / len(results), 3) if results else None


def _cleanup_outcome_log():
    if len(outcome_log) <= MAX_OUTCOMES:
        return
    sorted_keys = sorted(
        outcome_log.keys(),
        key=lambda k: outcome_log[k].get("ts", 0),
    )
    for k in sorted_keys[: len(outcome_log) - MAX_OUTCOMES]:
        del outcome_log[k]


def predict_5min_price(
    current_price: float,
    whale_kas: float,
    whale_count: int,
    tier: str,
) -> dict:
    if not current_price or current_price <= 0:
        return {
            "direction": "FLAT",
            "change_pct": 0.0,
            "confidence": 0,
            "reasoning": "No price data",
            "price_target": current_price,
            "scan_id": str(uuid4()),
        }

    reasons = []

    if tier == "MEGA":
        base_pct, base_conf = 1.8, 72
        reasons.append(f"MEGA whale ({whale_kas:,.0f} KAS) — strongest historical signal")
    elif tier == "BIG":
        base_pct, base_conf = 0.9, 58
        reasons.append(f"BIG whale ({whale_kas:,.0f} KAS) — medium upward pressure")
    else:
        base_pct, base_conf = 0.4, 44
        reasons.append(f"Whale ({whale_kas:,.0f} KAS) — weak buy pressure")

    if whale_count >= 3:
        base_pct *= 1.4
        base_conf += 8
        reasons.append(f"{whale_count} simultaneous whale TXs — cluster signal")
    elif whale_count == 2:
        base_pct *= 1.2
        base_conf += 4

    momentum = _calc_momentum()
    if momentum > 0.3:
        base_pct *= 1.15
        base_conf += 6
        reasons.append(f"Price momentum +{momentum:.2f}% — confirming")
    elif momentum < -0.3:
        base_pct *= 0.70
        base_conf -= 8
        reasons.append(f"Price momentum {momentum:.2f}% — headwind")

    if accuracy_stats["total"] >= 5:
        acc = accuracy_stats["pct"]
        if acc >= 70:
            base_conf += 5
            reasons.append(f"Model accuracy {acc:.0f}% on last {accuracy_stats['total']} calls")
        elif acc < 45:
            base_conf -= 10
            reasons.append(f"Model accuracy {acc:.0f}% — use caution")

    past = _past_whale_avg(tier)
    if past is not None:
        if past > 0:
            base_conf += 7
            reasons.append(f"Past {tier} whales avg +{past:.2f}% in 5 min")
        else:
            base_pct *= 0.8
            base_conf -= 5
            reasons.append(f"Past {tier} whales avg {past:.2f}% — mixed")

    base_pct  = round(min(base_pct, 5.0), 2)
    base_conf = max(20, min(base_conf, 92))
    direction = "UP" if base_pct > 0.1 else "DOWN" if base_pct < -0.1 else "FLAT"
    target    = round(current_price * (1 + base_pct / 100), 6)

    return {
        "direction": direction,
        "change_pct": base_pct,
        "confidence": base_conf,
        "reasoning": " | ".join(reasons),
        "price_target": target,
        "scan_id": str(uuid4()),
    }


def predict_no_whale(current_price: float) -> dict:
    if not current_price or current_price <= 0:
        return {
            "direction": "FLAT",
            "change_pct": 0.0,
            "confidence": 0,
            "reasoning": "No price data",
            "price_target": current_price,
            "scan_id": str(uuid4()),
        }

    momentum = _calc_momentum()
    if abs(momentum) < 0.1:
        return {
            "direction": "FLAT",
            "change_pct": 0.0,
            "confidence": 30,
            "reasoning": "Low volatility, no whale activity",
            "price_target": current_price,
            "scan_id": str(uuid4()),
        }

    direction  = "UP" if momentum > 0 else "DOWN"
    change     = round(min(max(momentum * 0.25, -1.5), 1.5), 2)
    confidence = max(20, min(45, int(abs(momentum) * 15)))
    target     = round(current_price * (1 + change / 100), 6)

    return {
        "direction": direction,
        "change_pct": change,
        "confidence": confidence,
        "reasoning": f"Momentum-based: {momentum:+.2f}% trend | No whale activity",
        "price_target": target,
        "scan_id": str(uuid4()),
    }


def verify_prediction_outcome(scan_id: str, current_price: float):
    entry = outcome_log.get(scan_id)
    if not entry or entry.get("verified"):
        return
    old_price  = entry.get("price_at_prediction")
    if not old_price:
        return
    actual_pct = ((current_price - old_price) / old_price) * 100
    actual_dir = "UP" if actual_pct > 0.05 else "DOWN" if actual_pct < -0.05 else "FLAT"
    correct    = actual_dir == entry["pred_dir"]
    entry.update({"actual_pct": round(actual_pct, 3), "correct": correct, "verified": True})
    accuracy_stats["total"]   += 1
    accuracy_stats["correct"] += int(correct)
    t = accuracy_stats["total"]
    accuracy_stats["pct"] = round((accuracy_stats["correct"] / t) * 100, 1) if t else 0.0


def verify_all_pending(kas_price: float):
    now = time.time()
    for sid, entry in list(outcome_log.items()):
        if not entry.get("verified") and (now - entry.get("ts", 0)) >= 300:
            verify_prediction_outcome(sid, kas_price)

# ═══════════════════════════════════════════════════════════════
# 📊 MODELS
# ═══════════════════════════════════════════════════════════════

class WhaleAlert(Model):
    kas_amount:   float
    whale_count:  int
    tier:         str
    timestamp:    str
    total_volume: float = 0.0
    transactions: list  = Field(default_factory=list)


class PricePrediction(Model):
    direction:    str
    change_pct:   float
    confidence:   int
    price_now:    float
    price_target: float
    reasoning:    str
    scan_id:      str
    tier:         str


class ChainMetrics(Model):
    chain:        str
    signal:       str
    metric_value: float
    metric_label: str
    note:         str
    confidence:   int = 50


class IntelligenceReport(Model):
    sentiment:       str
    sentiment_score: int
    confidence:      int
    kas:             ChainMetrics
    sol:             ChainMetrics
    tao:             ChainMetrics
    fet:             ChainMetrics
    whale_alert:     Optional[WhaleAlert]      = None
    prediction:      Optional[PricePrediction]  = None
    timestamp:       str
    node:            str
    scan_id:         str


class IntelRequest(Model):
    min_score: int = 0


class ErrorResponse(Model):
    error: str

# ═══════════════════════════════════════════════════════════════
# 📝 FORMATTERS
# ═══════════════════════════════════════════════════════════════

def fmt_prediction_text(pred: PricePrediction) -> str:
    arrow    = "⬆️ UP" if pred.direction == "UP" else "⬇️ DOWN" if pred.direction == "DOWN" else "➡️ FLAT"
    conf_bar = "█" * (pred.confidence // 10) + "░" * (10 - pred.confidence // 10)
    return (
        f"🔮 **5-MIN PRICE PREDICTION**\n{'─' * 28}\n"
        f"Direction: **{arrow}**\n"
        f"Expected:  **{pred.change_pct:+.2f}%**\n"
        f"Target:    **${pred.price_target:.6f}**\n"
        f"Now:       **${pred.price_now:.6f}**\n"
        f"Confidence:[{conf_bar}] **{pred.confidence}%**\n"
        f"Trigger:   **{pred.tier}**\n\n"
        f"_{pred.reasoning[:220]}_\n\n"
        f"Model accuracy: **{accuracy_stats['pct']:.1f}%** "
        f"({accuracy_stats['correct']}/{accuracy_stats['total']})\n\n"
        f"⚠️ _Not financial advice. DYOR._"
    )


def fmt_market_text(r: IntelligenceReport) -> str:
    def sig(s):
        return "🟢" if s == "BULLISH" else "🔴" if s == "BEARISH" else "🟡"

    conf_bar = "█" * (r.confidence // 10) + "░" * (10 - r.confidence // 10)

    whale_sec = (
        f"\n🐋 **WHALE**: {r.whale_alert.kas_amount:,.0f} KAS | "
        f"{r.whale_alert.whale_count} TX(s) | {r.whale_alert.tier}\n"
        if r.whale_alert and r.whale_alert.whale_count > 0
        else "\n🌊 Whale Monitor: Quiet\n"
    )

    pred_sec = ""
    if r.prediction:
        p     = r.prediction
        arrow = "⬆️" if p.direction == "UP" else "⬇️" if p.direction == "DOWN" else "➡️"
        pred_sec = (
            f"🔮 **5-min prediction**: {arrow} **{p.direction} {p.change_pct:+.2f}%** "
            f"(conf: {p.confidence}%)\n"
        )

    acc_sec = (
        f"🎯 Model accuracy: **{accuracy_stats['pct']:.0f}%** "
        f"({accuracy_stats['correct']}/{accuracy_stats['total']})\n"
        if accuracy_stats["total"] >= 3
        else ""
    )

    return (
        f"🌐 **KASPA WHALE ORACLE REPORT**\n{'─' * 32}\n"
        f"📊 Sentiment: **{r.sentiment}**\n"
        f"🎯 Score: **{r.sentiment_score}/4**\n"
        f"📈 Confidence: [{conf_bar}] {r.confidence}%\n{'─' * 32}\n"
        f"{sig(r.kas.signal)} **KAS**: {r.kas.note}\n"
        f"{sig(r.sol.signal)} **SOL**: {r.sol.note}\n"
        f"{sig(r.tao.signal)} **TAO**: {r.tao.note}\n"
        f"{sig(r.fet.signal)} **FET**: {r.fet.note}\n"
        f"{whale_sec}{pred_sec}{'─' * 32}\n"
        f"{acc_sec}"
        f"🕐 {r.timestamp} | 🔄 60 sec\n"
        f"💎 Full report: 0.02 FET | ⚠️ _DYOR_"
    )

# ═══════════════════════════════════════════════════════════════
# 🌐 DATA FETCHERS
# ═══════════════════════════════════════════════════════════════

async def fetch_kas_price(session) -> float:
    try:
        async with session.get(
            f"{CG_BASE}/simple/price?ids=kaspa&vs_currencies=usd",
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                return float(data.get("kaspa", {}).get("usd", KAS_USD_ESTIMATE))
    except Exception:
        pass
    return KAS_USD_ESTIMATE


async def fetch_with_retry(session, url, retries=3):
    for i in range(retries):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as r:
                if r.status == 429:
                    await asyncio.sleep(2 ** i)
                    continue
                if r.status == 200:
                    return await r.json(content_type=None)
        except Exception:
            await asyncio.sleep(0.5 * i)
    return None


async def fetch_kas(session) -> tuple:
    for api_base in KAS_API_ENDPOINTS:
        try:
            raw = await fetch_with_retry(
                session,
                f"{api_base}/blocks?includeTransactions=true&limit=20",
            )
            if not raw:
                continue
            blocks = (
                raw.get("blocks", [])
                if isinstance(raw, dict)
                else (raw if isinstance(raw, list) else [])
            )
            whale_count, max_kas, total_kas, whale_txs = 0, 0.0, 0.0, []
            for block in blocks:
                for tx in block.get("transactions", []):
                    tx_id = (tx.get("transactionId") or "")[:20]
                    for out in tx.get("outputs", []):
                        try:
                            sompi = int(out.get("amount", 0))
                        except (TypeError, ValueError):
                            continue
                        kas_val = sompi / SOMPI_PER_KAS
                        if sompi >= WHALE_THRESHOLD_SOMPI:
                            whale_count += 1
                            total_kas += kas_val
                            max_kas = max(max_kas, kas_val)
                            whale_txs.append({"tx_id": tx_id, "amount": kas_val})
            ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
            if whale_count > 0:
                tier = (
                    "MEGA" if max_kas >= MEGA_WHALE
                    else "BIG" if max_kas >= BIG_WHALE
                    else "WHALE"
                )
                conf = 75 if tier == "MEGA" else 65 if tier == "BIG" else 55
                wa = WhaleAlert(
                    kas_amount=round(max_kas, 2),
                    whale_count=whale_count,
                    tier=tier,
                    timestamp=ts,
                    total_volume=round(total_kas, 2),
                    transactions=whale_txs[:5],
                )
                signal = "BULLISH"
                note   = f"🐋 {whale_count} whale TX | Max: {max_kas:,.0f} KAS"
            else:
                wa     = WhaleAlert(kas_amount=0, whale_count=0, tier="NONE", timestamp=ts)
                signal = "NEUTRAL"
                conf   = 50
                note   = "No whales in latest blocks"

            metrics = ChainMetrics(
                chain="KAS",
                signal=signal,
                metric_value=round(max_kas, 2),
                metric_label="Largest whale TX (KAS)",
                note=note,
                confidence=conf,
            )
            return metrics, wa
        except Exception:
            continue

    return (
        ChainMetrics(
            chain="KAS", signal="NEUTRAL", metric_value=0.0,
            metric_label="API Error", note="KAS API unavailable", confidence=30,
        ),
        None,
    )


async def fetch_sol(session) -> ChainMetrics:
    try:
        data = await fetch_with_retry(
            session,
            f"{CG_BASE}/coins/solana/market_chart?vs_currency=usd&days=1",
        )
        if not data:
            raise ValueError("No SOL data")
        prices = data.get("prices", [])
        if len(prices) >= 2:
            prev, curr = prices[-2][1], prices[-1][1]
            change = ((curr - prev) / prev) * 100 if prev else 0
            signal = "BULLISH" if change > 0.5 else "BEARISH" if change < -0.5 else "NEUTRAL"
            return ChainMetrics(
                chain="SOL", signal=signal,
                metric_value=round(change, 3),
                metric_label="Price momentum %",
                note=f"${curr:.2f} | {change:+.2f}%",
                confidence=70 if abs(change) > 1.5 else 55,
            )
    except Exception:
        pass
    return ChainMetrics(
        chain="SOL", signal="NEUTRAL", metric_value=0,
        metric_label="Error", note="SOL unavailable", confidence=30,
    )


async def fetch_coin(session, coin_id: str, name: str) -> ChainMetrics:
    try:
        data = await fetch_with_retry(
            session,
            f"{CG_BASE}/coins/markets?vs_currency=usd&ids={coin_id}",
        )
        if not data or not isinstance(data, list):
            raise ValueError("Invalid response")
        d      = data[0]
        price  = d.get("current_price") or 0
        change = d.get("price_change_percentage_24h") or 0
        vol    = d.get("total_volume") or 0
        mcap   = d.get("market_cap") or 1
        vm     = vol / mcap
        signal = (
            "BULLISH" if change > 2 and vm > 0.05
            else "BEARISH" if change < -2
            else "NEUTRAL"
        )
        conf = 70 if abs(change) > 5 else 55 if abs(change) > 2 else 45
        return ChainMetrics(
            chain=name, signal=signal,
            metric_value=round(change, 2),
            metric_label="24h change %",
            note=f"${price:.4f} | {change:+.2f}%",
            confidence=conf,
        )
    except Exception:
        pass
    return ChainMetrics(
        chain=name, signal="NEUTRAL", metric_value=0,
        metric_label="Error", note=f"{name} unavailable", confidence=30,
    )

# ═══════════════════════════════════════════════════════════════
# 🔄 ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════

_FB_KAS = ChainMetrics(chain="KAS", signal="NEUTRAL", metric_value=0, metric_label="Error", note="Failed", confidence=30)
_FB_SOL = ChainMetrics(chain="SOL", signal="NEUTRAL", metric_value=0, metric_label="Error", note="Failed", confidence=30)
_FB_TAO = ChainMetrics(chain="TAO", signal="NEUTRAL", metric_value=0, metric_label="Error", note="Failed", confidence=30)
_FB_FET = ChainMetrics(chain="FET", signal="NEUTRAL", metric_value=0, metric_label="Error", note="Failed", confidence=30)


async def run_all_scans() -> tuple:
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            fetch_kas(session),
            fetch_sol(session),
            fetch_coin(session, "bittensor", "TAO"),
            fetch_coin(session, "fetch-ai", "FET"),
            fetch_kas_price(session),
            return_exceptions=True,
        )

    def safe(r, fb):
        return r if not isinstance(r, Exception) else fb

    kas, wa   = safe(results[0], (_FB_KAS, None))
    sol       = safe(results[1], _FB_SOL)
    tao       = safe(results[2], _FB_TAO)
    fet       = safe(results[3], _FB_FET)
    kas_price = safe(results[4], KAS_USD_ESTIMATE)

    prediction = None

    if wa and wa.whale_count > 0 and wa.kas_amount > 0:
        pd = predict_5min_price(kas_price, wa.kas_amount, wa.whale_count, wa.tier)
        prediction = PricePrediction(
            direction=pd["direction"],
            change_pct=pd["change_pct"],
            confidence=pd["confidence"],
            price_now=round(kas_price, 6),
            price_target=pd["price_target"],
            reasoning=pd["reasoning"],
            scan_id=pd["scan_id"],
            tier=wa.tier,
        )
        outcome_log[pd["scan_id"]] = {
            "ts":                  time.time(),
            "pred_dir":            pd["direction"],
            "pred_pct":            pd["change_pct"],
            "price_at_prediction": kas_price,
            "verified":            False,
        }
        _cleanup_outcome_log()
    else:
        pd = predict_no_whale(kas_price)
        prediction = PricePrediction(
            direction=pd["direction"],
            change_pct=pd["change_pct"],
            confidence=pd["confidence"],
            price_now=round(kas_price, 6),
            price_target=pd["price_target"],
            reasoning=pd["reasoning"],
            scan_id=pd["scan_id"],
            tier="NONE",
        )

    record_price_snapshot(
        kas_price,
        wa.kas_amount if wa else 0,
        wa.whale_count if wa else 0,
    )

    verify_all_pending(kas_price)

    metrics    = [kas, sol, tao, fet]
    score      = sum(1 for m in metrics if m.signal == "BULLISH")
    bearish    = sum(1 for m in metrics if m.signal == "BEARISH")
    if wa and wa.whale_count > 0:
        score  = min(score + 1, 4)
    confidence = sum(m.confidence for m in metrics) // 4
    sentiment  = (
        "🟢 BULLISH" if score >= 3
        else "🔴 BEARISH" if bearish >= 3
        else "🟡 NEUTRAL"
    )

    report = IntelligenceReport(
        sentiment=sentiment,
        sentiment_score=score,
        confidence=confidence,
        kas=kas,
        sol=sol,
        tao=tao,
        fet=fet,
        whale_alert=wa,
        prediction=prediction,
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        node="Kaspa Whale Oracle v3.1",
        scan_id=str(uuid4()),
    )
    return report, kas_price

# ═══════════════════════════════════════════════════════════════
# 🤖 AGENT
# ═══════════════════════════════════════════════════════════════

agent = Agent(
    name="Kaspa Whale Oracle",
    seed="kaspa-whale-oracle-v31-2026",
    port=8002,
    endpoint=["http://127.0.0.1:8002/submit"],
)
fund_agent_if_low(agent.wallet.address())

# ═══════════════════════════════════════════════════════════════
# 🚀 STARTUP
# ═══════════════════════════════════════════════════════════════

@agent.on_event("startup")
async def startup(ctx: Context):
    ctx.storage.set("stats", {
        "total_queries":    0,
        "paid_reports":     0,
        "whale_alerts":     0,
        "revenue_fet":      0.0,
        "predictions_sent": 0,
    })
    ctx.logger.info("🐋 Kaspa Whale Oracle v3.1 — Prediction Engine")
    ctx.logger.info(f"📍 Agent : {agent.address}")
    ctx.logger.info(f"💰 Wallet: {WALLET_ADDRESS}")
    ctx.logger.info(f"🤖 ASI-1 : {'✅' if ASI_API_KEY else '⚠️ Set OPEN_API_KEY secret'}")
    try:
        report, kas_price = await run_all_scans()
        ctx.storage.set("latest_report", report.dict())
        ctx.storage.set("kas_price", kas_price)
        ctx.logger.info(f"✅ First scan: KAS=${kas_price:.5f} | {report.sentiment}")
    except Exception as e:
        ctx.logger.error(f"❌ Startup error: {e}")

# ═══════════════════════════════════════════════════════════════
# ⏰ MAIN LOOP
# ═══════════════════════════════════════════════════════════════

@agent.on_interval(period=SCAN_INTERVAL_SEC)
async def main_loop(ctx: Context):
    try:
        report, kas_price = await run_all_scans()
        old_raw = ctx.storage.get("latest_report")
        ctx.storage.set("latest_report", report.dict())
        ctx.storage.set("kas_price", kas_price)

        wc = report.whale_alert.whale_count if report.whale_alert else 0
        ctx.logger.info(
            f"📊 {report.sentiment} | KAS=${kas_price:.5f} | "
            f"Whale: {wc} | Acc: {accuracy_stats['pct']:.0f}%"
        )

        if report.whale_alert and report.whale_alert.whale_count > 0:
            wa     = report.whale_alert
            is_new = True
            if old_raw:
                old = IntelligenceReport(**old_raw)
                if (
                    old.whale_alert
                    and old.whale_alert.kas_amount == wa.kas_amount
                    and old.whale_alert.timestamp  == wa.timestamp
                ):
                    is_new = False
            if is_new:
                stats = ctx.storage.get("stats") or {}
                stats["whale_alerts"]     = stats.get("whale_alerts", 0) + 1
                stats["predictions_sent"] = stats.get("predictions_sent", 0) + (
                    1 if report.prediction else 0
                )
                ctx.storage.set("stats", stats)
                ctx.logger.info(f"🐋 {wa.tier} | {wa.kas_amount:,.0f} KAS")
                if report.prediction:
                    p = report.prediction
                    ctx.logger.info(
                        f"🔮 {p.direction} {p.change_pct:+.2f}% | "
                        f"Target: ${p.price_target:.5f} | Conf: {p.confidence}%"
                    )
    except Exception as e:
        ctx.logger.error(f"❌ Main loop error: {e}")

# ═══════════════════════════════════════════════════════════════
# 💬 CHAT PROTOCOL
# ═══════════════════════════════════════════════════════════════

chat_protocol = Protocol(spec=chat_protocol_spec)


def _make_chat(text: str, end_session: bool = False) -> ChatMessage:
    content = [TextContent(type="text", text=text)]
    if end_session:
        content.append(EndSessionContent(type="end-session"))
    return ChatMessage(
        timestamp=datetime.now(timezone.utc),
        msg_id=str(uuid4()),
        content=content,
    )


def _build_ai_context(report, kas_price) -> str:
    ctx_str = f"KAS=${kas_price:.5f}. "
    if report:
        ctx_str += f"Market: {report.sentiment}. KAS: {report.kas.note}. "
    if report and report.prediction:
        p = report.prediction
        ctx_str += (
            f"Prediction: {p.direction} {p.change_pct:+.2f}% "
            f"conf:{p.confidence}%. "
        )
    return ctx_str


@chat_protocol.on_message(ChatMessage)
async def handle_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(
        sender,
        ChatAcknowledgement(
            timestamp=datetime.now(timezone.utc),
            acknowledged_msg_id=msg.msg_id,
        ),
    )

    user_input = " ".join(
        b.text for b in msg.content if hasattr(b, "text") and b.text
    ).strip()
    text_l = user_input.lower().strip()

    stats = ctx.storage.get("stats") or {}
    stats["total_queries"] = stats.get("total_queries", 0) + 1
    ctx.storage.set("stats", stats)

    raw       = ctx.storage.get("latest_report")
    report    = IntelligenceReport(**raw) if raw else None
    kas_price = ctx.storage.get("kas_price") or KAS_USD_ESTIMATE

    if not text_l or text_l in ("hi", "hello", "hey", "start", "help"):
        await ctx.send(sender, _make_chat(
            "🐋 **Kaspa Whale Oracle v3.1**\n\n"
            "Commands:\n"
            "• `report`    — full market scan\n"
            "• `whales`    — latest whale activity\n"
            "• `predict`   — 5-min price prediction 🔮\n"
            "• `accuracy`  — model performance stats\n"
            "• `price`     — current KAS price\n\n"
            "Or ask any question about KAS!\n"
            "⚠️ _Not financial advice. DYOR._"
        ))
        return

    if text_l in ("predict", "prediction", "5min", "forecast"):
        if report and report.prediction:
            await ctx.send(sender, _make_chat(fmt_prediction_text(report.prediction)))
        else:
            await ctx.send(sender, _make_chat(
                "⏳ No prediction yet. Waiting for data…\n"
                "Try `report` for the current market scan."
            ))
        return

    if text_l in ("accuracy", "stats", "performance"):
        if accuracy_stats["total"] == 0:
            resp = "📊 Not enough data yet. Need whale events to start tracking."
        else:
            acc  = accuracy_stats["pct"]
            icon = "🟢" if acc >= 65 else "🟡" if acc >= 50 else "🔴"
            resp = (
                f"🎯 **Prediction Performance**\n{'─' * 28}\n"
                f"{icon} Accuracy: **{acc:.1f}%**\n"
                f"✅ Correct: **{accuracy_stats['correct']}** | "
                f"Total: **{accuracy_stats['total']}**\n\n"
                f"_Verified over 5-min windows after each whale event._"
            )
        await ctx.send(sender, _make_chat(resp))
        return

    if text_l in ("whales", "whale"):
        if report and report.whale_alert and report.whale_alert.whale_count > 0:
            wa  = report.whale_alert
            usd = wa.kas_amount * kas_price
            await ctx.send(sender, _make_chat(
                f"🐋 **WHALE DETECTED!**\n{'─' * 28}\n"
                f"Size: **{wa.kas_amount:,.0f} KAS** (~${usd:,.0f})\n"
                f"TXs: **{wa.whale_count}** | Tier: **{wa.tier}**\n"
                f"Volume: **{wa.total_volume:,.0f} KAS**\n\n"
                f"🔒 Pay {PRICE_FULL} FET → full TX details"
            ))
            await ctx.send(sender, RequestPayment(
                accepted_funds=[
                    Funds(amount=PRICE_FULL, currency="FET", payment_method="wallet"),
                ],
                recipient=agent.address,
                deadline_seconds=PAYMENT_DEADLINE,
                reference=str(uuid4()),
                description="Unlock Whale TX Details",
                metadata={"service": "whale_detail"},
            ))
        else:
            await ctx.send(sender, _make_chat(
                "🌊 No whale activity right now.\n"
                "Threshold: 500,000 KAS. Scanning every 60 sec…"
            ))
        return

    if text_l in ("price", "kas", "kas price"):
        await ctx.send(sender, _make_chat(
            f"💰 **KAS Price**: ${kas_price:.5f}\n"
            f"🕐 Updated: {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
        ))
        return

    if text_l in ("report", "scan", "market"):
        if not report:
            await ctx.send(sender, _make_chat("⏳ First scan in progress. Try in 30 seconds!"))
            return
        await ctx.send(sender, _make_chat(fmt_market_text(report)))
        await ctx.send(sender, RequestPayment(
            accepted_funds=[
                Funds(amount=PRICE_FULL, currency="FET", payment_method="wallet"),
            ],
            recipient=agent.address,
            deadline_seconds=PAYMENT_DEADLINE,
            reference=str(uuid4()),
            description="Kaspa Whale Oracle — Full Report",
            metadata={"service": "full_report"},
        ))
        return

    ai_ctx = _build_ai_context(report, kas_price)

    if HAS_OPENAI and ASI_API_KEY and len(user_input) > 3:
        try:
            client = OpenAI(base_url="https://api.asi1.ai/v1", api_key=ASI_API_KEY)
            resp_ai = client.chat.completions.create(
                model="asi1-mini",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            f"You are Kaspa Whale Oracle v3.1. Expert on KAS blockchain. "
                            f"Current data: {ai_ctx} Be concise (max 150 words). Always add DYOR."
                        ),
                    },
                    {"role": "user", "content": user_input},
                ],
                max_tokens=300,
            )
            await ctx.send(sender, _make_chat(
                f"🤖 **AI Analysis**\n{'─' * 28}\n"
                f"{resp_ai.choices[0].message.content}\n\n⚠️ _DYOR_"
            ))
        except Exception:
            await ctx.send(sender, _make_chat(f"📊 {ai_ctx}\n\n⚠️ _DYOR_"))
    else:
        await ctx.send(sender, _make_chat(
            f"📊 {ai_ctx}\n\n"
            f"Try: `report` · `whales` · `predict` · `accuracy`"
        ))


@chat_protocol.on_message(ChatAcknowledgement)
async def handle_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass


agent.include(chat_protocol, publish_manifest=True)

# ═══════════════════════════════════════════════════════════════
# 💳 PAYMENT PROTOCOL
# ═══════════════════════════════════════════════════════════════

payment_protocol = Protocol(spec=payment_protocol_spec, role="seller")


@payment_protocol.on_message(CommitPayment)
async def on_payment_commit(ctx: Context, sender: str, msg: CommitPayment):
    amount = float(msg.funds.amount)
    if msg.funds.currency != "FET" or amount < float(PRICE_FULL):
        await ctx.send(sender, CancelPayment(
            transaction_id=msg.transaction_id,
            reason=f"Need minimum {PRICE_FULL} FET.",
        ))
        return

    await ctx.send(sender, CompletePayment(transaction_id=msg.transaction_id))

    stats = ctx.storage.get("stats") or {}
    stats["paid_reports"] = stats.get("paid_reports", 0) + 1
    stats["revenue_fet"]  = stats.get("revenue_fet", 0.0) + amount
    ctx.storage.set("stats", stats)
    ctx.logger.info(f"✅ Revenue: +{amount} FET | Total: {stats['revenue_fet']:.2f}")

    raw    = ctx.storage.get("latest_report")
    report = IntelligenceReport(**raw) if raw else None

    lines = ["💎 **PAID FULL REPORT**\n"]

    if report:
        lines.append(fmt_market_text(report))

        if report.whale_alert and report.whale_alert.whale_count > 0:
            wa = report.whale_alert
            lines.append(
                f"\n🐋 **WHALE TX DETAILS**\n"
                f"Amount: {wa.kas_amount:,.0f} KAS\n"
                f"Volume: {wa.total_volume:,.0f} KAS\n"
                f"TXs: {wa.whale_count}\n"
            )
            if wa.transactions:
                for i, tx in enumerate(wa.transactions[:5], 1):
                    lines.append(
                        f"  {i}. `{tx.get('tx_id', '?')}…` — "
                        f"{tx.get('amount', 0):,.0f} KAS"
                    )

        if report.prediction:
            p = report.prediction
            lines.append(
                f"\n🔮 **PREDICTION**\n"
                f"Direction: {p.direction} {p.change_pct:+.2f}%\n"
                f"Target: ${p.price_target:.6f} | Conf: {p.confidence}%\n"
                f"Reason: _{p.reasoning}_\n"
                f"Model accuracy: {accuracy_stats['pct']:.1f}% "
                f"({accuracy_stats['correct']}/{accuracy_stats['total']})\n"
            )
    else:
        lines.append("⏳ First scan in progress.")

    await ctx.send(sender, _make_chat("\n".join(lines)))


@payment_protocol.on_message(RejectPayment)
async def on_payment_reject(ctx: Context, sender: str, msg: RejectPayment):
    await ctx.send(sender, _make_chat(
        "❌ Payment cancelled.\nFree features still available — try `report` or `predict`!"
    ))


agent.include(payment_protocol, publish_manifest=True)

# ═══════════════════════════════════════════════════════════════
# 📨 DIRECT INTEL REQUEST
# ═══════════════════════════════════════════════════════════════

@agent.on_message(model=IntelRequest, replies={IntelligenceReport, ErrorResponse})
async def handle_intel_request(ctx: Context, sender: str, msg: IntelRequest):
    raw = ctx.storage.get("latest_report")
    if not raw:
        await ctx.send(sender, ErrorResponse(error="No report yet."))
        return
    report = IntelligenceReport(**raw)
    if report.sentiment_score < msg.min_score:
        await ctx.send(sender, ErrorResponse(
            error=f"Score {report.sentiment_score} < min {msg.min_score}"
        ))
        return
    await ctx.send(sender, report)

# ═══════════════════════════════════════════════════════════════
# 🍏 FLASK DASHBOARD
# ═══════════════════════════════════════════════════════════════

flask_app = Flask(__name__)

DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>🍏 Kaspa Whale Oracle</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&display=swap');
        * { box-sizing: border-box; margin: 0; padding: 0; }

        body {
            background: #05070a;
            color: white;
            font-family: 'Orbitron', Arial, sans-serif;
            overflow: hidden;
        }

        #header {
            position: fixed;
            top: 0; left: 0; right: 0;
            height: 70px;
            background: rgba(5,7,10,0.97);
            display: flex;
            align-items: center;
            padding: 0 20px;
            z-index: 20;
            border-bottom: 1px solid rgba(59,130,246,0.2);
            gap: 15px;
        }

        .logo {
            font-size: 30px;
            filter: drop-shadow(0 0 12px rgba(34,197,94,0.9));
            animation: pulse 2s ease-in-out infinite;
        }

        @keyframes pulse {
            0%,100% { transform: scale(1); }
            50%      { transform: scale(1.12); }
        }

        .title {
            font-size: 14px;
            font-weight: 900;
            letter-spacing: 2px;
            background: linear-gradient(135deg, #22c55e, #3b82f6);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        #pred-box {
            background: rgba(17,24,39,0.9);
            border: 1px solid rgba(59,130,246,0.3);
            border-radius: 10px;
            padding: 8px 15px;
            font-size: 11px;
            letter-spacing: 1px;
            min-width: 220px;
        }

        #pred-direction {
            font-size: 15px;
            font-weight: 900;
        }

        #price-box {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-left: auto;
        }

        #price-apple { font-size: 26px; transition: all 0.3s; }

        #price {
            font-size: 20px;
            font-weight: 900;
            letter-spacing: 1px;
            transition: color 0.3s;
        }

        #whale-alert {
            position: fixed;
            top: 85px; right: 25px;
            z-index: 30;
            font-size: 0px;
            font-weight: 900;
            opacity: 0;
            transition: all 0.4s cubic-bezier(0.34,1.56,0.64,1);
            pointer-events: none;
            letter-spacing: 1px;
        }

        #whale-alert.show {
            font-size: 18px;
            opacity: 1;
        }

        #apple-meter {
            position: fixed;
            left: 12px;
            top: 50%;
            transform: translateY(-50%);
            z-index: 15;
            display: flex;
            flex-direction: column;
            gap: 5px;
            align-items: center;
        }

        .am {
            font-size: 20px;
            opacity: 0.15;
            transition: all 0.4s;
        }

        .am.on {
            opacity: 1;
            transform: scale(1.5);
            filter: drop-shadow(0 0 8px rgba(34,197,94,0.9));
        }

        #dolphin-box {
            position: fixed;
            right: 12px; bottom: 45px;
            z-index: 15;
            text-align: center;
            font-size: 10px;
            letter-spacing: 1px;
        }

        #dolphin-emoji {
            font-size: 36px;
            display: block;
            animation: swim 3s ease-in-out infinite;
        }

        @keyframes swim {
            0%,100% { transform: translateX(-8px) rotate(-5deg); }
            50%      { transform: translateX(8px)  rotate(5deg);  }
        }

        #dolphin-txt { margin-top: 4px; transition: all 0.4s; }

        #accuracy-box {
            position: fixed;
            top: 85px; left: 50px;
            z-index: 15;
            background: rgba(17,24,39,0.85);
            border: 1px solid rgba(59,130,246,0.2);
            border-radius: 8px;
            padding: 8px 12px;
            font-size: 10px;
            letter-spacing: 1px;
        }

        #stats-bar {
            position: fixed;
            bottom: 0; left: 0; right: 0;
            height: 35px;
            background: rgba(5,7,10,0.95);
            border-top: 1px solid rgba(59,130,246,0.1);
            display: flex;
            align-items: center;
            padding: 0 60px 0 50px;
            gap: 25px;
            z-index: 15;
            font-size: 10px;
            letter-spacing: 1px;
            color: #374151;
        }

        .sv { color: #6b7280; font-weight: 700; }
        .sg { color: #22c55e; }
        .sr { color: #ef4444; }

        canvas {
            display: block;
            margin-top: 70px;
            background: #05070a;
        }
    </style>
</head>
<body>

<div id="header">
    <span class="logo">🍏</span>
    <div>
        <div class="title">KASPA WHALE ORACLE</div>
        <div style="font-size:9px;color:#374151;letter-spacing:2px;">
            5D PREDICTION ENGINE
        </div>
    </div>

    <div id="pred-box">
        <div style="color:#4b5563;margin-bottom:3px;">🔮 5-MIN PREDICTION</div>
        <span id="pred-direction">--</span>
        <span id="pred-pct" style="margin-left:8px;">--</span>
        <span id="pred-conf" style="margin-left:8px;color:#4b5563;">conf: --%</span>
    </div>

    <div id="price-box">
        <span id="price-apple">🍏</span>
        <div id="price">KAS $--</div>
    </div>
</div>

<div id="whale-alert"></div>

<div id="apple-meter">
    <div style="font-size:8px;color:#1f2937;writing-mode:vertical-rl;margin-bottom:4px;">SIGNAL</div>
    <div class="am" id="am5">🍏</div>
    <div class="am" id="am4">🍏</div>
    <div class="am" id="am3">🍋</div>
    <div class="am" id="am2">🍎</div>
    <div class="am" id="am1">🍎</div>
    <div style="font-size:8px;color:#1f2937;writing-mode:vertical-rl;margin-top:4px;">POWER</div>
</div>

<div id="accuracy-box">
    🎯 ACCURACY: <span id="acc-pct" style="color:#22c55e;">--%</span>
    <span id="acc-stats" style="color:#374151;">(0/0)</span>
</div>

<div id="dolphin-box">
    <span id="dolphin-emoji">🐬</span>
    <div id="dolphin-txt" style="color:#374151;">SCANNING...</div>
</div>

<div id="stats-bar">
    <span>🍏 BUYS: <span class="sv sg" id="s-buys">0</span></span>
    <span>🍎 SELLS: <span class="sv sr" id="s-sells">0</span></span>
    <span>🐳 WHALES: <span class="sv" id="s-whales">0</span></span>
    <span>📈 MOMENTUM: <span class="sv" id="s-mom">0%</span></span>
    <span>🕐 UPDATED: <span class="sv" id="s-time">--</span></span>
    <span style="margin-left:auto;color:#22c55e;">● LIVE</span>
</div>

<canvas id="chart"></canvas>

<script>
const canvas = document.getElementById('chart');
const ctx    = canvas.getContext('2d');

let lastPrice  = 0;
let prevPrice  = 0;
let lastWhalTs = null;

function resize() {
    canvas.width  = window.innerWidth;
    canvas.height = window.innerHeight - 70 - 35;
}
window.addEventListener('resize', resize);
resize();

function updateMeter(pct) {
    ['am1','am2','am3','am4','am5'].forEach(id =>
        document.getElementById(id).classList.remove('on')
    );
    let id = 'am3';
    if      (pct >=  1.5) id = 'am5';
    else if (pct >=  0.5) id = 'am4';
    else if (pct >= -0.5) id = 'am3';
    else if (pct >= -1.5) id = 'am2';
    else                  id = 'am1';
    document.getElementById(id).classList.add('on');
}

function updateDolphin(direction, whale) {
    const txt = document.getElementById('dolphin-txt');
    const em  = document.getElementById('dolphin-emoji');
    if (whale) {
        txt.innerText   = '🐳 WHALE!';
        txt.style.color = '#f59e0b';
        em.style.filter = 'drop-shadow(0 0 15px rgba(251,191,36,0.9))';
        return;
    }
    if (direction === 'UP') {
        txt.innerText   = '🍏 BUY';
        txt.style.color = '#22c55e';
        em.style.filter = 'drop-shadow(0 0 12px rgba(34,197,94,0.8))';
    } else if (direction === 'DOWN') {
        txt.innerText   = '🍎 SELL';
        txt.style.color = '#ef4444';
        em.style.filter = 'drop-shadow(0 0 12px rgba(239,68,68,0.8))';
    } else {
        txt.innerText   = '🌊 WAIT';
        txt.style.color = '#374151';
        em.style.filter = 'none';
    }
}

function showWhale(amount) {
    const el = document.getElementById('whale-alert');
    el.innerText        = `🐳 WHALE! ${Number(amount).toLocaleString()} KAS`;
    el.style.color      = '#f59e0b';
    el.style.textShadow = '0 0 20px rgba(251,191,36,0.9)';
    el.classList.add('show');
    setTimeout(() => el.classList.remove('show'), 4000);
}

function drawGrid() {
    ctx.strokeStyle = 'rgba(255,255,255,0.025)';
    ctx.lineWidth   = 1;
    for (let x = 0; x < canvas.width;  x += 60) {
        ctx.beginPath(); ctx.moveTo(x,0); ctx.lineTo(x,canvas.height); ctx.stroke();
    }
    for (let y = 0; y < canvas.height; y += 60) {
        ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(canvas.width,y); ctx.stroke();
    }
}

function drawCandles(trades) {
    if (trades.length < 2) return;
    const prices    = trades.map(t => t.price);
    const minP      = Math.min(...prices) * 0.9995;
    const maxP      = Math.max(...prices) * 1.0005;
    const range     = maxP - minP || 1;
    const pad       = canvas.height * 0.1;
    const groupSize = Math.max(1, Math.floor(trades.length / 60));
    const candles   = [];

    function toY(p) {
        return pad + (1 - (p - minP) / range) * (canvas.height - pad * 2);
    }

    for (let i = 0; i < trades.length; i += groupSize) {
        const group = trades.slice(i, i + groupSize);
        candles.push({
            open:  group[0].price,
            close: group[group.length-1].price,
            high:  Math.max(...group.map(t => t.price)),
            low:   Math.min(...group.map(t => t.price)),
            isUp:  group[group.length-1].price >= group[0].price
        });
    }

    candles.forEach((c, i) => {
        const x  = (i / Math.max(candles.length-1,1)) * canvas.width;
        const cW = Math.max(4, canvas.width / candles.length * 0.7);

        ctx.strokeStyle = c.isUp ? 'rgba(34,197,94,0.6)' : 'rgba(239,68,68,0.6)';
        ctx.lineWidth   = 1.5;
        ctx.beginPath();
        ctx.moveTo(x, toY(c.high));
        ctx.lineTo(x, toY(c.low));
        ctx.stroke();

        const bodyTop = Math.min(toY(c.open), toY(c.close));
        const bodyH   = Math.max(Math.abs(toY(c.close) - toY(c.open)), 2);
        const g = ctx.createLinearGradient(x-cW/2, bodyTop, x+cW/2, bodyTop+bodyH);

        if (c.isUp) {
            g.addColorStop(0,   '#86efac');
            g.addColorStop(0.5, '#22c55e');
            g.addColorStop(1,   '#052e16');
            ctx.shadowColor = 'rgba(34,197,94,0.4)';
        } else {
            g.addColorStop(0,   '#fca5a5');
            g.addColorStop(0.5, '#ef4444');
            g.addColorStop(1,   '#450a0a');
            ctx.shadowColor = 'rgba(239,68,68,0.4)';
        }
        ctx.shadowBlur = 8;
        ctx.fillStyle  = g;
        ctx.fillRect(x-cW/2, bodyTop, cW, bodyH);
        ctx.shadowBlur = 0;
    });
}

function drawBubbles(trades) {
    const prices = trades.map(t => t.price);
    const minP   = Math.min(...prices) * 0.9995;
    const maxP   = Math.max(...prices) * 1.0005;
    const range  = maxP - minP || 1;
    const pad    = canvas.height * 0.1;

    trades.forEach((t, i) => {
        if (!t.whale && t.value < 1000) return;
        const x = (i / Math.max(trades.length-1,1)) * canvas.width;
        const y = pad + (1-(t.price-minP)/range)*(canvas.height-pad*2);

        let r = Math.sqrt(t.value) / 20;
        r = Math.max(4, Math.min(r, 32));
        if (t.whale) r *= 2;

        const g = ctx.createRadialGradient(x-r*0.3, y-r*0.3, r*0.05, x, y, r);
        if (t.side === 'BUY') {
            g.addColorStop(0,   '#ffffff');
            g.addColorStop(0.2, '#86efac');
            g.addColorStop(0.6, '#22c55e');
            g.addColorStop(1,   '#052e16');
            ctx.shadowColor = t.whale ? 'rgba(34,197,94,1)' : 'rgba(34,197,94,0.4)';
        } else {
            g.addColorStop(0,   '#ffffff');
            g.addColorStop(0.2, '#fca5a5');
            g.addColorStop(0.6, '#ef4444');
            g.addColorStop(1,   '#450a0a');
            ctx.shadowColor = t.whale ? 'rgba