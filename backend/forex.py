import asyncio
import random
from decimal import Decimal
from datetime import datetime, timezone
import common
from common import logger

LOT_SIZES = {'standard': 1.00, 'mini': 0.10, 'micro': 0.01}

ORDER_FIELDS = """id, user_id, asset_class, symbol, side, order_type, time_in_force,
    size, filled_size, avg_fill_price, trigger_price, take_profit_price, stop_loss_price,
    trailing_distance, trailing_trigger, status, margin_mode, leverage,
    initial_margin, maintenance_margin, liquidation_price,
    maker_fee, taker_fee, funding_paid, swap_paid, opened_at, closed_at,
    close_reason, realized_pnl, created_at"""


async def execute_forex_order(user_id, symbol, side, lots, order_type,
                              lot_type='standard', tif='GTC',
                              leverage=100, margin_mode='cross',
                              trigger_price=None, take_profit=None,
                              stop_loss=None, trailing_distance=None):
    """Execute forex order with spread, lot sizes, and margin."""
    from data_generator import market_engine

    await asyncio.sleep(random.uniform(0.05, 0.250))

    state = market_engine.state[symbol]
    pip_size = state['pip_size']
    spread_pips = state['spread_pips']
    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT assessment_completed, upstox_verified FROM users_state WHERE user_id=%s",
                (user_id,)
            )
            row = await cur.fetchone()
            if row and not bool(row[0]):
                if not bool(row[1]):
                    return {"error": "Please open your Upstox account first."}
                if order_type == 'market' and (take_profit is None or stop_loss is None):
                    return {"error": "Stop-loss and take-profit are mandatory in assessment mode."}
    units = lots * 100000  # Standard lot = 100k units

    # ── Pending orders ──
    if order_type in ('limit', 'stop_market', 'stop_limit', 'trailing_stop') and trigger_price:
        async with common.db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO trades_orders 
                    (user_id, asset_class, symbol, side, order_type, time_in_force, 
                     size, trigger_price, status, margin_mode, leverage,
                     take_profit_price, stop_loss_price, trailing_distance)
                    VALUES (%s,'forex',%s,%s,%s,%s,%s,%s,'PENDING',%s,%s,%s,%s,%s)""",
                    (user_id, symbol, side, order_type, tif,
                     Decimal(str(units)), Decimal(str(trigger_price)),
                     margin_mode, leverage,
                     Decimal(str(take_profit)) if take_profit else Decimal('0'),
                     Decimal(str(stop_loss)) if stop_loss else Decimal('0'),
                     Decimal(str(trailing_distance)) if trailing_distance else Decimal('0'))
                )
                order_id = cur.lastrowid
        logger.info(f"[Forex Pending] User {user_id} {order_type} {lots} lots {symbol} @ {trigger_price}")
        return {"status": "pending", "order_id": order_id}

    # ── Market order: apply spread ──
    if side == 'buy':
        fill_price = Decimal(str(state['ask']))
    else:
        fill_price = Decimal(str(state['bid']))
    fill_price = round(fill_price, 5)
    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT assessment_completed FROM users_state WHERE user_id=%s", (user_id,)
            )
            row = await cur.fetchone()
            if row and not bool(row[0]):
                sl_val = float(stop_loss) if stop_loss else None
                tp_val = float(take_profit) if take_profit else None
                ok, msg = common.validate_sl_tp(side, float(fill_price), sl_val, tp_val)
                if not ok:
                    return {"error": msg}
    notional = float(fill_price) * units
    initial_margin = notional / leverage
    maintenance_margin = notional * common.MAINT_MARGIN_RATE_FOREX

    # Pip value calculation
    if 'USD' in symbol[:3]:
        pip_value = units * pip_size
    else:
        pip_value = (pip_size / float(fill_price)) * units

    fee = round(Decimal(str(notional)) * Decimal(str(common.TAKER_FEE_FOREX)), 2)

    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT free_margin FROM users_state WHERE user_id=%s", (user_id,))
            row = await cur.fetchone()
            if not row or float(row[0]) < initial_margin:
                return {"error": "Insufficient free margin"}

            await cur.execute(
                """INSERT INTO trades_orders 
                (user_id, asset_class, symbol, side, order_type, time_in_force, 
                 size, filled_size, avg_fill_price, status, margin_mode, leverage,
                 initial_margin, maintenance_margin, taker_fee, opened_at,
                 take_profit_price, stop_loss_price)
                VALUES (%s,'forex',%s,%s,%s,%s,%s,%s,%s,'OPEN',%s,%s,%s,%s,%s,%s,%s,%s)""",
                (user_id, symbol, side, order_type, tif,
                 Decimal(str(units)), Decimal(str(units)), fill_price,
                 margin_mode, leverage,
                 Decimal(str(round(initial_margin, 2))),
                 Decimal(str(round(maintenance_margin, 2))),
                 fee, datetime.now(timezone.utc),
                 Decimal(str(take_profit)) if take_profit else Decimal('0'),
                 Decimal(str(stop_loss)) if stop_loss else Decimal('0'))
            )
            order_id = cur.lastrowid

            await cur.execute(
                """UPDATE users_state 
                   SET balance=balance-%s, total_fees=total_fees+%s, used_margin=used_margin+%s 
                   WHERE user_id=%s""",
                (fee, fee, Decimal(str(round(initial_margin, 2))), user_id)
            )

    logger.info(f"[Forex Fill] ID:{order_id} User {user_id} {side} {lots} lots ({lot_type}) "
                f"{symbol} @ {fill_price} | Spread: {spread_pips} pips | Pip value: ${pip_value:.2f}")
    return {
        "status": "filled", "order_id": order_id,
        "avg_price": float(fill_price), "fee": float(fee),
        "pip_value": round(pip_value, 2),
        "initial_margin": round(initial_margin, 2)
    }


async def check_forex_liquidations():
    """Margin call at 50%, stop out at 20% of margin level."""
    from data_generator import market_engine
    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {ORDER_FIELDS} FROM trades_orders "
                "WHERE asset_class='forex' AND status IN ('OPEN','PARTIAL','MARGIN_CALL')"
            )
            positions = await cur.fetchall()
            if not positions:
                return

            user_positions = {}
            for pos in positions:
                user_positions.setdefault(pos[1], []).append(pos)

            for uid, pos_list in user_positions.items():
                total_unrealized = 0
                total_margin = 0
                pos_data = []

                for pos in pos_list:
                    sym = pos[3]
                    if sym not in market_engine.state:
                        continue
                    state = market_engine.state[sym]
                    entry = float(pos[9])
                    units = float(pos[8])
                    side = pos[4]

                    if side == 'buy':
                        current = state['bid']
                        pnl = (current - entry) * units
                    else:
                        current = state['ask']
                        pnl = (entry - current) * units

                    margin = float(pos[18]) if pos[18] else 0
                    pos_data.append({'pos': pos, 'pnl': pnl, 'margin': margin, 'current': current})
                    total_unrealized += pnl
                    total_margin += margin

                await cur.execute("SELECT balance FROM users_state WHERE user_id=%s", (uid,))
                acc = await cur.fetchone()
                if not acc:
                    continue
                balance = float(acc[0])
                equity = balance + total_unrealized
                margin_level = (equity / total_margin * 100) if total_margin > 0 else 999999
                free_margin = equity - total_margin
                peak = max(balance, equity)

                await cur.execute(
                    """UPDATE users_state 
                       SET equity=%s, unrealized_pnl=%s, free_margin=%s, 
                           margin_level=%s, peak_equity=GREATEST(peak_equity, %s)
                       WHERE user_id=%s""",
                    (Decimal(str(round(equity, 2))), Decimal(str(round(total_unrealized, 2))),
                     Decimal(str(round(free_margin, 2))), Decimal(str(round(margin_level, 2))),
                     Decimal(str(round(peak, 2))), uid)
                )

                # ── Margin call warning at 50% ──
                if margin_level < 50 and margin_level >= 20:
                    for pd in pos_data:
                        if pd['pos'][15] != 'MARGIN_CALL':
                            await cur.execute(
                                "UPDATE trades_orders SET status='MARGIN_CALL' WHERE id=%s",
                                (pd['pos'][0],)
                            )
                    logger.warning(f"[MARGIN CALL] User {uid} margin level: {margin_level:.1f}%")

                # ── Stop out at 20%: close worst position ──
                if margin_level < 20:
                    worst = min(pos_data, key=lambda x: x['pnl'])
                    await _forex_stop_out(cur, worst, uid)


async def _forex_stop_out(cur, pd, uid):
    """Force-close worst-performing forex position."""
    pos = pd['pos']
    pos_id = pos[0]
    sym = pos[3]
    units = float(pos[8])
    entry = float(pos[9])
    current = pd['current']
    side = pos[4]
    leverage = pos[17]
    margin_mode = pos[16]
    fees = float(pos[21]) + float(pos[22])

    if side == 'buy':
        realized = (current - entry) * units
    else:
        realized = (entry - current) * units

    await cur.execute(
        """INSERT INTO closed_trades 
        (user_id, symbol, asset_class, side, size, entry_price, exit_price,
         leverage, margin_mode, realized_pnl, fees, funding, swap, close_reason, opened_at)
        VALUES (%s,%s,'forex',%s,%s,%s,%s,%s,%s,%s,%s,0,0,'stop_out',%s)""",
        (uid, sym, side, Decimal(str(units)), Decimal(str(entry)), Decimal(str(current)),
         leverage, margin_mode, Decimal(str(round(realized, 2))),
         Decimal(str(round(fees, 2))),
         pos[25] if pos[25] else datetime.now(timezone.utc))
    )

    await cur.execute(
        """UPDATE trades_orders 
           SET status='STOP_OUT', closed_at=%s, close_reason='stop_out', realized_pnl=%s 
           WHERE id=%s""",
        (datetime.now(timezone.utc), Decimal(str(round(realized, 2))), pos_id)
    )
    await cur.execute(
        """UPDATE users_state 
           SET balance=balance+%s, used_margin=used_margin-%s, realized_pnl=realized_pnl+%s 
           WHERE user_id=%s""",
        (Decimal(str(round(realized, 2))), Decimal(str(round(pd['margin'], 2))),
         Decimal(str(round(realized, 2))), uid)
    )
    logger.error(f"[STOP OUT] Position {pos_id} {sym} closed. Realized: ${realized:.2f}")


async def check_forex_swap():
    """Apply swap fees at 22:00 UTC (17:00 NY)."""
    now = datetime.now(timezone.utc)
    if now.hour != 22 or now.minute != 0 or now.second > 10:
        return

    from data_generator import market_engine
    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {ORDER_FIELDS} FROM trades_orders "
                "WHERE asset_class='forex' AND status IN ('OPEN','PARTIAL','MARGIN_CALL')"
            )
            positions = await cur.fetchall()
            for pos in positions:
                sym = pos[3]
                state = market_engine.state.get(sym)
                if not state:
                    continue
                side = pos[4]
                units = float(pos[8])
                swap_rate = state['swap_long'] if side == 'buy' else state['swap_short']
                pip_size = state['pip_size']
                # Pip value in USD per pip per unit-volume traded
                if sym[:3] == 'USD':
                    pip_value_per_unit = pip_size  # e.g., USD/JPY
                else:
                    pip_value_per_unit = pip_size / float(state['mark'])
                # Swap charge: rate is per standard lot, units already includes lot multiplier
                swap = units * swap_rate * pip_value_per_unit
                swap = round(swap, 2)

                await cur.execute(
                    "UPDATE trades_orders SET swap_paid=swap_paid+%s WHERE id=%s",
                    (Decimal(str(round(swap, 2))), pos[0])
                )
                await cur.execute(
                    """UPDATE users_state 
                       SET balance=balance+%s, total_swap=total_swap+%s 
                       WHERE user_id=%s""",
                    (Decimal(str(round(swap, 2))),
                     Decimal(str(abs(round(swap, 2)))),
                     pos[1])
                )
                logger.info(f"[Swap] User {pos[1]} {sym} swap: ${swap:.2f}")

async def check_forex_pending_orders():
    """Check and fill pending forex orders when trigger price is hit."""
    from data_generator import market_engine
    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {ORDER_FIELDS} FROM trades_orders "
                "WHERE asset_class='forex' AND status='PENDING'"
            )
            pending = await cur.fetchall()
            for pos in pending:
                sym = pos[3]
                if sym not in market_engine.state:
                    continue
                state = market_engine.state[sym]
                mark = state['mark']
                trigger = float(pos[9])  # trigger_price
                side = pos[4]
                otype = pos[5]
                should_trigger = False
                if otype == 'limit':
                    if side == 'buy' and mark <= trigger: should_trigger = True
                    elif side == 'sell' and mark >= trigger: should_trigger = True
                elif otype in ('stop_market', 'stop_limit'):
                    if side == 'buy' and mark >= trigger: should_trigger = True
                    elif side == 'sell' and mark <= trigger: should_trigger = True

                if should_trigger:
                    # Fill at current bid/ask
                    fill_price = float(state['ask']) if side == 'buy' else float(state['bid'])
                    fill_price_dec = round(Decimal(str(fill_price)), 5)
                    units = float(pos[7])
                    leverage = int(pos[17])
                    notional = fill_price * units
                    initial_margin = notional / leverage
                    maintenance_margin = notional * common.MAINT_MARGIN_RATE_FOREX
                    fee = round(Decimal(str(notional)) * Decimal(str(common.TAKER_FEE_FOREX)), 2)

                    # Daily drawdown check
                    await cur.execute(
                        "SELECT balance, daily_start_balance, assessment_completed FROM users_state WHERE user_id=%s",
                        (pos[1],)
                    )
                    srow = await cur.fetchone()
                    if srow:
                        bal, daily_start, completed = float(srow[0]), float(srow[1]), bool(srow[2])
                        if not completed:
                            min_allowed = daily_start * (1 - common.ASSESSMENT_DAILY_DD_PCT)
                            if (bal - initial_margin) < min_allowed:
                                await cur.execute(
                                    "UPDATE trades_orders SET status='CANCELLED' WHERE id=%s", (pos[0],)
                                )
                                logger.warning(f"[Forex Pending→Cancelled] {pos[0]} violates daily DD")
                                continue

                    await cur.execute(
                        """UPDATE trades_orders
                           SET status='OPEN', filled_size=%s, avg_fill_price=%s,
                               initial_margin=%s, maintenance_margin=%s,
                               taker_fee=%s, opened_at=%s
                           WHERE id=%s""",
                        (Decimal(str(units)), fill_price_dec,
                         Decimal(str(round(initial_margin, 2))),
                         Decimal(str(round(maintenance_margin, 2))),
                         fee, datetime.now(timezone.utc), pos[0])
                    )
                    await cur.execute(
                        """UPDATE users_state
                           SET balance=balance-%s, total_fees=total_fees+%s,
                               used_margin=used_margin+%s
                           WHERE user_id=%s""",
                        (fee, fee, Decimal(str(round(initial_margin, 2))), pos[1])
                    )
                    logger.info(f"[Forex Pending→Filled] Order {pos[0]} filled @ {fill_price}")

async def check_forex_sl_tp():
    """Auto-close forex positions when SL or TP price is hit."""
    from data_generator import market_engine
    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""SELECT {ORDER_FIELDS} FROM trades_orders
                    WHERE asset_class='forex' AND status IN ('OPEN','PARTIAL','MARGIN_CALL')
                    AND (take_profit_price > 0 OR stop_loss_price > 0)"""
            )
            positions = await cur.fetchall()
            for pos in positions:
                sym = pos[3]
                if sym not in market_engine.state:
                    continue
                state = market_engine.state[sym]
                entry = float(pos[9])
                side = pos[4]
                tp = float(pos[11]) if pos[11] else 0
                sl = float(pos[12]) if pos[12] else 0
                # Use bid for closing longs, ask for closing shorts
                close_price = state['bid'] if side == 'buy' else state['ask']
                hit = None
                if side == 'buy':
                    if sl and close_price <= sl: hit = 'stop_loss'
                    elif tp and close_price >= tp: hit = 'take_profit'
                else:
                    if sl and close_price >= sl: hit = 'stop_loss'
                    elif tp and close_price <= tp: hit = 'take_profit'
                if hit:
                    units = float(pos[8])
                    realized = (close_price - entry) * units if side == 'buy' else (entry - close_price) * units
                    await cur.execute(
                        "UPDATE trades_orders SET status='FILLED', close_reason=%s, "
                        "closed_at=%s, realized_pnl=%s WHERE id=%s",
                        (hit, datetime.now(timezone.utc),
                         Decimal(str(round(realized, 2))), pos[0])
                    )
                    await cur.execute(
                        "UPDATE users_state SET balance=balance+%s, used_margin=used_margin-%s, "
                        "realized_pnl=realized_pnl+%s WHERE user_id=%s",
                        (Decimal(str(round(realized, 2))),
                         Decimal(str(round(float(pos[18]), 2))),
                         Decimal(str(round(realized, 2))), pos[1])
                    )
                    logger.info(f"[Forex SL/TP] {pos[0]} closed via {hit} @ {close_price}")