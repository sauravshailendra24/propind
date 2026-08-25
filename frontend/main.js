import { renderCryptoView, renderCryptoData } from '/static/crypto.js';
import { renderForexView, renderForexData } from '/static/forex.js';

let currentView = 'crypto';
let currentSymbol = 'BTC/USDT';
let currentTimeframe = '1m';
let ws = null;
let chart = null;
let candleSeries = null;
let priceLine = null;
let isLoginMode = true;
let lastCandle = null;
let resizeObserver = null;

const CRYPTO_SYMBOLS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT'];
const FOREX_SYMBOLS = ['EUR/USD', 'GBP/USD', 'USD/JPY', 'AUD/USD'];

window.currentSymbol = 'BTC/USDT';

window.addEventListener('DOMContentLoaded', () => {
    checkAuth();
});

async function checkAuth() {
    const res = await fetch('/api/auth/me');
    if (res.ok) {
        document.getElementById('logout-btn').style.display = 'block';
        initApp();
    } else {
        window.location.href = '/';
    }
}

window.handleAuth = async function() {
    const username = document.getElementById('auth-username').value;
    const password = document.getElementById('auth-password').value;
    const name = document.getElementById('auth-name').value;
    const whatsapp = document.getElementById('auth-whatsapp').value;
    const opt_in = whatsapp.length > 0;

    const endpoint = isLoginMode ? '/api/auth/login' : '/api/auth/signup';
    const payload = isLoginMode
        ? { username, password }
        : { username, password, name, whatsapp_opt_in: opt_in, whatsapp_number: whatsapp };

    const res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    });

    if (res.ok) {
        checkAuth();
    } else {
        alert('Auth failed');
    }
}

window.toggleAuthMode = function() {
    isLoginMode = !isLoginMode;
    document.getElementById('auth-title').innerText = isLoginMode ? 'Login' : 'Sign Up';
    document.getElementById('auth-password').style.display = 'block';
    document.getElementById('auth-name').style.display = isLoginMode ? 'none' : 'block';
    document.getElementById('auth-whatsapp').style.display = isLoginMode ? 'none' : 'block';
}

window.logout = async function() {
    await fetch('/api/auth/logout', { method: 'POST' });
    location.reload();
}

function initApp() {
    const chartElement = document.getElementById('chart-container');

    // Use actual container height, fallback to 400 if not yet rendered
    const initialHeight = chartElement.clientHeight || 400;
    const initialWidth = chartElement.clientWidth || 600;

    chart = LightweightCharts.createChart(chartElement, {
        width: initialWidth,
        height: initialHeight,
        layout: {
            background: { type: 'solid', color: 'transparent' },
            textColor: '#848e9c',
            attributionLogo: false
        },
        grid: {
            vertLines: { color: 'rgba(44, 47, 54, 0.5)' },
            horzLines: { color: 'rgba(44, 47, 54, 0.5)' },
        },
        crosshair: { mode: 0 },
        rightPriceScale: { borderColor: 'rgba(44, 47, 54, 0.5)' },
        timeScale: { borderColor: 'rgba(44, 47, 54, 0.5)' }
    });

    candleSeries = chart.addCandlestickSeries({
        upColor: '#0ecb81', downColor: '#f6465d',
        borderVisible: false, wickUpColor: '#0ecb81', wickDownColor: '#f6465d'
    });

    // ── Auto-resize chart when container dimensions change ──
    resizeObserver = new ResizeObserver(entries => {
        if (!chart || !chartElement) return;
        const { width, height } = entries[0].contentRect;
        if (width > 0 && height > 0) {
            chart.applyOptions({ width: Math.floor(width), height: Math.floor(height) });
        }
    });
    resizeObserver.observe(chartElement);

    initDrawingTools();
    loadView('crypto', null);
    connectWebSocket();
    fetchStateLoop();
}

// ─── FIXED: now accepts ev parameter ───
window.loadView = function(view, ev) {
    currentView = view;
    document.querySelectorAll('#nav-buttons button').forEach(b => b.classList.remove('active'));
    if (ev && ev.target) ev.target.classList.add('active');

    const selector = document.getElementById('symbol-selector');
    const symbols = view === 'crypto' ? CRYPTO_SYMBOLS : FOREX_SYMBOLS;
    selector.innerHTML = symbols.map(s => `<option value="${s}">${s}</option>`).join('');

    currentSymbol = symbols[0];
    window.currentSymbol = currentSymbol;
    changeSymbol(currentSymbol);
}

