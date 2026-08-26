import json
import asyncio
import os
from datetime import datetime, timezone
from decimal import Decimal
from auth import (signup, login, update_whatsapp_optin, get_current_user,
                  set_experience_level, advance_onboarding, get_onboarding_state,
                  verify_upstox_account, record_stock360s_purchase)
from crypto import (execute_crypto_order, check_crypto_liquidations,
                    check_crypto_funding, check_pending_orders, check_crypto_sl_tp)
from forex import (execute_forex_order, check_forex_liquidations,
                   check_forex_swap, check_forex_sl_tp, check_forex_pending_orders)
import common
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse,RedirectResponse
from fastapi.staticfiles import StaticFiles
import common
from common import init_db, logger
from data_generator import market_engine
from crypto import (execute_crypto_order, check_crypto_liquidations,check_crypto_funding, check_pending_orders)
from forex import (execute_forex_order, check_forex_liquidations, check_forex_swap)
from auth import (signup, login, update_whatsapp_optin, get_current_user,set_experience_level, advance_onboarding, get_onboarding_state)
from data_generator import market_engine
import crypto

app = FastAPI()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
frontend_dir = os.path.abspath(os.path.join(BASE_DIR, '..', 'frontend'))

if os.path.isdir(frontend_dir):
    app.mount("/static", StaticFiles(directory=frontend_dir), name="static")
    logger.info(f"Serving static files from: {frontend_dir}")
else:
    logger.error(f"Frontend directory does not exist at: {frontend_dir}")


# ═══════════════════════════════════════════════════════════════
#  STARTUP & BACKGROUND TASKS
# ═══════════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup_event():
    await init_db()
    market_engine.init_symbols()
    asyncio.create_task(background_market_task())
    asyncio.create_task(background_risk_task())
    asyncio.create_task(background_funding_swap_task())
    asyncio.create_task(background_pending_orders_task())
    asyncio.create_task(background_analytics_snapshot_task())
    asyncio.create_task(background_daily_rules_task())
    asyncio.create_task(background_assessment_task())
    asyncio.create_task(background_stock360s_confirm_task())
    asyncio.create_task(background_sl_tp_task())

async def background_market_task():
    """1-second price updates + candle saves."""
    while True:
        await market_engine.tick()
        await asyncio.sleep(1)


async def background_risk_task():
    """Check liquidations and margin calls every 2 seconds."""
    while True:
        await check_crypto_liquidations()
        await check_forex_liquidations()
        await asyncio.sleep(2)


async def background_funding_swap_task():
    """Check funding/swap timing every 5 seconds."""
    while True:
        await check_crypto_funding()
        await check_forex_swap()
        await asyncio.sleep(5)


async def background_pending_orders_task():
    """Check pending limit/stop orders every second."""
    while True:
        await check_pending_orders()          # crypto
        await check_forex_pending_orders()    # forex
        await asyncio.sleep(1)


async def background_analytics_snapshot_task():
    """Save equity snapshot every 30s for Sharpe/Sortino/MaxDD."""
    while True:
        try:
            async with common.db_pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT user_id, equity FROM users_state")
                    rows = await cur.fetchall()
                    for uid, eq in rows:
                        try:
                            await cur.execute(
                                "INSERT INTO equity_snapshots (user_id, equity) VALUES (%s,%s)",
                                (uid, eq)
                            )
                        except Exception as row_err:
                            logger.error(f"[Analytics Snapshot] Row error: {row_err}")
                            continue
        except Exception as e:
            logger.error(f"[Analytics Snapshot] Error: {e}")
        await asyncio.sleep(30)

async def background_daily_rules_task():
    """Daily 00:00 UTC reset of daily_start_balance + assessment progress check."""
    last_reset_day = None
    while True:
        try:
            now = datetime.now(timezone.utc)
            # ── Daily reset at 00:00 UTC ──
            today_str = now.strftime("%Y-%m-%d")
            if now.hour == 0 and now.minute < 5 and last_reset_day != today_str:
                async with common.db_pool.acquire() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "UPDATE users_state SET daily_start_balance = step_start_balance "
                            "WHERE assessment_completed=FALSE"
                        )
                last_reset_day = today_str
                logger.info("[Daily Reset] daily_start_balance reset to step_start_balance")
        except Exception as e:
            logger.error(f"[Daily Rules] {e}")
        await asyncio.sleep(60)

