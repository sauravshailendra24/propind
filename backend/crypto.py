import asyncio
import random
from decimal import Decimal
from datetime import datetime, timezone
import common
from common import logger

ORDER_FIELDS = """id, user_id, asset_class, symbol, side, order_type, time_in_force,
    size, filled_size, avg_fill_price, trigger_price, take_profit_price, stop_loss_price,
    trailing_distance, trailing_trigger, status, margin_mode, leverage,
    initial_margin, maintenance_margin, liquidation_price,
    maker_fee, taker_fee, funding_paid, swap_paid, opened_at, closed_at,
    close_reason, realized_pnl, created_at"""


async def execute_crypto_order(user_id, symbol, side, size, order_type, tif,leverage, margin_mode,
            trigger_price=None, take_profit=None,stop_loss=None, trailing_distance=None):
    from data_generator import market_engine
    await asyncio.sleep(random.uniform(0.05, 0.250))
    logger.info(f"[Latency] Order reached matching engine after delay.")
    state = market_engine.state[symbol]
    mark_price = state['mark']
    # ── SL/TP restriction (assessment mode) ──
    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT assessment_completed, upstox_verified FROM users_state WHERE user_id=%s",
                (user_id,)
            )
            row = await cur.fetchone()
            if row and not bool(row[0]):
                # In assessment mode → enforce SL/TP restrictions
                if not bool(row[1]):
                    return {"error": "Please open your Upstox account first."}
                # For pending orders, the entry will be the trigger price (validated on fill instead).
                # For market orders we don't know entry yet — but SL/TP can still be sanity-checked.
                if order_type == 'market' and (take_profit is None or stop_loss is None):
                    return {"error": "Stop-loss and take-profit are mandatory in assessment mode."}
                if take_profit and stop_loss:
                    # Use mark_price as approximate entry for pre-validation
                    ok, msg = common.validate_sl_tp(side, float(mark_price), float(stop_loss), float(take_profit))
                    if not ok:
                        return {"error": msg}
    is_maker = order_type in ('limit', 'stop_limit') or tif == 'POST_ONLY'
    fee_rate = common.MAKER_FEE_CRYPTO if is_maker else common.TAKER_FEE_CRYPTO

    if tif == 'POST_ONLY' and order_type == 'limit' and trigger_price:
        if (side == 'buy' and float(trigger_price) >= mark_price) or \
           (side == 'sell' and float(trigger_price) <= mark_price):
            return {"error": "Post-only order would cross, rejected"}

    if order_type in ('limit', 'stop_market', 'stop_limit', 'trailing_stop') and trigger_price:
        async with common.db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO trades_orders 
                    (user_id, asset_class, symbol, side, order_type, time_in_force, size, 
                     trigger_price, status, margin_mode, leverage, take_profit_price, 
                     stop_loss_price, trailing_distance)
                    VALUES (%s,'crypto',%s,%s,%s,%s,%s,%s,'PENDING',%s,%s,%s,%s,%s)""",
                    (user_id, symbol, side, order_type, tif, Decimal(str(size)),
                     Decimal(str(trigger_price)), margin_mode, leverage,
                     Decimal(str(take_profit)) if take_profit else Decimal('0'),
                     Decimal(str(stop_loss)) if stop_loss else Decimal('0'),
                     Decimal(str(trailing_distance)) if trailing_distance else Decimal('0'))
                )
                order_id = cur.lastrowid
        logger.info(f"[Crypto Pending] User {user_id} {order_type} {size} {symbol} @ {trigger_price}")
        return {"status": "pending", "order_id": order_id}
    fills = []
    remaining = size
    book_side = state['order_book']['asks'] if side == 'buy' else state['order_book']['bids']
    for level in book_side:
        if remaining <= 0:
            break
        fill_size = min(remaining, level['size'])
        fills.append({'price': level['price'], 'size': fill_size})
        remaining -= fill_size
    if remaining > 0:
        if tif == 'FOK':
            return {"error": "Fill or Kill: insufficient liquidity"}
        if tif == 'IOC' and not fills:
            return {"error": "IOC: no liquidity available"}
    if not fills:
        return {"error": "Insufficient liquidity"}
    filled_size = sum(f['size'] for f in fills)
    avg_fill = sum(f['price'] * f['size'] for f in fills) / filled_size
    impact_strength = (filled_size / 10.0) * state['vol'] * 0.1
    if side == 'buy':
        state['impact'] += impact_strength
    else:
        state['impact'] -= impact_strength
    avg_fill = round(Decimal(str(avg_fill)), 5)
    logger.info(f"[Execution] Filled {filled_size}/{size} {symbol} @ avg {avg_fill} (Mark {mark_price})")
    notional = float(avg_fill) * filled_size
    initial_margin = notional / leverage
    maintenance_margin = notional * common.MAINT_MARGIN_RATE_CRYPTO
    fill_price = float(avg_fill)
    if side == 'buy':
        liq_price = fill_price * (1 - 1/leverage + common.MAINT_MARGIN_RATE_CRYPTO)
    else:
        liq_price = fill_price * (1 + 1/leverage - common.MAINT_MARGIN_RATE_CRYPTO)
    liq_price = round(liq_price, 5)
    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT assessment_completed FROM users_state WHERE user_id=%s", (user_id,)
            )
            row = await cur.fetchone()
            if row and not bool(row[0]):
                sl_val = float(stop_loss) if stop_loss else None
                tp_val = float(take_profit) if take_profit else None
                ok, msg = common.validate_sl_tp(side, fill_price, sl_val, tp_val)
                if not ok:
                    return {"error": msg}
    fee = round(Decimal(str(notional)) * Decimal(str(fee_rate)), 2)
    maker_fee = fee if is_maker else Decimal('0')
    taker_fee = Decimal('0') if is_maker else fee

    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT free_margin, daily_start_balance, balance FROM users_state WHERE user_id=%s", (user_id,))
            row = await cur.fetchone()
            if not row or float(row[0]) < initial_margin:
                return {"error": "Insufficient free margin"}
            daily_start = float(row[1])
            current_balance = float(row[2])
            min_allowed_balance = daily_start * (1 - common.ASSESSMENT_DAILY_DD_PCT)
            if (current_balance - initial_margin) < min_allowed_balance:
                return {"error": "Order rejected: Violates daily drawdown limit"}
            await cur.execute(
                """INSERT INTO trades_orders 
                (user_id, asset_class, symbol, side, order_type, time_in_force, 
                 size, filled_size, avg_fill_price, status, margin_mode, leverage,
                 initial_margin, maintenance_margin, liquidation_price,
                 maker_fee, taker_fee, opened_at, take_profit_price, stop_loss_price)
                VALUES (%s,'crypto',%s,%s,%s,%s,%s,%s,%s,'OPEN',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (user_id, symbol, side, order_type, tif,
                 Decimal(str(size)), Decimal(str(filled_size)), avg_fill,
                 margin_mode, leverage,
                 Decimal(str(round(initial_margin, 2))),
                 Decimal(str(round(maintenance_margin, 2))),
                 Decimal(str(liq_price)),
                 maker_fee, taker_fee,
                 datetime.now(timezone.utc),
                 Decimal(str(take_profit)) if take_profit else Decimal('0'),
                 Decimal(str(stop_loss)) if stop_loss else Decimal('0'))
            )
            order_id = cur.lastrowid
            await cur.execute(
                """UPDATE users_state 
                   SET balance = balance - %s, total_fees = total_fees + %s, 
                       used_margin = used_margin + %s 
                   WHERE user_id = %s""",
                (fee, fee, Decimal(str(round(initial_margin, 2))), user_id)
            )
    logger.info(f"[Crypto Fill] ID:{order_id} User {user_id} {side} {filled_size} {symbol} "
                f"@ {avg_fill} | Liq: {liq_price} | Fee: {fee}")
    return {
        "status": "filled", "order_id": order_id,
        "avg_price": float(avg_fill), "filled_size": filled_size,
        "fee": float(fee), "liquidation_price": liq_price,
        "initial_margin": round(initial_margin, 2)
    }