window.changeSymbol = function(symbol) {
    currentSymbol = symbol;
    window.currentSymbol = symbol;
    if (currentView === 'crypto') {
        renderCryptoView(symbol);
    } else {
        renderForexView(symbol);
    }
    loadCandles();
}

// ─── FIXED: only affects #timeframe-selector buttons, not drawing buttons ───
window.changeTimeframe = function(tf, ev) {
    currentTimeframe = tf;
    document.querySelectorAll('#timeframe-selector .tf-btn').forEach(b => b.classList.remove('active'));
    if (ev && ev.target) ev.target.classList.add('active');
    loadCandles();
}

async function loadCandles() {
    const res = await fetch(`/api/candles?symbol=${currentSymbol}&timeframe=${currentTimeframe}`);
    if (!res.ok) return;
    const data = await res.json();
    const candles = data.candles.map(c => ({
        time: Math.floor(new Date(c.timestamp).getTime() / 1000),
        open: c.open, high: c.high, low: c.low, close: c.close
    }));

    candleSeries.setData(candles);
    if (candles.length > 0) {
        lastCandle = candles[candles.length - 1];
    }
    clearDrawings();
}

function connectWebSocket() {
    // Use relative URL so it works regardless of host/port
    const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${wsProtocol}//${window.location.host}/ws`;
    ws = new WebSocket(wsUrl);

    ws.onmessage = async (event) => {
        const data = JSON.parse(event.data);
        if (currentView === 'crypto') {
            renderCryptoData(data.crypto);
        } else {
            renderForexData(data.forex, data.sessions);
        }

                // ── If server broadcast a newly closed candle, push it as a new bar ──
        if (data.last_candle
            && data.last_candle.symbol === currentSymbol
            && data.last_candle.timeframe === currentTimeframe) {
            const newCandle = {
                time: data.last_candle.timestamp,
                open: data.last_candle.open,
                high: data.last_candle.high,
                low: data.last_candle.low,
                close: data.last_candle.close
            };
            // Only push if it's a new candle (different time)
            if (!lastCandle || lastCandle.time !== newCandle.time) {
                candleSeries.update(newCandle);
                lastCandle = newCandle;
            }
        }
        const symData = currentView === 'crypto' ? data.crypto[currentSymbol] : data.forex[currentSymbol];
        if (symData && lastCandle) {
            const markPrice = currentView === 'crypto' ? symData.mark : symData.bid;
            const updated = {
                time: lastCandle.time,
                open: lastCandle.open,
                high: Math.max(lastCandle.high, markPrice),
                low: Math.min(lastCandle.low, markPrice),
                close: markPrice
            };
            lastCandle = updated;
            candleSeries.update(lastCandle);

            if (!priceLine) {
                priceLine = candleSeries.createPriceLine({
                    price: markPrice, color: '#f0b90b', lineWidth: 1, lineStyle: 2
                });
            } else {
                priceLine.applyOptions({ price: markPrice });
            }
        }
    };
    ws.onopen = () => console.log('WebSocket connected');
    ws.onclose = () => setTimeout(connectWebSocket, 1000);
}

async function fetchStateLoop() {
    while (true) {
        try {
            const [stateRes, onbRes] = await Promise.all([
                fetch('/api/state'), fetch('/api/onboarding/state')
            ]);
            if (stateRes.ok) {
                const state = await stateRes.json();
                updateDashboard(state);
                updatePositions(state.positions);
                updateAnalytics();
            }
            if (onbRes.ok) {
                const onb = await onbRes.json();
                updateAssessmentBanner(onb);
            }
        } catch (e) {}
        await new Promise(r => setTimeout(r, 2000));
    }
}