async def background_assessment_task():
    """Check profit targets and daily/max drawdown for all active users."""
    while True:
        try:
            async with common.db_pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """SELECT user_id, assessment_step, step_start_balance,
                                  daily_start_balance, balance, equity
                           FROM users_state
                           WHERE assessment_completed=FALSE AND upstox_verified=TRUE"""
                    )
                    users = await cur.fetchall()
                    for uid, step, step_start, daily_start, bal, eq in users:
                        step = int(step)
                        target_mult = common.ASSESSMENT_TARGETS.get(step, 1.10)
                        target = float(step_start) * target_mult
                        equity = float(eq)

                        # Daily DD breach check
                        min_equity_today = float(daily_start) * (1 - common.ASSESSMENT_DAILY_DD_PCT)
                        if equity < min_equity_today:
                            await cur.execute(
                                "UPDATE users_state SET max_drawdown_breached=TRUE "
                                "WHERE user_id=%s", (uid,)
                            )
                            logger.warning(f"[Assessment] {uid} daily DD breached")
                        await cur.execute(
                            "SELECT peak_equity FROM users_state WHERE user_id=%s", (uid,)
                        )
                        peak_row = await cur.fetchone()
                        peak_equity = float(peak_row[0]) if peak_row else float(step_start)
                        if peak_equity > 0:
                            overall_dd_pct = (peak_equity - equity) / peak_equity
                            if overall_dd_pct >= common.ASSESSMENT_MAX_DD_PCT:
                                await cur.execute(
                                    "UPDATE users_state SET max_drawdown_breached=TRUE "
                                    "WHERE user_id=%s", (uid,)
                                )
                                logger.error(f"[Assessment] {uid} overall max DD breached: {overall_dd_pct*100:.1f}%")
                        # Target hit
                        if equity >= target:
                            await advance_onboarding(uid, step, method='profit')
                            logger.info(f"[Assessment] {uid} hit step {step} target")
        except Exception as e:
            logger.error(f"[Assessment Task] {e}")
        await asyncio.sleep(10)

async def background_stock360s_confirm_task():
    """Auto-confirm Stock360s purchases after 1 hour and advance step."""
    while True:
        try:
            async with common.db_pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """SELECT user_id, assessment_step, stock360s_1mo_purchased,
                                  stock360s_1yr_purchased, stock360s_purchase_time
                           FROM users_state
                           WHERE stock360s_purchase_time IS NOT NULL
                             AND stock360s_confirmed=FALSE
                             AND stock360s_purchase_time < (NOW() - INTERVAL 1 HOUR)"""
                    )
                    rows = await cur.fetchall()
                    for uid, step, mo, yr, ptime in rows:
                        await cur.execute(
                            "UPDATE users_state SET stock360s_confirmed=TRUE WHERE user_id=%s",
                            (uid,)
                        )
                        if yr:
                            # Skip directly to step 3 (regardless of current step)
                            if int(step) < 3:
                                await cur.execute(
                                    "UPDATE users_state SET assessment_step=3, "
                                    "step_start_balance=100000.00, balance=100000.00, "
                                    "daily_start_balance=100000.00, equity=100000.00 "
                                    "WHERE user_id=%s", (uid,)
                                )
                                logger.info(f"[Stock360s] {uid} → step 3 (1yr)")
                        elif mo and int(step) == 1:
                            await advance_onboarding(uid, 1, method='stock360s_1mo')
                            logger.info(f"[Stock360s] {uid} → step 2 (1mo)")
        except Exception as e:
            logger.error(f"[Stock360s confirm] {e}")
        await asyncio.sleep(60)

async def background_sl_tp_task():
    while True:
        try:
            await check_crypto_sl_tp()
            await check_forex_sl_tp()
        except Exception as e:
            logger.error(f"[SL/TP task] {e}")
        await asyncio.sleep(1)

