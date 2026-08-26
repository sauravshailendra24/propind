import logging
import math
import hashlib
import secrets
import os
from dotenv import load_dotenv
import aiomysql
from decimal import Decimal

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("PropInd-Common")

db_pool = None

SYMBOLS_CONFIG = {
    'crypto': {
        'BTC/USDT': {'price': 64500.0, 'vol': 50.0, 'base_iv': 0.55, 'tick_size': 0.5, 'strike_step': 500},
        'ETH/USDT': {'price': 3450.0,  'vol': 2.5,  'base_iv': 0.65, 'tick_size': 0.05, 'strike_step': 25},
        'SOL/USDT': {'price': 145.0,   'vol': 1.5,  'base_iv': 0.75, 'tick_size': 0.01, 'strike_step': 5},
        'BNB/USDT': {'price': 580.0,   'vol': 2.0,  'base_iv': 0.70, 'tick_size': 0.05, 'strike_step': 10},
    },
    'forex': {
        'EUR/USD': {'price': 1.0850, 'vol': 0.0005, 'base_iv': 0.07, 'pip_size': 0.0001, 'swap_long': -0.00002, 'swap_short': 0.00001},
        'GBP/USD': {'price': 1.2700, 'vol': 0.0006, 'base_iv': 0.08, 'pip_size': 0.0001, 'swap_long': -0.00003, 'swap_short': 0.00002},
        'USD/JPY': {'price': 149.50, 'vol': 0.05,   'base_iv': 0.09, 'pip_size': 0.01,   'swap_long': -0.005,  'swap_short': 0.003},
        'AUD/USD': {'price': 0.6580, 'vol': 0.0004, 'base_iv': 0.10, 'pip_size': 0.0001, 'swap_long': -0.00001,'swap_short': 0.000005},
    }
}

MAKER_FEE_CRYPTO = 0.0002   # 0.02%
TAKER_FEE_CRYPTO  = 0.0005   # 0.05%
MAKER_FEE_FOREX   = 0.0001
TAKER_FEE_FOREX   = 0.0003
MAINT_MARGIN_RATE_CRYPTO = 0.005   # 0.5% of notional
MAINT_MARGIN_RATE_FOREX  = 0.01    # 1%
MARGIN_CALL_THRESHOLD   = 0.50    # 50% margin level
STOP_OUT_THRESHOLD      = 0.20

# Stock360s redirect URL
STOCK360S_PURCHASE_URL = 'https://stock360s.com/#landing-pricing'

# Upstox referral URL
UPSTOX_REFERRAL_URL = 'https://upstox.com/open-account/?f=4XCT4L'  # replace with your real referral

# Assessment targets (multiplier on step_start_balance)
ASSESSMENT_TARGETS = {1: 1.10, 2: 1.20, 3: 1.40}
ASSESSMENT_DAILY_DD_PCT = 0.05   # 5% daily drawdown
ASSESSMENT_MAX_DD_PCT   = 0.10   # 10% max overall drawdown

# SL/TP restrictions (applied for both crypto & forex in assessment mode)
SL_MAX_DISTANCE_PCT = 0.05      # SL must be within 5% of entry
SL_MIN_DISTANCE_PCT = 0.005     # SL must be at least 0.5% from entry
TP_MIN_DISTANCE_PCT = 0.01      # If TP set, must be ≥1% from entry
SL_MANDATORY = True             # SL required for every order
ALLOWED_TIMEFRAMES = ('1m', '5m', '15m', '1h')

load_dotenv()