function updateAssessmentBanner(onb) {
    const banner = document.getElementById('assessment-banner');
    if (!banner) return;

    if (onb.assessment_completed) {
        banner.innerHTML = `
            <div style="background: rgba(14,203,129,0.1); border: 1px solid rgba(14,203,129,0.4); padding: 12px; border-radius: 8px;">
                <strong style="color: #0ecb81;">🎉 Assessment Completed!</strong><br>
                <span style="font-size: 12px; color: #848e9c;">You will be contacted very soon via your registered email.</span>
            </div>`;
        return;
    }

    if (!onb.upstox_verified) {
        window.location.href = '/upstox-verify';
        return;
    }

    // Show ONLY current step — never reveal future steps
    const step = onb.assessment_step;
    const target = onb.target_balance;
    const equity = onb.current_equity;
    const pct = Math.min(100, (equity / target) * 100);
    const ddLimit = onb.daily_drawdown_limit;
    const ddUsed = Math.max(0, onb.daily_start_balance - equity);
    const ddRemaining = ddLimit - ddUsed;

    let html = `
        <div style="background: rgba(240,185,11,0.08); border: 1px solid rgba(240,185,11,0.3); padding: 12px; border-radius: 8px; margin-bottom: 10px;">
            <div style="display: flex; justify-content: space-between; margin-bottom: 8px;">
                <strong style="color: #f0b90b;">Step ${step}</strong>
                <span style="font-size: 12px; color: #848e9c;">Target: $${target.toFixed(2)}</span>
            </div>
            <div style="background: #2c2f36; border-radius: 4px; height: 8px; overflow: hidden; margin-bottom: 6px;">
                <div style="background: linear-gradient(90deg, #f0b90b, #0ecb81); width: ${pct.toFixed(1)}%; height: 100%; transition: width 0.5s;"></div>
            </div>
            <div style="display: flex; justify-content: space-between; font-size: 11px; color: #848e9c;">
                <span>Equity: $${equity.toFixed(2)}</span>
                <span>${pct.toFixed(1)}%</span>
            </div>
            <div style="font-size: 11px; color: ${ddRemaining > 0 ? '#848e9c' : '#f6465d'}; margin-top: 4px;">
                Daily DD remaining: $${ddRemaining.toFixed(2)} / $${ddLimit.toFixed(2)}
            </div>`;

    if (onb.stock360s_pending) {
        html += `
            <div style="background: rgba(14,203,129,0.08); border: 1px dashed rgba(14,203,129,0.3); padding: 8px; border-radius: 6px; margin-top: 8px; font-size: 11px; color: #0ecb81;">
                ⏳ Stock360s purchase confirmed. Waiting for step advancement...
            </div>`;
    } else if (step < 3) {
        html += `
            <div style="margin-top: 8px;">
                <button class="btn-close" onclick="buyStock360s(${step})" style="width: 100%; padding: 6px; font-size: 11px;">
                    Skip with Stock360s
                </button>
            </div>`;
    }

    html += `</div>`;
    banner.innerHTML = html;
}

window.buyStock360s = async function(currentStep) {
    const plan = currentStep === 1 ? '1mo' : '1yr';
    const url = currentStep === 1
        ? 'https://stock360s.com/#landing-pricing'
        : 'https://stock360s.com/#landing-pricing';
    window.open(url, '_blank');
    await fetch('/api/stock360s/purchase', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ plan })
    });
    alert('Purchase recorded! Confirmation within 1 hour. You will be advanced automatically.');
}

function updateDashboard(state) {
    const acc = state.account;
    document.getElementById('account-info').innerText = `Balance: $${acc.balance.toFixed(2)} | Equity: $${acc.equity.toFixed(2)}`;
    document.getElementById('acc-balance').innerText = `$${acc.balance.toFixed(2)}`;
    document.getElementById('acc-equity').innerText = `$${acc.equity.toFixed(2)}`;
    document.getElementById('acc-used-margin').innerText = `$${acc.used_margin.toFixed(2)}`;
    document.getElementById('acc-free-margin').innerText = `$${acc.free_margin.toFixed(2)}`;
    document.getElementById('acc-margin-level').innerText = `${acc.margin_level.toFixed(2)}%`;
    document.getElementById('acc-unrealized').innerText = `$${acc.unrealized_pnl.toFixed(2)}`;
    document.getElementById('acc-realized').innerText = `$${acc.realized_pnl.toFixed(2)}`;
    document.getElementById('acc-fees').innerText = `$${(acc.fees + acc.funding + acc.swap).toFixed(2)}`;
}

function updatePositions(positions) {
    const tbody = document.getElementById('positions-body');
    if (!tbody || !positions) return;
    tbody.innerHTML = positions.map(p => `
        <tr>
            <td>${p.symbol}</td>
            <td style="color: ${p.side === 'buy' ? '#0ecb81' : '#f6465d'}">${p.side.toUpperCase()}</td>
            <td>${p.size.toFixed(4)}</td>
            <td>${p.entry_price.toFixed(5)}</td>
            <td>${p.mark_price !== undefined ? p.mark_price.toFixed(5) : '...'}</td>
            <td style="color: ${p.pnl >= 0 ? '#0ecb81' : '#f6465d'}">${p.pnl !== undefined ? '$' + p.pnl.toFixed(2) : '...'}</td>
            <td><button class="btn-close" onclick="closePosition(${p.id})">Close</button></td>
        </tr>
    `).join('');
}