async def check_pending_orders():
    from data_generator import market_engine
    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(f"SELECT {ORDER_FIELDS} FROM trades_orders WHERE asset_class='crypto' AND status='PENDING'")
            pending = await cur.fetchall()
            for pos in pending:
                sym = pos[3]
                if sym not in market_engine.state:
                    continue
                mark = market_engine.state[sym]['mark']
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
                elif otype == 'trailing_stop':
                    trailing_trigger = float(pos[14]) if pos[14] else 0
                    trailing_dist = float(pos[13]) if pos[13] else 0
                    if trailing_trigger == 0:
                        await cur.execute(
                            "UPDATE trades_orders SET trailing_trigger=%s WHERE id=%s",
                            (Decimal(str(mark)), pos[0])
                        )
                    else:
                        if side == 'buy' and mark <= trailing_trigger - trailing_dist:
                            should_trigger = True
                        elif side == 'sell' and mark >= trailing_trigger + trailing_dist:
                            should_trigger = True
                        if side == 'buy' and mark > trailing_trigger:
                            await cur.execute("UPDATE trades_orders SET trailing_trigger=%s WHERE id=%s",
                                              (Decimal(str(mark)), pos[0]))
                        elif side == 'sell' and mark < trailing_trigger:
                            await cur.execute("UPDATE trades_orders SET trailing_trigger=%s WHERE id=%s",
                                              (Decimal(str(mark)), pos[0]))
                if should_trigger:
                    # ── Fill the triggered order immediately at current mark ──
                    book_side = market_engine.state[sym]['order_book']['asks'] if side == 'buy' \
                                else market_engine.state[sym]['order_book']['bids']
                    fills = []
                    remaining = float(pos[7])  # size
                    for level in book_side:
                        if remaining <= 0:
                            break
                        fill_size = min(remaining, level['size'])
                        fills.append({'price': level['price'], 'size': fill_size})
                        remaining -= fill_size
                    if not fills:
                        await cur.execute("UPDATE trades_orders SET status='CANCELLED' WHERE id=%s", (pos[0],))
                        logger.warning(f"[Pending→Cancelled] {pos[0]} no liquidity")
                        continue

                    filled_size = sum(f['size'] for f in fills)
                    avg_fill = sum(f['price']*f['size'] for f in fills) / filled_size
                    avg_fill_dec = round(Decimal(str(avg_fill)), 5)
                    notional = avg_fill * filled_size
                    leverage = int(pos[17])
                    initial_margin = notional / leverage
                    maintenance_margin = notional * common.MAINT_MARGIN_RATE_CRYPTO
                    if side == 'buy':
                        liq_price = avg_fill * (1 - 1/leverage + common.MAINT_MARGIN_RATE_CRYPTO)
                    else:
                        liq_price = avg_fill * (1 + 1/leverage - common.MAINT_MARGIN_RATE_CRYPTO)
                    fee = round(Decimal(str(notional)) * Decimal(str(common.TAKER_FEE_CRYPTO)), 2)

                    # ── Daily drawdown check on the fill ──
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
                                logger.warning(f"[Pending→Cancelled] {pos[0]} violates daily DD")
                                continue

                    await cur.execute(
                        """UPDATE trades_orders
                        SET status='OPEN', filled_size=%s, avg_fill_price=%s,
                            initial_margin=%s, maintenance_margin=%s, liquidation_price=%s,
                            taker_fee=%s, opened_at=%s
                        WHERE id=%s""",
                        (Decimal(str(filled_size)), avg_fill_dec,
                        Decimal(str(round(initial_margin,2))),
                        Decimal(str(round(maintenance_margin,2))),
                        Decimal(str(round(liq_price,5))),
                        fee, datetime.now(timezone.utc), pos[0])
                    )
                    await cur.execute(
                        """UPDATE users_state
                        SET balance=balance-%s, total_fees=total_fees+%s,
                            used_margin=used_margin+%s
                        WHERE user_id=%s""",
                        (fee, fee, Decimal(str(round(initial_margin,2))), pos[1])
                    )
                    logger.info(f"[Pending→Filled] Order {pos[0]} filled @ {avg_fill}")


