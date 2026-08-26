import secrets
from fastapi import Request
import common
from common import hash_password, verify_password, logger
from datetime import datetime, timezone
from decimal import Decimal

async def signup(username: str, name: str, password: str,whatsapp_opt_in: bool = False, whatsapp_number: str = None):
    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT user_id FROM users WHERE username=%s", (username,))
            if await cur.fetchone():
                return {"error": "Username already taken"}
            user_id = secrets.token_hex(16)
            hashed, salt = hash_password(password)
            await cur.execute(
                """INSERT INTO users (user_id, username, name, password_hash, salt, whatsapp_opt_in, whatsapp_number)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (user_id, username, name, hashed, salt, whatsapp_opt_in, whatsapp_number)
            )
            await cur.execute(
                    "INSERT INTO users_state (user_id, upstox_verified, assessment_step, step_start_balance) "
                    "VALUES (%s, FALSE, 1, 100000.00)",
                    (user_id,)
                )
            await cur.execute("UPDATE users_state SET eval_start_date = NOW() WHERE user_id = %s", (user_id,))
            logger.info(f"[Auth] New signup: {username} ({user_id})")
            return {"user_id": user_id, "username": username, "name": name, 
                    "whatsapp_opt_in": whatsapp_opt_in}

async def login(username: str, password: str):
    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT user_id, password_hash, salt, name, whatsapp_opt_in, whatsapp_number FROM users WHERE username=%s",
                (username,)
            )
            row = await cur.fetchone()
            if not row:
                return {"error": "User not found"}
            user_id, stored_hash, salt, name, wa_opt, wa_num = row
            if not verify_password(password, salt, stored_hash):
                return {"error": "Invalid password"}
            logger.info(f"[Auth] Login: {username} ({user_id})")
            return {"user_id": user_id, "username": username, "name": name, 
                    "whatsapp_opt_in": bool(wa_opt), "whatsapp_number": wa_num}

async def record_stock360s_purchase(user_id: str, plan: str):
    if plan not in ('1mo', '1yr'):
        return {"error": "Invalid plan"}
    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            col = 'stock360s_1mo_purchased' if plan == '1mo' else 'stock360s_1yr_purchased'
            await cur.execute(
                f"""UPDATE users_state
                    SET {col}=TRUE,
                        stock360s_purchase_time=NOW(),
                        stock360s_confirmed=FALSE
                    WHERE user_id=%s""",
                (user_id,)
            )
            logger.info(f"[Stock360s] Purchase recorded: {user_id} plan={plan}")
            return {"status": "pending", "message": "Purchase recorded. Confirmation within 1 hour."}

async def update_whatsapp_optin(user_id: str, opt_in: bool, whatsapp_number: str = None):
    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE users SET whatsapp_opt_in=%s, whatsapp_number=%s WHERE user_id=%s",
                (opt_in, whatsapp_number, user_id)
            )
            logger.info(f"[Auth] WhatsApp opt-in updated for {user_id}: {opt_in}")
            return {"status": "updated", "whatsapp_opt_in": opt_in}

async def verify_upstox_account(user_id: str):
    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """UPDATE users_state
                   SET upstox_verified=TRUE,
                       upstox_verify_request_time=NOW()
                   WHERE user_id=%s""",
                (user_id,)
            )
            if cur.rowcount == 0:
                logger.error(f"[Upstox] User state not found for {user_id}")
                return {
                    "status": "error",
                    "message": "User state not found."
                }
            logger.info(f"[Upstox] Verified immediately for {user_id}")
            return {
                "status": "verified",
                "message": "Upstox verification completed successfully."
            }

async def get_current_user(request: Request):
    user_id = request.cookies.get("propind_user")
    if not user_id:
        return None
    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT user_id, username, name, whatsapp_opt_in, whatsapp_number FROM users WHERE user_id=%s",
                (user_id,)
            )
            row = await cur.fetchone()
            if not row:
                return None
            return {
                "user_id": row[0], "username": row[1], "name": row[2],
                "whatsapp_opt_in": bool(row[3]), "whatsapp_number": row[4]
            }

STOCK360S_LINKS = {
    '1mo': 'https://stock360s.com/#landing-pricing',
    '1yr': 'https://stock360s.com/#landing-pricing'
}

async def set_experience_level(user_id: str, level: str):
    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE users SET experience_level=%s WHERE user_id=%s",
                (level, user_id)
            )
            await cur.execute(
                "UPDATE users_state SET onboarding_step=1 WHERE user_id=%s AND onboarding_step=0",
                (user_id,)
            )
            logger.info(f"[Onboarding] Experience set for {user_id}: {level}")
            return {"status": "ok", "experience_level": level}

async def advance_onboarding(user_id: str, current_step: int = None, method: str = None):
    """Advance assessment step. Auto-called by background task OR stock360s confirmation."""
    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            if current_step is None:
                await cur.execute(
                    "SELECT assessment_step FROM users_state WHERE user_id=%s",
                    (user_id,)
                )
                row = await cur.fetchone()
                if not row:
                    return {"error": "No state"}
                current_step = int(row[0])

            next_step = current_step + 1
            if next_step > 3:
                await cur.execute(
                    """UPDATE users_state
                       SET assessment_step=4, assessment_completed=TRUE,
                           onboarding_completed=TRUE, onboarding_step=5
                       WHERE user_id=%s""",
                    (user_id,)
                )
                logger.info(f"[Assessment] COMPLETED for {user_id}")
                return {"status": "completed", "step": 4}
                        # ── Close all open/pending positions before resetting ──
            await cur.execute(
                f"SELECT id, user_id, asset_class, symbol, side, size, filled_size, "
                f"avg_fill_price, initial_margin, maker_fee, taker_fee, funding_paid, "
                f"swap_paid, opened_at FROM trades_orders "
                f"WHERE user_id=%s AND status IN ('OPEN','PARTIAL','MARGIN_CALL','PENDING')",
                (user_id,)
            )
            open_positions = await cur.fetchall()
            for op in open_positions:
                op_id, op_uid, op_ac, op_sym, op_side, op_size, op_filled, op_entry, \
                    op_margin, op_mfee, op_tfee, op_fund, op_swap, op_opened = op
                actual_size = float(op_filled) if op_filled else float(op_size)
                # Use mark for crypto, bid/ask for forex
                from data_generator import market_engine
                if op_sym in market_engine.state:
                    if op_ac == 'crypto':
                        close_px = market_engine.state[op_sym]['mark']
                    else:
                        close_px = market_engine.state[op_sym]['bid'] if op_side == 'buy' else market_engine.state[op_sym]['ask']
                else:
                    close_px = float(op_entry)
                if op_side == 'buy':
                    realized = (close_px - float(op_entry)) * actual_size
                else:
                    realized = (float(op_entry) - close_px) * actual_size
                total_fees = float(op_mfee or 0) + float(op_tfee or 0)
                total_funding = float(op_fund or 0)
                total_swap = float(op_swap or 0)
                await cur.execute(
                    """INSERT INTO closed_trades
                    (user_id, symbol, asset_class, side, size, entry_price, exit_price,
                     leverage, margin_mode, realized_pnl, fees, funding, swap, close_reason, opened_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,1,'cross',%s,%s,%s,%s,'step_advance',%s)""",
                    (user_id, op_sym, op_ac, op_side, Decimal(str(actual_size)),
                     Decimal(str(float(op_entry))), Decimal(str(round(close_px, 5))),
                     Decimal(str(round(realized, 2))), Decimal(str(round(total_fees, 2))),
                     Decimal(str(round(total_funding, 2))), Decimal(str(round(total_swap, 2))),
                     op_opened if op_opened else datetime.now(timezone.utc))
                )
                await cur.execute(
                    "UPDATE trades_orders SET status='FILLED', closed_at=%s, close_reason='step_advance', "
                    "realized_pnl=%s WHERE id=%s",
                    (datetime.now(timezone.utc), Decimal(str(round(realized, 2))), op_id)
                )
            # Release all used margin from closed positions
            if open_positions:
                await cur.execute(
                    "UPDATE users_state SET used_margin=0, unrealized_pnl=0 WHERE user_id=%s",
                    (user_id,)
                )

            # Reset balance for new step
            await cur.execute(
                """UPDATE users_state
                   SET assessment_step=%s,
                       step_start_balance=100000.00,
                       balance=100000.00,
                       daily_start_balance=100000.00,
                       equity=100000.00,
                       used_margin=0, free_margin=100000.00,
                       unrealized_pnl=0, peak_equity=100000.00,
                       max_drawdown_breached=FALSE
                   WHERE user_id=%s""",
                (next_step, user_id)
            )
            logger.info(f"[Assessment] {user_id} advanced to step {next_step}")
            return {"status": "advanced", "step": next_step}

async def get_onboarding_state(user_id: str):
    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT experience_level FROM users WHERE user_id=%s", (user_id,))
            urow = await cur.fetchone()

            await cur.execute(
                """SELECT onboarding_step, onboarding_completed,
                          assessment_step, assessment_completed,
                          upstox_verified, step_start_balance,
                          stock360s_1mo_purchased, stock360s_1yr_purchased,
                          stock360s_purchase_time, stock360s_confirmed,
                          balance, equity, daily_start_balance,upstox_verify_request_time
                   FROM users_state WHERE user_id=%s""",
                (user_id,)
            )
            srow = await cur.fetchone()
            if not srow:
                return None

            step = int(srow[2]) if srow[2] else 1
            step_start = float(srow[5]) if srow[5] else 100000.00
            target_mult = common.ASSESSMENT_TARGETS.get(step, 1.10)
            target_balance = round(step_start * target_mult, 2)

            # For UI: only show the CURRENT step's data. Never expose future step info.
            return {
                "experience_level": urow[0] if urow else None,
                "onboarding_step": int(srow[0]) if srow[0] else 0,
                "onboarding_completed": bool(srow[1]),
                "assessment_step": step,
                "assessment_completed": bool(srow[3]),
                "upstox_verified": bool(srow[4]),
                "upstox_verify_request_time": str(srow[13]) if srow[13] else None,
                "step_start_balance": step_start,
                "target_balance": target_balance,
                "current_balance": float(srow[10]) if srow[10] else 100000.00,
                "current_equity": float(srow[11]) if srow[11] else 100000.00,
                "daily_start_balance": float(srow[12]) if srow[12] else 100000.00,
                "daily_drawdown_limit": round(step_start * common.ASSESSMENT_DAILY_DD_PCT, 2),
                "stock360s_1mo_purchased": bool(srow[6]),
                "stock360s_1yr_purchased": bool(srow[7]),
                "stock360s_purchase_time": str(srow[8]) if srow[8] else None,
                "stock360s_confirmed": bool(srow[9]),
                "stock360s_pending": bool(srow[8]) and not bool(srow[9]),
                "stock360s_links": STOCK360S_LINKS,
            }
