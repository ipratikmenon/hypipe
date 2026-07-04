"""
TradingView → Dhan Webhook Server
==================================
Receives TradingView alert POSTs, dynamically selects the best affordable
option from the weekly chain, and places the order via the Dhan REST API.

Flow
----
  TradingView alert (Pine Script alertcondition)
    └─→ POST https://<your-domain>/alert
          └─→ OptionSelector finds best affordable CE or PE
                └─→ DhanClient places MARKET order on NSE_FNO

Run
---
  python webhook_server.py

Requirements
------------
  - A publicly reachable URL (ngrok / VPS / cloud function)
  - TradingView Pro+ plan (webhook alerts need Pro or above)
  - WEBHOOK_SECRET set in .env — paste the same value into every TV alert message

IP whitelisting (mandatory per SEBI / Dhan)
-------------------------------------------
  Whitelist your server's outbound IP at:
    web.dhan.co → Profile → Access DhanHQ APIs → IP Whitelist
  Note: Dhan enforces a 7-day lock after each IP change.
"""

import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal, Optional

import pytz
import uvicorn
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from config import config
from dhan_client import DhanClient
from option_selector import OptionCandidate, OptionSelector

logger = logging.getLogger("webhook_server")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ── Module-level singletons ────────────────────────────────────────────────
_client: Optional[DhanClient] = None
_selector: Optional[OptionSelector] = None
_daily_pnl: float = 0.0
_open_position: Optional[dict] = None  # {"instrument": OptionCandidate, "side": str, "entry": float}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client, _selector
    _client = DhanClient()
    _selector = OptionSelector(_client, budget=config.MAX_LOSS_PER_TRADE)
    logger.info(
        "Webhook server ready — budget ₹%.0f/trade, indices: %s",
        config.MAX_LOSS_PER_TRADE,
        ", ".join(config.INDICES),
    )
    yield


app = FastAPI(title="Hypipe Webhook", version="1.0.0", lifespan=lifespan)


# ── Request / response models ──────────────────────────────────────────────

class AlertPayload(BaseModel):
    """
    JSON body TradingView posts when an alert fires.

    In TradingView, set the alert "Message" field to:

      BUY signal (bullish crossover):
        {"signal":"BUY","option_type":"CE","index":"NIFTY","price":{{close}},"secret":"YOUR_SECRET"}

      SELL signal (bearish crossover):
        {"signal":"SELL","option_type":"PE","index":"NIFTY","price":{{close}},"secret":"YOUR_SECRET"}

      Exit signal:
        {"signal":"EXIT","price":{{close}},"secret":"YOUR_SECRET"}

    Replace YOUR_SECRET with the value of WEBHOOK_SECRET in your .env.
    The {{close}} placeholder is filled by TradingView with the current bar's close price.
    """
    signal: Literal["BUY", "SELL", "EXIT"]
    option_type: Literal["CE", "PE"] = "CE"
    index: Optional[str] = None        # e.g. "NIFTY" — narrows selector to one index
    price: float = 0.0                 # underlying close from TradingView {{close}}
    secret: str = ""


class OrderResult(BaseModel):
    status: str
    signal: str
    instrument: Optional[str] = None
    order_id: Optional[str] = None
    message: str = ""


# ── Auth helper ────────────────────────────────────────────────────────────

def _check_secret(provided: str) -> None:
    """Constant-time comparison to prevent timing attacks."""
    if not config.WEBHOOK_SECRET:
        return  # secret not configured → skip check (dev mode)
    if not secrets.compare_digest(provided, config.WEBHOOK_SECRET):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid secret")


# ── Market hours guard ─────────────────────────────────────────────────────

def _market_open() -> bool:
    tz = pytz.timezone(config.TIMEZONE)
    now = datetime.now(tz)
    oh, om = map(int, config.MARKET_OPEN.split(":"))
    ch, cm = map(int, config.TRADE_CUTOFF.split(":"))
    opens = now.replace(hour=oh, minute=om, second=0, microsecond=0)
    cuts  = now.replace(hour=ch, minute=cm, second=0, microsecond=0)
    return opens <= now <= cuts


# ── Option selection logic ─────────────────────────────────────────────────