async def check_crypto_liquidations():
    from data_generator import market_engine
    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {ORDER_FIELDS} FROM trades_orders "
                "WHERE asset_class='crypto' AND status IN ('OPEN','PARTIAL','MARGIN_CALL')"
            )
            positions = await cur.fetchall()
            if not positions:
                return
            user_positions = {}
            for pos in positions:
                user_positions.setdefault(pos[1], []).append(pos)
            for uid, pos_list in user_positions.items():
                total_unrealized = 0
                total_margin_used = 0
                pos_data = []
                for pos in pos_list:
                    sym = pos[3]
                    if sym not in market_engine.state:
                        continue
                    mark = market_engine.state[sym]['mark']
                    entry = float(pos[9])  # avg_fill_price
                    size = float(pos[8])   # filled_size
                    side = pos[4]
                    pnl = (mark - entry) * size if side == 'buy' else (entry - mark) * size
                    margin_used = float(pos[18]) if pos[18] else (entry * size) / pos[17]
                    maint = float(pos[19]) if pos[19] else margin_used * 0.05
                    margin_mode = pos[16]
                    leverage = pos[17]
                    pos_data.append({
                        'pos': pos, 'pnl': pnl, 'margin_used': margin_used,
                        'maint': maint, 'mark': mark, 'mode': margin_mode,
                        'leverage': leverage
                    })
                    total_unrealized += pnl
                    total_margin_used += margin_used
                await cur.execute("SELECT balance FROM users_state WHERE user_id=%s", (uid,))
                acc = await cur.fetchone()
                if not acc:
                    continue
                balance = float(acc[0])
                equity = balance + total_unrealized
                free_margin = equity - total_margin_used
                margin_level = (equity / total_margin_used * 100) if total_margin_used > 0 else 999999
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
                for pd in pos_data:
                    pos = pd['pos']
                    mode = pd['mode']
                    if mode == 'isolated':
                        pos_equity = pd['margin_used'] + pd['pnl']
                        if pos_equity <= pd['maint']:
                            if pos_equity > pd['maint'] * 0.5:
                                await _partial_liquidate(cur, pos, pd, uid)
                            else:
                                await _full_liquidate(cur, pos, pd, uid)
                    else:
                        total_maint = sum(p['maint'] for p in pos_data)
                        if equity <= total_maint:
                            worst = min(pos_data, key=lambda x: x['pnl'])
                            if equity > total_maint * 0.5:
                                await _partial_liquidate(cur, worst['pos'], worst, uid)
                            else:
                                await _full_liquidate(cur, worst['pos'], worst, uid)
                            break

