import random
from datetime import datetime, timezone, timedelta
import common
from common import logger, SYMBOLS_CONFIG, calculate_iv, calculate_greeks, black_scholes

class MarketEngine:
    def __init__(self):
        self.state = {}
        self.current_candles = {}  
        self.candle_marks = {}  
        self.last_closed_candle = None
        logger.info("Market Engine initialized.")

    def init_symbols(self):
        for asset_class, symbols in SYMBOLS_CONFIG.items():
            for sym, cfg in symbols.items():
                is_crypto = (asset_class == 'crypto')
                self.state[sym] = {
                    'mark':            cfg['price'],
                    'index':           cfg['price'],
                    'impact':          0.0,
                    'vol':             cfg['vol'],
                    'asset':           asset_class,
                    'base_iv':         cfg['base_iv'],
                    'rv':              cfg['base_iv'] * 0.8,
                    'regime':          0.0,
                    'order_book':      {'bids': [], 'asks': []},
                    'funding_rate':    0.0001,
                    'funding_countdown': '04:00:00',
                    'open_interest':   round(random.uniform(0.5e9, 3e9), 2) if is_crypto else 0,
                    'daily_high':      cfg['price'],
                    'daily_low':       cfg['price'],
                    'volume_24h':      round(random.uniform(1e8, 5e9), 2) if is_crypto else round(random.uniform(50e9, 200e9), 2),
                    'tick_size':       cfg.get('tick_size', 0.0001),
                    'pip_size':        cfg.get('pip_size', 0.0001),
                    'swap_long':       cfg.get('swap_long', 0),
                    'swap_short':      cfg.get('swap_short', 0),
                    'spread_pips':     0.8 if not is_crypto else 0,
                    'bid':             cfg['price'],
                    'ask':             cfg['price'],
                    'strike_step':     cfg.get('strike_step', 50),
                }
                self.current_candles[sym] = {}
                now = datetime.now(timezone.utc)
                for tf in common.ALLOWED_TIMEFRAMES:
                    self.current_candles[sym][tf] = {
                        'open': cfg['price'], 'high': cfg['price'],
                        'low': cfg['price'], 'close': cfg['price']
                    }
                self.candle_marks[sym] = {
                    '1m':  now.minute,
                    '5m':  now.minute // 5,
                    '15m': now.minute // 15,
                    '1h':  now.hour,
                }
                self.generate_order_book(sym)
        logger.info(f"Initialized {len(self.state)} symbols (4 crypto + 4 forex).")

    def generate_order_book(self, sym):
        mark = self.state[sym]['mark']
        vol = self.state[sym]['vol']
        bids, asks = [], []
        for i in range(1, 21):
            bid_price = round(mark - i * vol * 0.3, 5)
            ask_price = round(mark + i * vol * 0.3, 5)
            size_mult = random.uniform(0.5, 3.0)
            if i % 5 == 0:
                size_mult *= 2.5  # Walls
            bids.append({'price': bid_price, 'size': round(random.uniform(0.05, 2.0) * size_mult, 4)})
            asks.append({'price': ask_price, 'size': round(random.uniform(0.05, 2.0) * size_mult, 4)})
        self.state[sym]['order_book'] = {'bids': bids, 'asks': asks}

    def get_forex_sessions(self):
        utc_hour = datetime.now(timezone.utc).hour
        sessions = []
        if 22 <= utc_hour or utc_hour < 7:  sessions.append('Sydney')
        if 0 <= utc_hour < 9:               sessions.append('Tokyo')
        if 7 <= utc_hour < 16:              sessions.append('London')
        if 12 <= utc_hour < 21:             sessions.append('New York')
        if 'London' in sessions and 'New York' in sessions:
            sessions.append('London/NY Overlap')
        return sessions

    def get_session_vol_multiplier(self, sessions):
        if 'London/NY Overlap' in sessions: return 2.0
        if 'London' in sessions:            return 1.5
        if 'New York' in sessions:           return 1.5
        if 'Tokyo' in sessions:              return 1.0
        if 'Sydney' in sessions:             return 0.5
        return 0.3

    def update_funding_countdown(self):
        now = datetime.now(timezone.utc)
        funding_hours = [0, 8, 16]
        next_funding = None
        for h in funding_hours:
            target = now.replace(hour=h, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(hours=8)
            if next_funding is None or target < next_funding:
                next_funding = target
        delta = next_funding - now
        total_sec = int(delta.total_seconds())
        h, rem = divmod(total_sec, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    def update_funding_rate(self, sym):
        current = self.state[sym]['funding_rate']
        drift = random.uniform(-0.00003, 0.00003)
        new_rate = max(-0.001, min(0.001, current + drift))
        self.state[sym]['funding_rate'] = round(new_rate, 6)

    def generate_options_chain(self, sym):
        data = self.state[sym]
        spot = data['index']
        base_iv = data['base_iv']
        rv = data['rv']
        regime = data['regime']
        strike_step = data['strike_step']
        now = datetime.now(timezone.utc)
        expiries = []
        eod = now.replace(hour=23, minute=59, second=0)
        expiries.append(('0DTE', eod.date()))
        days_to_fri = (4 - now.weekday()) % 7
        eow = (now + timedelta(days=days_to_fri)).replace(hour=8, minute=0, second=0)
        expiries.append(('EOW', eow.date()))
        if now.month == 12:
            eom = now.replace(day=1, year=now.year+1, month=1) - timedelta(days=1)
        else:
            eom = now.replace(day=1, month=now.month+1) - timedelta(days=1)
        expiries.append(('EOM', eom.date()))
        chain = []
        for label, expiry in expiries:
            tte = (datetime.combine(expiry, datetime.min.time(), tzinfo=timezone.utc) - now).total_seconds() / (365.25 * 24 * 3600)
            tte = max(tte, 1 / (24 * 3600))
            for offset in range(-5, 6):
                strike = round(spot + offset * strike_step, 2)
                if strike <= 0:
                    continue
                moneyness = spot / strike
                noise = random.uniform(-0.02, 0.02)
                iv = calculate_iv(base_iv, rv, tte, moneyness, regime, noise)
                call_premium = black_scholes(spot, strike, tte, 0.02, iv, 'call')
                put_premium = black_scholes(spot, strike, tte, 0.02, iv, 'put')
                greeks = calculate_greeks(spot, strike, tte, 0.02, iv)
                call_be = strike + call_premium
                put_be = strike - put_premium
                chain.append({
                    'expiry': label,
                    'expiry_date': str(expiry),
                    'strike': strike,
                    'call_premium': call_premium,
                    'put_premium': put_premium,
                    'iv': round(iv, 4),
                    'greeks': greeks,
                    'moneyness': round(moneyness, 4),
                    'call_break_even': round(call_be, 2),
                    'put_break_even': round(put_be, 2),
                })
        return chain

    async def tick(self):
        sessions = self.get_forex_sessions()
        vol_mult = self.get_session_vol_multiplier(sessions)
        for sym, data in self.state.items():
            local_mult = vol_mult if data['asset'] == 'forex' else 1.0
            if data['asset'] == 'forex' and random.random() < 0.01:
                local_mult *= 3.0
                data['spread_pips'] = round(random.uniform(2.5, 4.0), 2)
                logger.info(f"[News Event] {sym} spread widened to {data['spread_pips']} pips")
            elif data['asset'] == 'forex':
                data['spread_pips'] = round(random.uniform(0.5, 1.2), 2)
            drift = random.uniform(-1, 1) * data['vol'] * local_mult
            drift += data['impact']
            data['impact'] *= 0.5
            data['mark'] = round(data['mark'] + drift, 5)
            # ── Mean reversion + price floor ──
            base = SYMBOLS_CONFIG[data['asset']].get(sym, {}).get('price', data['mark'])
            if base > 0 and data['mark'] > 0:
                reversion_pull = (base - data['mark']) * 0.002
                data['mark'] = round(data['mark'] + reversion_pull, 5)
                # Absolute floor at 1% of base price
                floor = base * 0.01
                if data['mark'] < floor:
                    data['mark'] = round(floor, 5)
                    data['impact'] = abs(drift) * 0.5  # bounce back
            data['index'] = round(data['mark'] + random.uniform(-0.3, 0.3) * data['vol'], 5)
            data['daily_high'] = max(data['daily_high'], data['mark'])
            data['daily_low'] = min(data['daily_low'], data['mark'])
            if data['asset'] == 'forex':
                half_spread = data['spread_pips'] * data['pip_size'] / 2
                data['bid'] = round(data['mark'] - half_spread, 5)
                data['ask'] = round(data['mark'] + half_spread, 5)
            ret = drift / data['mark'] if data['mark'] != 0 else 0
            data['rv'] = data['rv'] * 0.95 + abs(ret) * 0.05 * 100
            data['regime'] = data['regime'] * 0.99 + random.uniform(-0.005, 0.005)
            self.generate_order_book(sym)
            if data['asset'] == 'crypto':
                data['funding_countdown'] = self.update_funding_countdown()
                if random.random() < 0.05:
                    self.update_funding_rate(sym)
            now = datetime.now(timezone.utc)
            for tf in common.ALLOWED_TIMEFRAMES:
                c = self.current_candles[sym][tf]
                c['close'] = data['mark']
                c['high'] = max(c['high'], data['mark'])
                c['low'] = min(c['low'], data['mark'])
            marks = self.candle_marks[sym]
            if now.minute != marks['1m']:
                await self._save_candle(sym, '1m', now)
                marks['1m'] = now.minute
            if now.minute // 5 != marks['5m']:
                await self._save_candle(sym, '5m', now)
                marks['5m'] = now.minute // 5
            if now.minute // 15 != marks['15m']:
                await self._save_candle(sym, '15m', now)
                marks['15m'] = now.minute // 15
            if now.hour != marks['1h']:
                await self._save_candle(sym, '1h', now)
                marks['1h'] = now.hour

    async def _save_candle(self, sym, tf, now):
        c = self.current_candles[sym][tf]
        async with common.db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO candles (symbol, timeframe, open, high, low, close, timestamp) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (sym, tf, c['open'], c['high'], c['low'], c['close'], now)
                )
        self.last_closed_candle = {
            'symbol': sym, 'timeframe': tf,
            'open': c['open'], 'high': c['high'],
            'low': c['low'], 'close': c['close'],
            'timestamp': int(now.timestamp())
        }
        logger.info(f"[Candle] {sym} {tf} — O:{c['open']} H:{c['high']} L:{c['low']} C:{c['close']}")
        c['open'] = c['close']
        c['high'] = c['close']
        c['low'] = c['close']

market_engine = MarketEngine()