async def init_db():
    global db_pool
    db_pool = await aiomysql.create_pool(
        host=os.getenv('DB_HOST', '127.0.0.1'),
        port=int(os.getenv('DB_PORT', 3306)),
        user=os.getenv('DB_USER', 'propind_user'),
        password=os.getenv('DB_PASSWORD', 'password'),
        db=os.getenv('DB_NAME', 'propind'),
        autocommit=True
    )
    async with db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id VARCHAR(36) PRIMARY KEY,
                    username VARCHAR(50) UNIQUE NOT NULL,
                    name VARCHAR(100) NOT NULL,
                    password_hash VARCHAR(255) NOT NULL,
                    salt VARCHAR(64) NOT NULL,
                    whatsapp_opt_in BOOLEAN DEFAULT FALSE,
                    whatsapp_number VARCHAR(20),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS users_state (
                    user_id VARCHAR(36) PRIMARY KEY,
                    balance DECIMAL(15,2) DEFAULT 100000.00,
                    equity DECIMAL(15,2) DEFAULT 100000.00,
                    used_margin DECIMAL(15,2) DEFAULT 0.00,
                    free_margin DECIMAL(15,2) DEFAULT 100000.00,
                    margin_level DECIMAL(10,2) DEFAULT 0.00,
                    realized_pnl DECIMAL(15,2) DEFAULT 0.00,
                    unrealized_pnl DECIMAL(15,2) DEFAULT 0.00,
                    total_fees DECIMAL(15,2) DEFAULT 0.00,
                    total_funding DECIMAL(15,2) DEFAULT 0.00,
                    total_swap DECIMAL(15,2) DEFAULT 0.00,
                    peak_equity DECIMAL(15,2) DEFAULT 100000.00,
                    starting_balance DECIMAL(15,2) DEFAULT 100000.00,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS trades_orders (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    user_id VARCHAR(36),
                    asset_class ENUM('crypto','forex'),
                    symbol VARCHAR(20),
                    side ENUM('buy','sell'),
                    order_type ENUM('market','limit','stop_market','stop_limit','take_profit','stop_loss','trailing_stop'),
                    time_in_force ENUM('GTC','IOC','FOK','POST_ONLY','REDUCE_ONLY'),
                    size DECIMAL(18,8),
                    filled_size DECIMAL(18,8) DEFAULT 0,
                    avg_fill_price DECIMAL(15,5) DEFAULT 0,
                    trigger_price DECIMAL(15,5) DEFAULT 0,
                    take_profit_price DECIMAL(15,5) DEFAULT 0,
                    stop_loss_price DECIMAL(15,5) DEFAULT 0,
                    trailing_distance DECIMAL(15,5) DEFAULT 0,
                    trailing_trigger DECIMAL(15,5) DEFAULT 0,
                    status ENUM('PENDING','OPEN','PARTIAL','FILLED','CANCELLED','PARTIAL_LIQ','LIQUIDATED','STOP_OUT','MARGIN_CALL') DEFAULT 'PENDING',
                    margin_mode ENUM('cross','isolated') DEFAULT 'cross',
                    leverage INT DEFAULT 1,
                    initial_margin DECIMAL(15,2) DEFAULT 0,
                    maintenance_margin DECIMAL(15,2) DEFAULT 0,
                    liquidation_price DECIMAL(15,5) DEFAULT 0,
                    maker_fee DECIMAL(15,2) DEFAULT 0,
                    taker_fee DECIMAL(15,2) DEFAULT 0,
                    funding_paid DECIMAL(15,2) DEFAULT 0,
                    swap_paid DECIMAL(15,2) DEFAULT 0,
                    opened_at TIMESTAMP NULL,
                    closed_at TIMESTAMP NULL,
                    close_reason VARCHAR(20) NULL,
                    realized_pnl DECIMAL(15,2) DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_user_status (user_id, status),
                    INDEX idx_symbol (symbol)
                )
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS candles (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    symbol VARCHAR(20),
                    timeframe ENUM('1m','5m','15m','1h'),
                    open DECIMAL(15,5),
                    high DECIMAL(15,5),
                    low DECIMAL(15,5),
                    close DECIMAL(15,5),
                    timestamp DATETIME,
                    INDEX idx_symbol_tf_ts (symbol, timeframe, timestamp)
                )
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS closed_trades (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    user_id VARCHAR(36),
                    symbol VARCHAR(20),
                    asset_class ENUM('crypto','forex'),
                    side ENUM('buy','sell'),
                    size DECIMAL(18,8),
                    entry_price DECIMAL(15,5),
                    exit_price DECIMAL(15,5),
                    leverage INT DEFAULT 1,
                    margin_mode ENUM('cross','isolated'),
                    realized_pnl DECIMAL(15,2),
                    fees DECIMAL(15,2),
                    funding DECIMAL(15,2),
                    swap DECIMAL(15,2),
                    close_reason VARCHAR(20),
                    opened_at TIMESTAMP,
                    closed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_user (user_id)
                )
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS equity_snapshots (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    user_id VARCHAR(36),
                    equity DECIMAL(15,2),
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_user_ts (user_id, timestamp)
                )
            """)
            async def add_column_if_not_exists(table, column, definition):
                try:
                    await cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                except Exception as e:
                    if "Duplicate column" not in str(e):
                        logger.error(f"Migration error on {table}.{column}: {e}")

            await add_column_if_not_exists("trades_orders", "filled_size", "DECIMAL(18,8) DEFAULT 0")
            await add_column_if_not_exists("trades_orders", "trigger_price", "DECIMAL(15,5) DEFAULT 0")
            await add_column_if_not_exists("trades_orders", "take_profit_price", "DECIMAL(15,5) DEFAULT 0")
            await add_column_if_not_exists("trades_orders", "stop_loss_price", "DECIMAL(15,5) DEFAULT 0")
            await add_column_if_not_exists("trades_orders", "trailing_distance", "DECIMAL(15,5) DEFAULT 0")
            await add_column_if_not_exists("trades_orders", "trailing_trigger", "DECIMAL(15,5) DEFAULT 0")
            await add_column_if_not_exists("trades_orders", "status", "ENUM('PENDING','OPEN','PARTIAL','FILLED','CANCELLED','PARTIAL_LIQ','LIQUIDATED','STOP_OUT','MARGIN_CALL') DEFAULT 'PENDING'")
            await add_column_if_not_exists("trades_orders", "initial_margin", "DECIMAL(15,2) DEFAULT 0")
            await add_column_if_not_exists("trades_orders", "maintenance_margin", "DECIMAL(15,2) DEFAULT 0")
            await add_column_if_not_exists("trades_orders", "maker_fee", "DECIMAL(15,2) DEFAULT 0")
            await add_column_if_not_exists("trades_orders", "taker_fee", "DECIMAL(15,2) DEFAULT 0")
            await add_column_if_not_exists("trades_orders", "funding_paid", "DECIMAL(15,2) DEFAULT 0")
            await add_column_if_not_exists("trades_orders", "swap_paid", "DECIMAL(15,2) DEFAULT 0")
            await add_column_if_not_exists("trades_orders", "opened_at", "TIMESTAMP NULL")
            await add_column_if_not_exists("trades_orders", "closed_at", "TIMESTAMP NULL")
            await add_column_if_not_exists("trades_orders", "close_reason", "VARCHAR(20) NULL")
            await add_column_if_not_exists("trades_orders", "realized_pnl", "DECIMAL(15,2) DEFAULT 0")
            await add_column_if_not_exists("users_state", "used_margin", "DECIMAL(15,2) DEFAULT 0.00")
            await add_column_if_not_exists("users_state", "free_margin", "DECIMAL(15,2) DEFAULT 100000.00")
            await add_column_if_not_exists("users_state", "margin_level", "DECIMAL(10,2) DEFAULT 0.00")
            await add_column_if_not_exists("users_state", "unrealized_pnl", "DECIMAL(15,2) DEFAULT 0.00")
            await add_column_if_not_exists("users_state", "total_swap", "DECIMAL(15,2) DEFAULT 0.00")
            await add_column_if_not_exists("users_state", "peak_equity", "DECIMAL(15,2) DEFAULT 100000.00")
            await add_column_if_not_exists("users_state", "starting_balance", "DECIMAL(15,2) DEFAULT 100000.00")
            await add_column_if_not_exists("users_state", "daily_start_balance", "DECIMAL(15,2) DEFAULT 100000.00")
            await add_column_if_not_exists("users_state", "eval_start_date", "DATETIME NULL")
            await add_column_if_not_exists("users_state", "eval_passed", "BOOLEAN DEFAULT FALSE")
            await add_column_if_not_exists("users", "experience_level", "VARCHAR(20) DEFAULT NULL")
            await add_column_if_not_exists("users_state", "onboarding_step", "INT DEFAULT 0")
            await add_column_if_not_exists("users_state", "onboarding_completed", "BOOLEAN DEFAULT FALSE")
            await add_column_if_not_exists("users_state", "step1_method", "VARCHAR(20) DEFAULT NULL")
            await add_column_if_not_exists("users_state", "step2_method", "VARCHAR(20) DEFAULT NULL")
            await add_column_if_not_exists("users_state", "assessment_step", "INT DEFAULT 1")
            await add_column_if_not_exists("users_state", "step_start_balance", "DECIMAL(15,2) DEFAULT 100000.00")
            await add_column_if_not_exists("users_state", "stock360s_1mo_purchased", "BOOLEAN DEFAULT FALSE")
            await add_column_if_not_exists("users_state", "stock360s_1yr_purchased", "BOOLEAN DEFAULT FALSE")
            await add_column_if_not_exists("users_state", "stock360s_purchase_time", "TIMESTAMP NULL")
            await add_column_if_not_exists("users_state", "stock360s_confirmed", "BOOLEAN DEFAULT FALSE")
            await add_column_if_not_exists("users_state", "upstox_verified", "BOOLEAN DEFAULT FALSE")
            await add_column_if_not_exists("users_state", "assessment_completed", "BOOLEAN DEFAULT FALSE")
            await add_column_if_not_exists("users_state", "max_drawdown_breached", "BOOLEAN DEFAULT FALSE")
            await add_column_if_not_exists("users_state", "daily_drawdown_used", "DECIMAL(15,2) DEFAULT 0.00")
            await add_column_if_not_exists("users_state", "upstox_verify_request_time", "TIMESTAMP NULL")
    logger.info("Database initialized and tables verified.")

def hash_password(password: str, salt: str = None) -> tuple:
    if salt is None:
        salt = secrets.token_hex(32)
    hashed = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000)
    return hashed.hex(), salt

def verify_password(password: str, salt: str, stored_hash: str) -> bool:
    hashed, _ = hash_password(password, salt)
    return secrets.compare_digest(hashed, stored_hash)

def validate_sl_tp(side: str, entry_price: float, sl: float = None, tp: float = None):
    if SL_MANDATORY and (sl is None or sl <= 0):
        return False, "Stop-loss is mandatory for every order in assessment mode."

    if sl:
        sl_dist_pct = abs(sl - entry_price) / entry_price
        if sl_dist_pct > SL_MAX_DISTANCE_PCT:
            return False, f"Stop-loss too wide. Max {SL_MAX_DISTANCE_PCT*100:.1f}% from entry."
        if sl_dist_pct < SL_MIN_DISTANCE_PCT:
            return False, f"Stop-loss too tight. Min {SL_MIN_DISTANCE_PCT*100:.2f}% from entry."
        # SL must be on the correct side
        if side == 'buy' and sl >= entry_price:
            return False, "For buy/long, SL must be below entry price."
        if side == 'sell' and sl <= entry_price:
            return False, "For sell/short, SL must be above entry price."

    if tp and tp > 0:
        tp_dist_pct = abs(tp - entry_price) / entry_price
        if tp_dist_pct < TP_MIN_DISTANCE_PCT:
            return False, f"Take-profit too close. Min {TP_MIN_DISTANCE_PCT*100:.1f}% from entry."
        if side == 'buy' and tp <= entry_price:
            return False, "For buy/long, TP must be above entry price."
        if side == 'sell' and tp >= entry_price:
            return False, "For sell/short, TP must be below entry price."

    return True, None

def calculate_iv(base_iv, rv, tte, moneyness, regime, noise):
    skew = 0
    if moneyness < 1.0:
        skew = (1.0 - moneyness) * 20
    if moneyness > 1.0:
        skew = (moneyness - 1.0) * 10
    iv = base_iv + (rv * 0.5) + skew + regime + noise
    return max(iv, 0.05)

def black_scholes(s, k, t, r, sigma, option_type='call'):
    if t <= 0:
        return max(0.0, s - k) if option_type == 'call' else max(0.0, k - s)
    try:
        d1 = (math.log(s / k) + (r + 0.5 * sigma ** 2) * t) / (sigma * math.sqrt(t))
        d2 = d1 - sigma * math.sqrt(t)
        if option_type == 'call':
            price = s * 0.5 * (1 + math.erf(d1 / math.sqrt(2))) - k * math.exp(-r * t) * 0.5 * (1 + math.erf(d2 / math.sqrt(2)))
        else:
            price = k * math.exp(-r * t) * 0.5 * (1 + math.erf(-d2 / math.sqrt(2))) - s * 0.5 * (1 + math.erf(-d1 / math.sqrt(2)))
        return round(price, 4)
    except Exception:
        return 0.0

def calculate_greeks(s, k, t, r, sigma):
    if t <= 0:
        return {'delta': 1.0 if s > k else 0.0, 'gamma': 0.0, 'theta': 0.0, 'vega': 0.0}
    try:
        d1 = (math.log(s / k) + (r + 0.5 * sigma ** 2) * t) / (sigma * math.sqrt(t))
        d2 = d1 - sigma * math.sqrt(t)
        delta_call = 0.5 * (1 + math.erf(d1 / math.sqrt(2)))
        gamma = math.exp(-0.5 * d1 ** 2) / (s * sigma * math.sqrt(t) * math.sqrt(2 * math.pi))
        theta_call = (-s * sigma * math.exp(-0.5 * d1 ** 2) / (2 * math.sqrt(2 * math.pi * t)) 
                      - r * k * math.exp(-r * t) * 0.5 * (1 + math.erf(d2 / math.sqrt(2)))) / 365
        vega = s * math.exp(-0.5 * d1 ** 2) * math.sqrt(t) / math.sqrt(2 * math.pi) / 100
        return {'delta': round(delta_call, 4), 'gamma': round(gamma, 6), 
                'theta': round(theta_call, 4), 'vega': round(vega, 4)}
    except Exception:
        return {'delta': 0.0, 'gamma': 0.0, 'theta': 0.0, 'vega': 0.0}