async def _partial_liquidate(cur, pos, pd, uid):
    """Reduce position by 50%, realize PnL on reduced portion."""
    pos_id = pos[0]
    sym = pos[3]
    size = float(pos[8])
    entry = float(pos[9])
    mark = pd['mark']
    side = pos[4]
    reduce_size = size * 0.50
    new_size = size - reduce_size

    if side == 'buy':
        realized = (mark - entry) * reduce_size
    else:
        realized = (entry - mark) * reduce_size

    margin_reduction = pd['margin_used'] * 0.5

    await cur.execute(
        "UPDATE trades_orders SET size=%s, filled_size=%s, status='PARTIAL', realized_pnl=realized_pnl+%s WHERE id=%s",
        (Decimal(str(new_size)), Decimal(str(new_size)), Decimal(str(round(realized, 2))), pos_id)
    )
    await cur.execute(
        """UPDATE users_state 
           SET balance=balance+%s, used_margin=used_margin-%s, realized_pnl=realized_pnl+%s 
           WHERE user_id=%s""",
        (Decimal(str(round(realized, 2))), Decimal(str(round(margin_reduction, 2))),
         Decimal(str(round(realized, 2))), uid)
    )
    logger.warning(f"[Partial Liq] Position {pos_id} {sym} reduced 50%. "
                   f"Size: {size}→{new_size}. Realized: ${realized:.2f}")


async def _full_liquidate(cur, pos, pd, uid):
    """Fully close position, record in closed_trades."""
    pos_id = pos[0]
    sym = pos[3]
    size = float(pos[8])
    entry = float(pos[9])
    mark = pd['mark']
    side = pos[4]
    leverage = pos[17]
    margin_mode = pos[16]

    if side == 'buy':
        realized = (mark - entry) * size
    else:
        realized = (entry - mark) * size

    margin_used = pd['margin_used']
    fees = float(pos[21]) + float(pos[22])  # maker + taker
    funding = float(pos[23]) if pos[23] else 0

    # Record closed trade
    await cur.execute(
        """INSERT INTO closed_trades 
        (user_id, symbol, asset_class, side, size, entry_price, exit_price,
         leverage, margin_mode, realized_pnl, fees, funding, swap, close_reason, opened_at)
        VALUES (%s,%s,'crypto',%s,%s,%s,%s,%s,%s,%s,%s,%s,0,'liquidation',%s)""",
        (uid, sym, side, Decimal(str(size)), Decimal(str(entry)), Decimal(str(mark)),
         leverage, margin_mode, Decimal(str(round(realized, 2))),
         Decimal(str(round(fees, 2))), Decimal(str(round(funding, 2))),
         pos[25] if pos[25] else datetime.now(timezone.utc))
    )

    await cur.execute(
        """UPDATE trades_orders 
           SET status='LIQUIDATED', closed_at=%s, close_reason='liquidation', realized_pnl=%s 
           WHERE id=%s""",
        (datetime.now(timezone.utc), Decimal(str(round(realized, 2))), pos_id)
    )
    await cur.execute(
        """UPDATE users_state 
           SET balance=balance+%s, used_margin=used_margin-%s, realized_pnl=realized_pnl+%s 
           WHERE user_id=%s""",
        (Decimal(str(round(realized, 2))), Decimal(str(round(margin_used, 2))),
         Decimal(str(round(realized, 2))), uid)
    )
    logger.error(f"[FULL LIQUIDATION] Position {pos_id} {sym} liquidated. "
                 f"Realized: ${realized:.2f}. Margin freed: ${margin_used:.2f}")


