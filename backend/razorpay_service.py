import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timezone

import httpx

import common
from common import logger


async def create_challenge_order(user_id: str):
    """Create one Razorpay order for the current user. Fulfilment happens only via webhook."""
    if not common.RAZORPAY_KEY_ID or not common.RAZORPAY_KEY_SECRET:
        return {"error": "Razorpay is not configured on the server."}

    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT upstox_verified, challenge_active_until
                   FROM users_state WHERE user_id=%s""",
                (user_id,),
            )
            row = await cur.fetchone()
            if not row:
                return {"error": "State not found."}
            upstox_verified, active_until = row
            if not upstox_verified:
                return {"error": "Please complete Upstox account verification first."}
            if active_until:
                await cur.execute("SELECT %s > NOW()", (active_until,))
                active_now = bool((await cur.fetchone())[0])
                if active_now:
                    return {
                        "status": "active",
                        "message": "Your challenge is already active.",
                        "active_until": str(active_until),
                    }

    amount_paise = common.CHALLENGE_PRICE_INR * 100
    receipt = f"propind_{uuid.uuid4().hex[:24]}"
    payload = {
        "amount": amount_paise,
        "currency": "INR",
        "receipt": receipt,
        "notes": {"user_id": user_id, "product": "propind_4_day_challenge"},
        "partial_payment": False,
        "payment_capture": 1,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{common.RAZORPAY_API_BASE}/orders",
                auth=(common.RAZORPAY_KEY_ID, common.RAZORPAY_KEY_SECRET),
                json=payload,
            )
            response.raise_for_status()
            order = response.json()
    except Exception as exc:
        logger.error(f"[Razorpay] Order creation failed for {user_id}: {exc}")
        return {"error": "Unable to create payment order. Please try again."}

    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO challenge_payments
                   (user_id, razorpay_order_id, amount_paise, status)
                   VALUES (%s,%s,%s,'created')""",
                (user_id, order["id"], amount_paise),
            )
            await conn.commit()

    return {
        "status": "created",
        "key_id": common.RAZORPAY_KEY_ID,
        "order_id": order["id"],
        "amount": amount_paise,
        "currency": "INR",
        "name": "PropInd",
        "description": "4-day PropInd trading challenge",
    }


def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    secret = common.RAZORPAY_WEBHOOK_SECRET
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


async def _close_positions_for_challenge_reset(cur, user_id: str, reason: str):
    """Close open positions before a new paid challenge resets account equity."""
    from data_generator import market_engine
    await cur.execute(
        """SELECT id, asset_class, symbol, side, size, filled_size,
                  avg_fill_price, maker_fee, taker_fee, funding_paid, swap_paid, opened_at
           FROM trades_orders
           WHERE user_id=%s AND status IN ('OPEN','PARTIAL','MARGIN_CALL','PENDING')""",
        (user_id,),
    )
    rows = await cur.fetchall()
    for row in rows:
        (op_id, asset_class, symbol, side, size, filled_size, entry,
         maker_fee, taker_fee, funding_paid, swap_paid, opened_at) = row
        actual_size = float(filled_size or size or 0)
        if symbol in market_engine.state:
            state = market_engine.state[symbol]
            close_px = state['mark'] if asset_class == 'crypto' else (
                state['bid'] if side == 'buy' else state['ask']
            )
        else:
            close_px = float(entry or 0)
        if side == 'buy':
            realized = (float(close_px) - float(entry or 0)) * actual_size
        else:
            realized = (float(entry or 0) - float(close_px)) * actual_size
        fees = float(maker_fee or 0) + float(taker_fee or 0)
        await cur.execute(
            """INSERT INTO closed_trades
               (user_id, symbol, asset_class, side, size, entry_price, exit_price,
                leverage, margin_mode, realized_pnl, fees, funding, swap, close_reason, opened_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,1,'cross',%s,%s,%s,%s,%s,%s)""",
            (user_id, symbol, asset_class, side, actual_size, float(entry or 0),
             round(float(close_px), 5), round(realized, 2), round(fees, 2),
             round(float(funding_paid or 0), 2), round(float(swap_paid or 0), 2),
             reason, opened_at),
        )
        await cur.execute(
            """UPDATE trades_orders
               SET status='FILLED', closed_at=NOW(), close_reason=%s, realized_pnl=%s
               WHERE id=%s""",
            (reason, round(realized, 2), op_id),
        )
    if rows:
        await cur.execute(
            "UPDATE users_state SET used_margin=0, unrealized_pnl=0 WHERE user_id=%s",
            (user_id,),
        )


