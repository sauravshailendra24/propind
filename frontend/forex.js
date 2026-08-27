export function renderForexView(symbol) {
    const symbolTitle = document.getElementById('symbol-title');
    if (symbolTitle) symbolTitle.innerText = `${symbol} Spot`;
    
    const orderBook = document.getElementById('order-book');
    if (orderBook) orderBook.innerHTML = '<h4>Market Depth</h4><div id="forex-depth"></div>';
    
    const orderForm = document.getElementById('order-form');
    if (orderForm) orderForm.innerHTML = `
        <h4>Place Order (Forex)</h4>
        <select id="order-type" onchange="toggleForexOrderFields()">
            <option value="market">Market</option>
            <option value="limit">Limit</option>
            <option value="stop_market">Stop Market</option>
        </select>
        
        <div id="trigger-price-field" style="display: none;">
            <input type="number" id="trigger-price" placeholder="Trigger Price" step="0.0001">
        </div>
        
        <select id="lot-size">
            <option value="1.0">1.0 Lot (Standard)</option>
            <option value="0.1">0.1 Lot (Mini)</option>
            <option value="0.01">0.01 Lot (Micro)</option>
        </select>
        <div style="display: flex; gap: 5px;">
            <input type="number" id="stop-loss" placeholder="Stop Loss" step="0.0001" style="width: 50%;">
            <input type="number" id="take-profit" placeholder="Take Profit" step="0.0001" style="width: 50%;">
        </div>
        <button class="btn-trade btn-buy" onclick="submitForexOrder('buy')">Buy</button>
        <button class="btn-trade btn-sell" onclick="submitForexOrder('sell')">Sell</button>
        <p style="font-size: 10px; color: #848e9c;">Spreads widen dynamically based on active trading sessions.</p>
    `;
    
    const optionsChain = document.getElementById('options-chain');
    if (optionsChain) optionsChain.innerHTML = '<h4>Forex Options</h4><div style="font-size: 12px; color: #848e9c;">Simplified Vanilla Options available via API.</div>';
}

window.toggleForexOrderFields = function() {
    const type = document.getElementById('order-type').value;
    const triggerField = document.getElementById('trigger-price-field');
    if (triggerField) triggerField.style.display = type === 'market' ? 'none' : 'block';
}

window.submitForexOrder = async (side) => {
    const order_type = document.getElementById('order-type').value;
    const size = parseFloat(document.getElementById('lot-size').value);
    const payload = { 
        asset_class: 'forex', 
        symbol: window.currentSymbol, 
        side, size, order_type,
        lot_type: 'standard'
    };
    
    const trigger = document.getElementById('trigger-price');
    if (trigger && trigger.value) payload.trigger_price = parseFloat(trigger.value);
    const sl = document.getElementById('stop-loss');
    const tp = document.getElementById('take-profit');
    if (sl && sl.value) payload.stop_loss = parseFloat(sl.value);
    if (tp && tp.value) payload.take_profit = parseFloat(tp.value);
    await fetch('/api/order', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
    });
}

export function renderForexData(forexState, sessions) {
    const symData = forexState[window.currentSymbol];
    if (!symData) return;
    
    const depthDiv = document.getElementById('forex-depth');
    if (depthDiv) {
        depthDiv.innerHTML = `
            <div class="data-row bid"><span>Bid</span><span>${symData.bid}</span></div>
            <div class="data-row ask"><span>Ask</span><span>${symData.ask}</span></div>
            <hr style="border-color: #2c2f36;">
            <div class="data-row"><span>Spread:</span><span>${symData.spread_pips} pips</span></div>
            <div class="data-row"><span>Daily High:</span><span>${symData.daily_high}</span></div>
            <div class="data-row"><span>Daily Low:</span><span>${symData.daily_low}</span></div>
            <hr style="border-color: #2c2f36;">
            <div class="data-row"><span>Active Sessions:</span><span style="color: #f0b90b; font-size: 10px;">${sessions.join(', ')}</span></div>
        `;
    }
}