async def check_crypto_funding():
    """Apply funding fees at 00:00, 08:00, 16:00 UTC."""
    from data_generator import market_engine
    now = datetime.now(timezone.utc)
    if now.minute != 0 or now.second > 10:
        return
    if now.hour not in [0, 8, 16]:
        return

    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {ORDER_FIELDS} FROM trades_orders "
                "WHERE asset_class='crypto' AND status IN ('OPEN','PARTIAL')"
            )
            positions = await cur.fetchall()
            for pos in positions:
                sym = pos[3]
                if sym not in market_engine.state:
                    continue
                funding_rate = market_engine.state[sym]['funding_rate']
                size = float(pos[8])
                mark = market_engine.state[sym]['mark']
                notional = size * mark
                side = pos[4]

                # Longs pay positive funding, shorts receive
                if side == 'buy':
                    payment = -notional * funding_rate
                else:
                    payment = notional * funding_rate

                await cur.execute(
                    "UPDATE trades_orders SET funding_paid=funding_paid+%s WHERE id=%s",
                    (Decimal(str(round(payment, 2))), pos[0])
                )
                await cur.execute(
                    """UPDATE users_state 
                       SET balance=balance+%s, total_funding=total_funding+%s 
                       WHERE user_id=%s""",
                    (Decimal(str(round(payment, 2))),
                     Decimal(str(abs(round(payment, 2)))),
                     pos[1])
                )
                logger.info(f"[Funding] User {pos[1]} {sym} {'paid' if payment < 0 else 'received'}: "
                           f"${abs(payment):.2f} (rate: {funding_rate*100:.4f}%)")

async def check_crypto_sl_tp():
    """Auto-close positions when SL or TP price is hit."""
    from data_generator import market_engine
    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""SELECT {ORDER_FIELDS} FROM trades_orders
                    WHERE asset_class='crypto' AND status IN ('OPEN','PARTIAL','MARGIN_CALL')
                    AND (take_profit_price > 0 OR stop_loss_price > 0)"""
            )
            positions = await cur.fetchall()
            for pos in positions:
                sym = pos[3]
                if sym not in market_engine.state:
                    continue
                mark = market_engine.state[sym]['mark']
                entry = float(pos[9])
                side = pos[4]
                tp = float(pos[11]) if pos[11] else 0
                sl = float(pos[12]) if pos[12] else 0
                hit = None
                if side == 'buy':
                    if sl and mark <= sl: hit = 'stop_loss'
                    elif tp and mark >= tp: hit = 'take_profit'
                else:
                    if sl and mark >= sl: hit = 'stop_loss'
                    elif tp and mark <= tp: hit = 'take_profit'
                if hit:
                    size = float(pos[8])
                    realized = (mark-entry)*size if side=='buy' else (entry-mark)*size
                    await cur.execute(
                        "UPDATE trades_orders SET status='FILLED', close_reason=%s, "
                        "closed_at=%s, realized_pnl=%s WHERE id=%s",
                        (hit, datetime.now(timezone.utc),
                         Decimal(str(round(realized,2))), pos[0])
                    )
                    await cur.execute(
                        "UPDATE users_state SET balance=balance+%s, used_margin=used_margin-%s, "
                        "realized_pnl=realized_pnl+%s WHERE user_id=%s",
                        (Decimal(str(round(realized,2))),
                         Decimal(str(round(float(pos[18]),2))),
                         Decimal(str(round(realized,2))), pos[1])
                    )
                    logger.info(f"[SL/TP] {pos[0]} closed via {hit} @ {mark}")