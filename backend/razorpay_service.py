import hashlib
import hmac
import json
import time
import uuid
import httpx
import common
from common import logger

async def create_challenge_payment_link(user_id: str):
    if not common.RAZORPAY_KEY_ID or not common.RAZORPAY_KEY_SECRET:
        return {"error": "Razorpay is not configured on the server."}

    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT upstox_verified, challenge_active_until
                   FROM users_state
                   WHERE user_id=%s""",
                (user_id,)
            )

            row = await cur.fetchone()

            if not row:
                return {"error": "State not found."}

            upstox_verified, active_until = row

            if not upstox_verified:
                return {
                    "error":
                    "Please complete Upstox account verification first."
                }

            if active_until:
                await cur.execute(
                    "SELECT %s > NOW()",
                    (active_until,)
                )

                active_now = bool((await cur.fetchone())[0])

                if active_now:
                    return {
                        "status": "active",
                        "message": "Your challenge is already active.",
                        "active_until": str(active_until),
                    }

    amount_paise = common.CHALLENGE_PRICE_INR * 100

    unique_suffix = uuid.uuid4().hex[:12]

    reference_id = (
        f"PROPIND_{user_id[:20]}_{unique_suffix}"
    )[:40]

    callback_url = (
        "https://propind.onrender.com"
        "/api/challenge/payment-callback"
    )

    payload = {
        "amount": amount_paise,
        "currency": "INR",
        "accept_partial": False,

        "reference_id": reference_id,

        "description": "PropInd 4-Day Trading Challenge",

        "callback_url": callback_url,
        "callback_method": "get",

        "notes": {
            "user_id": user_id,
            "product": "propind_4_day_challenge"
        },

        "reminder_enable": False
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{common.RAZORPAY_API_BASE}/payment_links",
                auth=(
                    common.RAZORPAY_KEY_ID,
                    common.RAZORPAY_KEY_SECRET
                ),
                json=payload
            )

            response.raise_for_status()
            payment_link = response.json()

    except Exception as exc:
        logger.error(
            f"[Razorpay] Payment Link creation failed "
            f"for {user_id}: {exc}"
        )
        return {
            "error":
            "Unable to create payment link. Please try again."
        }

    payment_link_id = payment_link.get("id")

    if not payment_link_id:
        logger.error(
            f"[Razorpay] Payment Link response missing ID: "
            f"{payment_link}"
        )
        return {
            "error":
            "Razorpay returned an invalid payment link."
        }

    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO challenge_payments
                   (
                       user_id,
                       razorpay_payment_link_id,
                       razorpay_reference_id,
                       amount_paise,
                       status
                   )
                   VALUES (%s,%s,%s,%s,'created')""",
                (
                    user_id,
                    payment_link_id,
                    reference_id,
                    amount_paise
                )
            )

            await conn.commit()

    logger.info(
        f"[Razorpay] Payment Link created "
        f"user={user_id} "
        f"link={payment_link_id} "
        f"reference={reference_id}"
    )

    return {
        "status": "created",
        "payment_link_id": payment_link_id,
        "reference_id": reference_id,
        "short_url": payment_link.get("short_url"),
        "amount": amount_paise,
        "currency": "INR",
        "description": "4-day PropInd trading challenge"
    }

def verify_payment_link_signature(
    payment_link_id: str,
    reference_id: str,
    payment_link_status: str,
    payment_id: str,
    signature: str
) -> bool:

    if not common.RAZORPAY_KEY_SECRET or not signature:
        return False

    message = (
        f"{payment_link_id}|"
        f"{reference_id}|"
        f"{payment_link_status}|"
        f"{payment_id}"
    )

    expected = hmac.new(
        common.RAZORPAY_KEY_SECRET.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(
        expected,
        signature
    )

async def _close_positions_for_challenge_reset(cur, user_id: str, reason: str):
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

async def activate_challenge(
    user_id: str,
    payment_link_id: str,
    payment_id: str
):
    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:

            await _close_positions_for_challenge_reset(
                cur,
                user_id,
                "challenge_reset"
            )

            await cur.execute(
                """UPDATE users_state
                   SET challenge_active_until =
                           DATE_ADD(
                               NOW(),
                               INTERVAL %s HOUR
                           ),
                       challenge_paid_at=NOW(),
                       challenge_payment_id=%s,
                       challenge_order_id=NULL,
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
                (
                    common.CHALLENGE_DURATION_HOURS,
                    payment_id,
                    user_id
                )
            )

            await cur.execute(
                """UPDATE challenge_payments
                   SET status='paid',
                       razorpay_payment_id=%s,
                       paid_at=NOW()
                   WHERE razorpay_payment_link_id=%s
                     AND status<>'paid'""",
                (
                    payment_id,
                    payment_link_id
                )
            )

            await conn.commit()

    logger.info(
        f"[Razorpay] Challenge activated "
        f"user={user_id} "
        f"payment_link={payment_link_id} "
        f"payment={payment_id}"
    )

    return True

async def process_payment_link_callback(
    payment_link_id: str,
    reference_id: str,
    payment_link_status: str,
    payment_id: str,
    signature: str
):

    if payment_link_status != "paid":
        return False, "Payment not completed"

    if not verify_payment_link_signature(
        payment_link_id,
        reference_id,
        payment_link_status,
        payment_id,
        signature
    ):
        logger.error(
            "[Razorpay] Invalid Payment Link callback signature"
        )
        return False, "Invalid payment signature"

    async with common.db_pool.acquire() as conn:
        async with conn.cursor() as cur:

            await cur.execute(
                """SELECT user_id, amount_paise, status
                   FROM challenge_payments
                   WHERE razorpay_payment_link_id=%s
                     AND razorpay_reference_id=%s""",
                (
                    payment_link_id,
                    reference_id
                )
            )

            row = await cur.fetchone()

    if not row:
        logger.error(
            f"[Razorpay] Unknown Payment Link: "
            f"{payment_link_id}"
        )
        return False, "Unknown payment link"

    user_id, amount_paise, current_status = row

    if int(amount_paise) != common.CHALLENGE_PRICE_INR * 100:
        return False, "Invalid payment amount"

    if current_status == "paid":
        return True, "Already processed"

    # Confirm the actual Payment Link state directly with Razorpay.
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                f"{common.RAZORPAY_API_BASE}"
                f"/payment_links/{payment_link_id}",
                auth=(
                    common.RAZORPAY_KEY_ID,
                    common.RAZORPAY_KEY_SECRET
                )
            )

            response.raise_for_status()
            link_data = response.json()

    except Exception as exc:
        logger.error(
            f"[Razorpay] Failed to verify Payment Link "
            f"{payment_link_id}: {exc}"
        )
        return False, "Unable to verify payment with Razorpay"

    if link_data.get("status") != "paid":
        return False, "Razorpay payment is not marked paid"

    if int(link_data.get("amount_paid") or 0) < (
        common.CHALLENGE_PRICE_INR * 100
    ):
        return False, "Incorrect amount paid"

    await activate_challenge(
        user_id,
        payment_link_id,
        payment_id
    )

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