@app.post("/api/auth/signup")
async def api_signup(request: Request):
    data = await request.json()
    result = await signup(
        username=data['username'],
        name=data['name'],
        password=data['password'],
        whatsapp_opt_in=data.get('whatsapp_opt_in', False),
        whatsapp_number=data.get('whatsapp_number')
    )
    if "error" in result:
        return JSONResponse(result, status_code=400)
    response = JSONResponse(result)
    response.set_cookie(
        key="propind_user", value=result['user_id'],
        httponly=True, samesite="lax", secure=os.getenv('HTTPS_ENABLED', 'false').lower() == 'true'
    )
    return response


@app.post("/api/auth/login")
async def api_login(request: Request):
    data = await request.json()
    result = await login(data['username'], data['password'])
    if "error" in result:
        return JSONResponse(result, status_code=401)
    response = JSONResponse(result)
    response.set_cookie(
        key="propind_user", value=result['user_id'],
        httponly=True, samesite="lax", secure=os.getenv('HTTPS_ENABLED', 'false').lower() == 'true'
    )
    return response

@app.post("/api/auth/upstox-verify")
async def api_upstox_verify(request: Request):
    user = await get_current_user(request)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    result = await verify_upstox_account(user['user_id'])
    return JSONResponse(result)

@app.post("/api/stock360s/purchase")
async def api_stock360s_purchase(request: Request):
    user = await get_current_user(request)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    data = await request.json()
    result = await record_stock360s_purchase(user['user_id'], data['plan'])
    return JSONResponse(result)