window.closePosition = async function(id) {
    await fetch(`/api/close/${id}`, { method: 'POST' });
}

async function updateAnalytics() {
    const res = await fetch('/api/analytics');
    if (!res.ok) return;
    const a = await res.json();
    const analyticsDiv = document.getElementById('analytics');
    analyticsDiv.innerHTML = `
        <h4>Performance Analytics</h4>
        <div class="data-row"><span>Win Rate:</span><span>${a.win_rate}%</span></div>
        <div class="data-row"><span>Profit Factor:</span><span>${a.profit_factor}</span></div>
        <div class="data-row"><span>Expectancy:</span><span>$${a.expectancy}</span></div>
        <div class="data-row"><span>Max Drawdown:</span><span>${a.max_drawdown}%</span></div>
        <div class="data-row"><span>Sharpe Ratio:</span><span>${a.sharpe}</span></div>
        <div class="data-row"><span>Liquidations:</span><span>${a.liquidations}</span></div>
        <div class="data-row"><span>Total Trades:</span><span>${a.total_trades}</span></div>
    `;
}

// ─── Drawing Tools Logic ─────────────────────────────────────
let activeTool = 'none';
let isDrawing = false;
let startX = 0, startY = 0;
let currentElement = null;
const svg = document.getElementById('drawing-overlay');

function initDrawingTools() {
    if (!svg) return;

    svg.addEventListener('mousedown', (e) => {
        if (activeTool === 'none') return;
        isDrawing = true;
        const rect = svg.getBoundingClientRect();
        startX = e.clientX - rect.left;
        startY = e.clientY - rect.top;

        if (activeTool === 'line') {
            currentElement = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            currentElement.setAttribute('stroke', '#f0b90b');
            currentElement.setAttribute('stroke-width', '1.5');
        } else if (activeTool === 'rect') {
            currentElement = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            currentElement.setAttribute('stroke', '#f0b90b');
            currentElement.setAttribute('fill', 'rgba(240, 185, 11, 0.2)');
            currentElement.setAttribute('stroke-width', '1.5');
        }

        if (currentElement) {
            currentElement.setAttribute('x1', startX);
            currentElement.setAttribute('y1', startY);
            currentElement.setAttribute('x2', startX);
            currentElement.setAttribute('y2', startY);
            svg.appendChild(currentElement);
        }
    });

    // Attach to window so drawing continues even if cursor leaves the chart bounds
    window.addEventListener('mousemove', (e) => {
        if (!isDrawing || !currentElement) return;
        const rect = svg.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;

        if (activeTool === 'line') {
            currentElement.setAttribute('x2', x);
            currentElement.setAttribute('y2', y);
        } else if (activeTool === 'rect') {
            const width = x - startX;
            const height = y - startY;
            currentElement.setAttribute('x', width < 0 ? x : startX);
            currentElement.setAttribute('y', height < 0 ? y : startY);
            currentElement.setAttribute('width', Math.abs(width));
            currentElement.setAttribute('height', Math.abs(height));
        }
    });

    // Attach to window so mouseup is always caught
    window.addEventListener('mouseup', () => {
        isDrawing = false;
        currentElement = null;
    });
}

window.setDrawingTool = function(tool) {
    activeTool = tool;

    // Reset all drawing buttons
    ['draw-line', 'draw-rect', 'draw-clear', 'draw-none'].forEach(id => {
        const btn = document.getElementById(id);
        if (btn) btn.classList.remove('active');
    });

    // Highlight active drawing tool
    const activeId = tool === 'line' ? 'draw-line'
                   : tool === 'rect' ? 'draw-rect'
                   : 'draw-none';
    const activeBtn = document.getElementById(activeId);
    if (activeBtn) activeBtn.classList.add('active');

    // Toggle pointer events directly on the SVG
    svg.style.pointerEvents = tool === 'none' ? 'none' : 'all';
}

window.clearDrawings = function() {
    if (svg) svg.innerHTML = '';
    window.setDrawingTool('none');
}