def _pick_best_option(
    option_type: str,
    index_hint: Optional[str],
    underlying_price: float,
) -> Optional[OptionCandidate]:
    """
    From the affordable candidate list, pick the option closest to ATM
    with meaningful open interest.  Index hint narrows to a single index
    when the TradingView chart is on a specific underlying.
    """
    indices = [index_hint.upper()] if index_hint else None
    candidates = _selector.get_affordable_options(
        option_types=(option_type,),
        indices=indices,
    )

    if not candidates:
        return None

    # Rank by: proximity to ATM (if underlying price known) then by OI
    def rank(c: OptionCandidate) -> tuple:
        atm_dist = abs(c.strike - underlying_price) if underlying_price > 0 else 0
        return (atm_dist, -c.oi)

    candidates.sort(key=rank)
    return candidates[0]


# ── Routes ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "market_open": _market_open()}


@app.post("/alert", response_model=OrderResult)
async def receive_alert(payload: AlertPayload, request: Request):
    global _daily_pnl, _open_position

    _check_secret(payload.secret)

    logger.info(
        "Alert received: signal=%s option_type=%s index=%s price=%.2f",
        payload.signal, payload.option_type, payload.index, payload.price,
    )

    # ── EXIT ──────────────────────────────────────────────────────────────
    if payload.signal == "EXIT":
        if not _open_position:
            return OrderResult(status="ok", signal="EXIT", message="No open position to exit")

        instrument: OptionCandidate = _open_position["instrument"]
        side = _open_position["side"]
        entry = _open_position["entry"]

        tx = "SELL" if side == "LONG" else "BUY"
        resp = _client.place_market_order(
            security_id=instrument.security_id,
            exchange_segment=instrument.exchange_segment,
            transaction_type=tx,
            quantity=instrument.lot_size,
            product_type=config.PRODUCT_TYPE,
        )
        ltp = payload.price or entry
        pnl = (ltp - entry) * instrument.lot_size if side == "LONG" else (entry - ltp) * instrument.lot_size
        _daily_pnl += pnl
        _open_position = None

        logger.info("EXIT %s | PnL=₹%.2f | Daily PnL=₹%.2f", instrument, pnl, _daily_pnl)
        return OrderResult(
            status="ok", signal="EXIT",
            instrument=str(instrument),
            order_id=resp.get("orderId", ""),
            message=f"Exited. PnL ₹{pnl:.2f}",
        )

    # ── Market hours check ─────────────────────────────────────────────────
    if not _market_open():
        logger.warning("Alert received outside market hours — ignored.")
        return OrderResult(status="ignored", signal=payload.signal, message="Outside market hours")

    # ── Daily loss guard ───────────────────────────────────────────────────
    if _daily_pnl <= -config.MAX_DAILY_LOSS:
        logger.warning("Daily loss limit hit — ignoring new entry signal.")
        return OrderResult(status="ignored", signal=payload.signal, message="Daily loss limit reached")

    # ── Already in a position ──────────────────────────────────────────────
    if _open_position:
        return OrderResult(
            status="ignored", signal=payload.signal,
            message=f"Already holding {_open_position['instrument']}",
        )

    # ── Select option ──────────────────────────────────────────────────────
    # BUY signal → buy a CALL (CE); SELL signal → buy a PUT (PE)
    opt_type = "CE" if payload.signal == "BUY" else "PE"
    if payload.option_type:
        opt_type = payload.option_type  # allow explicit override in alert

    instrument = _pick_best_option(opt_type, payload.index, payload.price)
    if not instrument:
        msg = f"No affordable {opt_type} found within ₹{config.MAX_LOSS_PER_TRADE:.0f} budget"
        logger.warning(msg)
        return OrderResult(status="error", signal=payload.signal, message=msg)

    logger.info("Selected: %s", instrument)

    # ── Place order ────────────────────────────────────────────────────────
    resp = _client.place_market_order(
        security_id=instrument.security_id,
        exchange_segment=instrument.exchange_segment,
        transaction_type="BUY",   # we always BUY options (CE on bullish, PE on bearish)
        quantity=instrument.lot_size,
        product_type=config.PRODUCT_TYPE,
    )

    order_id = resp.get("orderId", "")
    order_status = resp.get("orderStatus", "")
    logger.info("Order placed: id=%s status=%s", order_id, order_status)

    _open_position = {
        "instrument": instrument,
        "side": "LONG",
        "entry": instrument.ltp,
    }

    return OrderResult(
        status="ok",
        signal=payload.signal,
        instrument=str(instrument),
        order_id=order_id,
        message=f"Order {order_status} | cost ₹{instrument.cost_per_lot:.0f}",
    )


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    logger.error("Unhandled error: %s", exc, exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": str(exc)},
    )


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "webhook_server:app",
        host=config.WEBHOOK_HOST,
        port=config.WEBHOOK_PORT,
        log_level="info",
        reload=False,
    )