@app.get("/upstox-verify")
async def upstox_verify_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/")
    state = await get_onboarding_state(user['user_id'])
    is_pending = state and state.get('upstox_verify_request_time') and not state.get('upstox_verified')
    html_path = os.path.join(frontend_dir, "upstox_verify.html")
    if not os.path.exists(html_path):
        if is_pending:
            return HTMLResponse("<h1>Verification Pending</h1><p>Please wait up to 1 hour for your account to be fully verified.</p><p>Your trading session will start automatically.</p>")
        return HTMLResponse("<h1>Open your Upstox account</h1>"
                             f"<a href='{common.UPSTOX_REFERRAL_URL}'>Open Upstox</a><br>"
                             "<button onclick='fetch(\"/api/auth/upstox-verify\",{method:\"POST\"})"
                             ".then(()=>location.reload())'>I've opened my account, Please Verify</button>")  
    with open(html_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.post("/api/auth/whatsapp")
async def api_whatsapp(request: Request):
    user = await get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    data = await request.json()
    result = await update_whatsapp_optin(
        user['user_id'],
        data.get('opt_in', False),
        data.get('whatsapp_number')
    )
    return JSONResponse(result)

@app.get("/api/auth/me")
async def api_me(request: Request):
    user = await get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    return JSONResponse(user)


@app.post("/api/auth/logout")
async def api_logout():
    response = JSONResponse({"status": "logged_out"})
    response.delete_cookie("propind_user")
    return response

@app.get("/")
async def get_landing(request: Request):
    user = await get_current_user(request)
    if user:
        state = await get_onboarding_state(user['user_id'])
        if state and not state['onboarding_completed']:
            return RedirectResponse(url="/onboarding")
        return RedirectResponse(url="/dashboard")
    html_path = os.path.join(frontend_dir, "landing.html")
    if not os.path.exists(html_path):
        return HTMLResponse("<h1>Landing page not found</h1>", status_code=404)
    with open(html_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/dashboard")
async def get_dashboard(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/")
    state = await get_onboarding_state(user['user_id'])
    # Block 1: experience not set → onboarding
    if state and not state['experience_level']:
        return RedirectResponse(url="/onboarding")
    # Block 2: Upstox not verified → upstox_verify
    if state and not state['upstox_verified']:
        return RedirectResponse(url="/upstox-verify")
    # Block 3: assessment not started → onboarding (for experience step only)
    # Otherwise → dashboard
    html_path = os.path.join(frontend_dir, "index.html")
    if not os.path.exists(html_path):
        return HTMLResponse("<h1>Frontend not found</h1>", status_code=404)
    with open(html_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/api/state")
async def get_state(request: Request):
    """Full account state + open positions — the risk dashboard."""
    user = await get_current_user(request)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    user_id = user['user_id']

    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT balance, equity, used_margin, free_margin, margin_level,
                          realized_pnl, unrealized_pnl, total_fees, total_funding,
                          total_swap, peak_equity, starting_balance
                   FROM users_state WHERE user_id=%s""",
                (user_id,)
            )
            ud = await cur.fetchone()
            if not ud:
                return JSONResponse({"error": "Account not found"}, status_code=404)

            await cur.execute(
                """SELECT id, symbol, side, size, filled_size, avg_fill_price, status,
                          asset_class, leverage, margin_mode, liquidation_price,
                          take_profit_price, stop_loss_price, initial_margin,
                          maintenance_margin, order_type, trigger_price
                   FROM trades_orders 
                   WHERE user_id=%s AND status IN ('OPEN','PARTIAL','PENDING','MARGIN_CALL')
                   ORDER BY created_at DESC""",
                (user_id,)
            )
            raw_positions = await cur.fetchall()

    positions = []
    for p in raw_positions:
        pos_id, symbol, side, size, filled_size, entry_price, status, \
            asset_class, leverage, margin_mode, liquidation_price, \
            take_profit_price, stop_loss_price, initial_margin, \
            maintenance_margin, order_type, trigger_price = p

        actual_size = float(filled_size) if filled_size else float(size)
        entry = float(entry_price)
        mark_price = 0.0
        pnl = 0.0
        if symbol in market_engine.state and actual_size > 0:
            state = market_engine.state[symbol]
            if asset_class == 'crypto':
                mark_price = float(state['mark'])
            else: # forex
                mark_price = float(state['bid']) if side == 'buy' else float(state['ask'])
            
            if side == 'buy':
                pnl = (mark_price - entry) * actual_size
            else:
                pnl = (entry - mark_price) * actual_size
        positions.append({
            "id": pos_id, "symbol": symbol, "side": side, "size": actual_size,
            "filled_size": float(filled_size) if filled_size else 0.0, 
            "entry_price": entry,
            "status": status, "asset_class": asset_class, "leverage": leverage,
            "margin_mode": margin_mode, "liquidation_price": float(liquidation_price),
            "take_profit": float(take_profit_price), "stop_loss": float(stop_loss_price),
            "initial_margin": float(initial_margin), "maintenance_margin": float(maintenance_margin),
            "order_type": order_type, "trigger_price": float(trigger_price) if trigger_price else None,
            "mark_price": mark_price,  # Added to payload
            "pnl": pnl                 # Added to payload
        })
    peak = float(ud[10]) if float(ud[10]) > 0 else float(ud[11])
    equity = float(ud[1])
    drawdown = round((1 - equity / peak) * 100, 2) if peak > 0 else 0
    return JSONResponse({
        "user": user,
        "account": {
            "balance": float(ud[0]),
            "equity": float(ud[1]),
            "used_margin": float(ud[2]),
            "free_margin": float(ud[3]),
            "margin_level": float(ud[4]),
            "realized_pnl": float(ud[5]),
            "unrealized_pnl": float(ud[6]),
            "fees": float(ud[7]),
            "funding": float(ud[8]),
            "swap": float(ud[9]),
            "peak_equity": float(ud[10]),
            "starting_balance": float(ud[11]),
            "drawdown": drawdown
        },
        "positions": positions
    })

# ═══════════════════════════════════════════════════════════════
#  ONBOARDING
# ═══════════════════════════════════════════════════════════════
@app.get("/onboarding")
async def get_onboarding_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/")
    state = await get_onboarding_state(user['user_id'])
    if state and state['onboarding_completed']:
        return RedirectResponse(url="/dashboard")
    html_path = os.path.join(frontend_dir, "onboarding.html")
    if not os.path.exists(html_path):
        return HTMLResponse("<h1>Onboarding page not found</h1>", status_code=404)
    with open(html_path, "r", encoding="utf-8") as f:   # 👈 ADD encoding="utf-8"
        return HTMLResponse(content=f.read())


@app.get("/api/onboarding/state")
async def api_onboarding_state(request: Request):
    user = await get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    state = await get_onboarding_state(user['user_id'])
    if not state:
        return JSONResponse({"error": "State not found"}, status_code=404)
    return JSONResponse(state)


@app.post("/api/onboarding/experience")
async def api_set_experience(request: Request):
    user = await get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    data = await request.json()
    result = await set_experience_level(user['user_id'], data['level'])
    return JSONResponse(result)


@app.post("/api/onboarding/advance")
async def api_advance_onboarding(request: Request):
    user = await get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    data = await request.json()
    result = await advance_onboarding(
        user['user_id'], data['step'], data.get('method')
    )
    return JSONResponse(result)

@app.get("/api/candles")
async def get_candles(symbol: str, timeframe: str, limit: int = 100):
    if timeframe not in common.ALLOWED_TIMEFRAMES:
        return JSONResponse({"error": f"Invalid timeframe. Allowed: {common.ALLOWED_TIMEFRAMES}"}, status_code=400)

    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT open, high, low, close, timestamp 
                   FROM candles 
                   WHERE symbol=%s AND timeframe=%s 
                   ORDER BY timestamp DESC LIMIT %s""",
                (symbol, timeframe, limit)
            )
            rows = await cur.fetchall()
    candles = [{"open": float(r[0]), "high": float(r[1]), "low": float(r[2]),
                "close": float(r[3]), "timestamp": str(r[4])}
               for r in reversed(rows)]
    return JSONResponse({"symbol": symbol, "timeframe": timeframe, "candles": candles})

@app.get("/api/options")
async def get_options_chain(symbol: str):
    """Generate options chain with dynamic IV smile."""
    if symbol not in market_engine.state:
        return JSONResponse({"error": "Symbol not found"}, status_code=404)
    chain = market_engine.generate_options_chain(symbol)
    spot = market_engine.state[symbol]['index']
    return JSONResponse({
        "symbol": symbol,
        "spot": spot,
        "chain": chain
    })

@app.get("/api/symbols")
async def get_symbols():
    """List all available symbols grouped by asset class."""
    return JSONResponse({
        "crypto": [s for s, d in market_engine.state.items() if d['asset'] == 'crypto'],
        "forex":  [s for s, d in market_engine.state.items() if d['asset'] == 'forex']
    })


@app.post("/api/order")
async def place_order(request: Request):
    user = await get_current_user(request)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    user_id = user['user_id']
    data = await request.json()

    # ── Pre-trade gates ──
    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT upstox_verified, assessment_completed, max_drawdown_breached, "
                "balance, daily_start_balance FROM users_state WHERE user_id=%s",
                (user_id,)
            )
            row = await cur.fetchone()
            if not row:
                return JSONResponse({"error": "State missing"}, status_code=400)
            upstox_ok, completed, dd_breached, bal, daily_start = row
            if not upstox_ok:
                return JSONResponse({"error": "Please open your Upstox account first."}, status_code=403)
            if completed:
                return JSONResponse({"error": "Assessment completed. Trading disabled."}, status_code=403)
            if dd_breached:
                return JSONResponse({"error": "Daily drawdown limit breached. Try again tomorrow."}, status_code=403)
            # Hard daily-DD check at order entry
            if float(bal) < float(daily_start) * (1 - common.ASSESSMENT_DAILY_DD_PCT):
                return JSONResponse({"error": "Daily drawdown limit reached."}, status_code=403)

    # Continue with existing execute_crypto_order/execute_forex_order calls …
    if data['asset_class'] == 'crypto':
        result = await execute_crypto_order(
            user_id=user_id,
            symbol=data['symbol'],
            side=data['side'],
            size=float(data['size']),
            order_type=data.get('order_type', 'market'),
            tif=data.get('tif', 'GTC'),
            leverage=int(data.get('leverage', 1)),
            margin_mode=data.get('margin_mode', 'cross'),
            trigger_price=data.get('trigger_price'),
            take_profit=data.get('take_profit'),
            stop_loss=data.get('stop_loss'),
            trailing_distance=data.get('trailing_distance')
        )
    elif data['asset_class'] == 'forex':
        result = await execute_forex_order(
            user_id=user_id,
            symbol=data['symbol'],
            side=data['side'],
            lots=float(data['size']),
            order_type=data.get('order_type', 'market'),
            lot_type=data.get('lot_type', 'standard'),
            tif=data.get('tif', 'GTC'),
            leverage=int(data.get('leverage', 100)),
            margin_mode=data.get('margin_mode', 'cross'),
            trigger_price=data.get('trigger_price'),
            take_profit=data.get('take_profit'),
            stop_loss=data.get('stop_loss'),
            trailing_distance=data.get('trailing_distance')
        )
    else:
        return JSONResponse({"error": "Invalid asset_class"}, status_code=400)
    return JSONResponse(result)


@app.post("/api/close/{position_id}")
async def close_position(position_id: int, request: Request):
    user = await get_current_user(request)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    user_id = user['user_id']
    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            # Check assessment completed
            await cur.execute(
                "SELECT assessment_completed FROM users_state WHERE user_id=%s",
                (user_id,)
            )
            row = await cur.fetchone()
            if row and bool(row[0]):
                return JSONResponse({"error": "Assessment completed."}, status_code=403)

            # Fetch the actual position
            await cur.execute(
                """SELECT id, asset_class, symbol, side, size, filled_size,
                          avg_fill_price, status, leverage, margin_mode,
                          initial_margin, maker_fee, taker_fee, funding_paid,
                          swap_paid, opened_at
                   FROM trades_orders
                   WHERE id=%s AND user_id=%s AND status IN ('OPEN','PARTIAL','MARGIN_CALL')""",
                (position_id, user_id)
            )
            pos = await cur.fetchone()
            if not pos:
                return JSONResponse({"error": "Position not found"}, status_code=404)

            pos_id, asset_class, sym, side, size, filled_size, entry, status, \
                leverage, margin_mode, init_margin, maker_fee, taker_fee, \
                funding, swap, opened_at = pos

            state = market_engine.state[sym]
            if asset_class == 'crypto':
                current = state['mark']
            else:
                current = state['bid'] if side == 'buy' else state['ask']

            actual_size = float(filled_size) if filled_size else float(size)
            if side == 'buy':
                realized = (current - float(entry)) * actual_size
            else:
                realized = (float(entry) - current) * actual_size

            total_fees = float(maker_fee) + float(taker_fee)
            total_funding = float(funding) if funding else 0
            total_swap = float(swap) if swap else 0
            margin_used = float(init_margin) if init_margin else 0

            await cur.execute(
                """INSERT INTO closed_trades
                (user_id, symbol, asset_class, side, size, entry_price, exit_price,
                 leverage, margin_mode, realized_pnl, fees, funding, swap, close_reason, opened_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'manual',%s)""",
                (user_id, sym, asset_class, side, Decimal(str(actual_size)),
                 Decimal(str(entry)), Decimal(str(current)), leverage, margin_mode,
                 Decimal(str(round(realized, 2))), Decimal(str(round(total_fees, 2))),
                 Decimal(str(round(total_funding, 2))), Decimal(str(round(total_swap, 2))),
                 opened_at if opened_at else datetime.now(timezone.utc))
            )
            await cur.execute(
                """UPDATE trades_orders
                   SET status='FILLED', closed_at=%s, close_reason='manual', realized_pnl=%s
                   WHERE id=%s""",
                (datetime.now(timezone.utc), Decimal(str(round(realized, 2))), pos_id)
            )
            await cur.execute(
                """UPDATE users_state
                   SET balance=balance+%s, used_margin=used_margin-%s,
                       realized_pnl=realized_pnl+%s
                   WHERE user_id=%s""",
                (Decimal(str(round(realized, 2))), Decimal(str(round(margin_used, 2))),
                 Decimal(str(round(realized, 2))), user_id)
            )
            # ── Recalculate equity after manual close ──
            from crypto import execute_crypto_order
            await cur.execute(
                f"SELECT {crypto.ORDER_FIELDS} FROM trades_orders "
                f"WHERE user_id=%s AND status IN ('OPEN','PARTIAL','MARGIN_CALL')",
                (user_id,)
            )
            remaining = await cur.fetchall()
            total_unrealized = 0
            for rp in remaining:
                rp_sym = rp[3]
                rp_side = rp[4]
                rp_entry = float(rp[9])
                rp_size = float(rp[8])
                if rp_sym in market_engine.state:
                    rp_state = market_engine.state[rp_sym]
                    if rp_state['asset'] == 'crypto':
                        rp_current = rp_state['mark']
                    else:
                        rp_current = rp_state['bid'] if rp_side == 'buy' else rp_state['ask']
                    if rp_side == 'buy':
                        total_unrealized += (rp_current - rp_entry) * rp_size
                    else:
                        total_unrealized += (rp_entry - rp_current) * rp_size
            await cur.execute(
                "SELECT balance FROM users_state WHERE user_id=%s", (user_id,)
            )
            bal_row = await cur.fetchone()
            new_equity = float(bal_row[0]) + total_unrealized
            await cur.execute(
                "UPDATE users_state SET equity=%s, unrealized_pnl=%s, "
                "free_margin=equity-used_margin WHERE user_id=%s",
                (Decimal(str(round(new_equity, 2))),
                 Decimal(str(round(total_unrealized, 2))), user_id)
            )

    logger.info(f"[Manual Close] Position {pos_id} {sym} closed. Realized: ${realized:.2f}")
    return JSONResponse({"status": "closed", "realized_pnl": round(realized, 2), "exit_price": current})

@app.get("/api/analytics")
async def get_analytics(request: Request):
    """Performance analytics: win rate, profit factor, Sharpe, Sortino, MaxDD, behavioral."""
    user = await get_current_user(request)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    user_id = user['user_id']

    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT realized_pnl, fees, funding, swap, leverage, 
                          opened_at, closed_at, side, close_reason, symbol
                   FROM closed_trades WHERE user_id=%s ORDER BY closed_at ASC""",
                (user_id,)
            )
            trades = await cur.fetchall()

            await cur.execute(
                "SELECT equity, timestamp FROM equity_snapshots WHERE user_id=%s ORDER BY timestamp ASC",
                (user_id,)
            )
            snapshots = await cur.fetchall()

    if not trades:
        return JSONResponse({
            "total_trades": 0, "win_rate": 0, "profit_factor": 0,
            "expectancy": 0, "max_drawdown": 0, "sharpe": 0, "sortino": 0
        })

    pnls = [float(t[0]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    win_rate = (len(wins) / len(pnls) * 100) if pnls else 0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 999
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    expectancy = (win_rate / 100 * avg_win) - ((100 - win_rate) / 100 * abs(avg_loss))

    # Max consecutive losses
    max_consec_loss = 0
    curr_streak = 0
    for p in pnls:
        if p < 0:
            curr_streak += 1
            max_consec_loss = max(max_consec_loss, curr_streak)
        else:
            curr_streak = 0

    # Holding times
    hold_times = []
    for t in trades:
        if t[5] and t[6]:
            hold_times.append((t[6] - t[5]).total_seconds() / 60)
    avg_hold = sum(hold_times) / len(hold_times) if hold_times else 0

    # Max drawdown from equity snapshots
    peak_eq = 0
    max_dd = 0
    for snap in snapshots:
        eq = float(snap[0])
        peak_eq = max(peak_eq, eq)
        dd = ((peak_eq - eq) / peak_eq * 100) if peak_eq > 0 else 0
        max_dd = max(max_dd, dd)

    # Sharpe & Sortino from snapshot returns
    returns = []
    for i in range(1, len(snapshots)):
        prev = float(snapshots[i - 1][0])
        curr_eq = float(snapshots[i][0])
        if prev > 0:
            returns.append((curr_eq - prev) / prev)

    if returns:
        mean_r = sum(returns) / len(returns)
        std_r = (sum((r - mean_r) ** 2 for r in returns) / len(returns)) ** 0.5
        # Annualize from 30s snapshots: 365*24*120 = ~1,051,200 snapshots/year
        annual_factor = (365 * 24 * 120) ** 0.5
        sharpe = (mean_r / std_r * annual_factor) if std_r > 0 else 0
        downside = [r for r in returns if r < 0]
        std_d = (sum(r ** 2 for r in downside) / len(downside)) ** 0.5 if downside else 0
        sortino = (mean_r / std_d * annual_factor) if std_d > 0 else 0
    else:
        sharpe = sortino = 0

    # ── Behavioral metrics ──
    avg_leverage = sum(float(t[4]) for t in trades) / len(trades) if trades else 0
    revenge_trades = 0
    for i in range(1, len(trades)):
        if (trades[i-1][0] < 0 and 
            trades[i][8] == 'manual' and 
            trades[i-1][6] and trades[i][5] and
            (trades[i][6] - trades[i-1][6]).total_seconds() < 300):
            revenge_trades += 1

    # Overtrading: more than 10 trades in a single day
    overtrading_days = 0
    day_counts = {}
    for t in trades:
        if t[6]:
            day = str(t[6].date())
            day_counts[day] = day_counts.get(day, 0) + 1
    overtrading_days = sum(1 for c in day_counts.values() if c > 10)

    # Stop-loss adherence: % of manual closes that had a stop loss set
    # (would need to check original orders — simplified here)
    liquidation_count = sum(1 for t in trades if t[8] == 'liquidation')
    stop_out_count = sum(1 for t in trades if t[8] == 'stop_out')

    return JSONResponse({
        "total_trades": len(trades),
        "win_rate": round(win_rate, 2),
        "profit_factor": round(profit_factor, 2),
        "expectancy": round(expectancy, 2),
        "avg_winner": round(avg_win, 2),
        "avg_loser": round(avg_loss, 2),
        "max_drawdown": round(max_dd, 2),
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "avg_holding_time_min": round(avg_hold, 2),
        "largest_loss": round(min(pnls), 2) if pnls else 0,
        "consecutive_losses": max_consec_loss,
        "total_pnl": round(sum(pnls), 2),
        "total_fees": round(sum(float(t[1]) for t in trades), 2),
        "total_funding": round(sum(float(t[2]) for t in trades), 2),
        "total_swap": round(sum(float(t[3]) for t in trades), 2),
        "liquidations": liquidation_count,
        "stop_outs": stop_out_count,
        "behavioral": {
            "avg_leverage": round(avg_leverage, 1),
            "revenge_trades": revenge_trades,
            "overtrading_days": overtrading_days,
            "risk_per_trade_avg": round(abs(avg_loss), 2) if avg_loss else 0,
        }
    })


# ═══════════════════════════════════════════════════════════════
#  WEBSOCKET
# ═══════════════════════════════════════════════════════════════

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            payload = {
                "crypto": {
                    sym: {
                        'mark': d['mark'],
                        'index': d['index'],
                        'funding_rate': d['funding_rate'],
                        'funding_countdown': d['funding_countdown'],
                        'open_interest': d['open_interest'],
                        'volume_24h': d['volume_24h'],
                        'order_book': d['order_book'],
                        'daily_high': d['daily_high'],
                        'daily_low': d['daily_low'],
                        'rv': round(d['rv'], 4),
                        'regime': round(d['regime'], 4),
                    }
                    for sym, d in market_engine.state.items() if d['asset'] == 'crypto'
                },
                "forex": {
                    sym: {
                        'mark': d['mark'],
                        'bid': d['bid'],
                        'ask': d['ask'],
                        'spread_pips': d['spread_pips'],
                        'daily_high': d['daily_high'],
                        'daily_low': d['daily_low'],
                        'pip_size': d['pip_size'],
                        'swap_long': d['swap_long'],
                        'swap_short': d['swap_short'],
                    }
                    for sym, d in market_engine.state.items() if d['asset'] == 'forex'
                },
                "sessions": market_engine.get_forex_sessions(),
                "last_candle": getattr(market_engine, 'last_closed_candle', None)
            }
            await websocket.send_text(json.dumps(payload, default=str))
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        logger.info("Client disconnected from WebSocket.")