async def process_challenge_webhook(raw_body: bytes, signature: str):
    if not verify_webhook_signature(raw_body, signature):
        return False, "Invalid signature"

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:
        return False, "Invalid JSON"

    event = payload.get("event", "")
    if event == "payment.failed":
        payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
        failed_order_id = payment_entity.get("order_id")
        if failed_order_id:
            async with common.db_pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE challenge_payments SET status='failed' WHERE razorpay_order_id=%s AND status='created'",
                        (failed_order_id,),
                    )
                    await conn.commit()
        return True, "Payment failed recorded"
    if event not in ("order.paid", "payment.captured"):
        return True, "Ignored event"

    created_at = payload.get("created_at")
    # Reject genuinely stale/replayed events. Razorpay also retries failed deliveries;
    # once a valid payment row is marked paid, duplicate deliveries are harmless.
    if created_at:
        try:
            if abs(int(time.time()) - int(created_at)) > 300:
                return False, "Stale webhook"
        except (TypeError, ValueError):
            return False, "Invalid created_at"

    event_id = payload.get("id")
    order_entity = payload.get("payload", {}).get("order", {}).get("entity", {})
    payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
    order_id = order_entity.get("id") or payment_entity.get("order_id")
    payment_id = payment_entity.get("id")
    amount = payment_entity.get("amount") or order_entity.get("amount_paid") or order_entity.get("amount")

    if not order_id or int(amount or 0) != common.CHALLENGE_PRICE_INR * 100:
        return False, "Invalid challenge payment payload"

    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT id, user_id, status, razorpay_payment_id
                   FROM challenge_payments WHERE razorpay_order_id=%s""",
                (order_id,),
            )
            payment_row = await cur.fetchone()
            if not payment_row:
                logger.warning(f"[Razorpay] Unknown order_id received: {order_id}")
                return False, "Unknown order"

            payment_row_id, user_id, current_status, stored_payment_id = payment_row
            if current_status == "paid":
                return True, "Already processed"

            if event_id:
                await cur.execute(
                    """UPDATE challenge_payments
                       SET razorpay_event_id=%s,
                           razorpay_payment_id=COALESCE(%s, razorpay_payment_id),
                           status='paid', paid_at=NOW()
                       WHERE id=%s AND status<>'paid'""",
                    (event_id, payment_id, payment_row_id),
                )
            else:
                await cur.execute(
                    """UPDATE challenge_payments
                       SET razorpay_payment_id=COALESCE(%s, razorpay_payment_id),
                           status='paid', paid_at=NOW()
                       WHERE id=%s AND status<>'paid'""",
                    (payment_id, payment_row_id),
                )

            # Ensure stale/expired positions cannot survive into the new paid challenge.
            await _close_positions_for_challenge_reset(cur, user_id, "challenge_reset")

            # Every successful daily payment starts a completely fresh 24-hour challenge.
            await cur.execute(
                """UPDATE users_state
                   SET challenge_active_until=DATE_ADD(NOW(), INTERVAL 24 HOUR),
                       challenge_paid_at=NOW(),
                       challenge_payment_id=%s,
                       challenge_order_id=%s,
                       challenge_status='active',
                       assessment_step=1,
                       assessment_completed=FALSE,
                       onboarding_completed=TRUE,
                       onboarding_step=5,
                       step1_method=NULL,
                       step2_method=NULL,
                       step_start_balance=100000.00,
                       starting_balance=100000.00,
                       balance=100000.00,
                       equity=100000.00,
                       daily_start_balance=100000.00,
                       used_margin=0.00,
                       free_margin=100000.00,
                       margin_level=0.00,
                       realized_pnl=0.00,
                       unrealized_pnl=0.00,
                       total_fees=0.00,
                       total_funding=0.00,
                       total_swap=0.00,
                       peak_equity=100000.00,
                       max_drawdown_breached=FALSE,
                       daily_drawdown_used=0.00
                   WHERE user_id=%s""",
                (payment_id, order_id, user_id),
            )
            await conn.commit()

    logger.info(f"[Razorpay] Challenge activated: user={user_id} order={order_id} payment={payment_id}")
    return True, "Challenge activated"


async def get_challenge_status(user_id: str):
    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT upstox_verified, challenge_active_until,
                          challenge_paid_at, challenge_payment_id, challenge_order_id,
                          challenge_status
                   FROM users_state WHERE user_id=%s""",
                (user_id,),
            )
            row = await cur.fetchone()
    if not row:
        return {"error": "State not found"}

    upstox_verified, active_until, paid_at, payment_id, order_id, status = row
    active = False
    if active_until:
        async with common.db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT %s > NOW()", (active_until,))
                active = bool((await cur.fetchone())[0])
    return {
        "upstox_verified": bool(upstox_verified),
        "active": active,
        "active_until": str(active_until) if active_until else None,
        "paid_at": str(paid_at) if paid_at else None,
        "payment_id": payment_id,
        "challenge_order_id": order_id,
        "status": status or "inactive",
    }
