export function renderCryptoView(symbol) {
    const symbolTitle = document.getElementById('symbol-title');
    if (symbolTitle) symbolTitle.innerText = `${symbol} Perpetual`;
    
    const orderBook = document.getElementById('order-book');
    if (orderBook) orderBook.innerHTML = `
        <h4>Order Book</h4>
        <div style="display: flex; gap: 10px;">
            <div style="flex: 1;">
                <div style="text-align: center; color: #848e9c; font-size: 10px;">Bids</div>
                <div id="crypto-bids"></div>
            </div>
            <div style="flex: 1;">
                <div style="text-align: center; color: #848e9c; font-size: 10px;">Asks</div>
                <div id="crypto-asks"></div>
            </div>
        </div>
    `;
    
    const orderForm = document.getElementById('order-form');
    if (orderForm) orderForm.innerHTML = `
        <h4>Place Order (Crypto)</h4>
        <select id="order-type" onchange="toggleCryptoOrderFields()">
            <option value="market">Market</option>
            <option value="limit">Limit</option>
            <option value="stop_market">Stop Market</option>
            <option value="trailing_stop">Trailing Stop</option>
        </select>
        
        <div id="trigger-price-field" style="display: none;">
            <input type="number" id="trigger-price" placeholder="Trigger Price" step="0.01">
        </div>
        
        <div id="trailing-dist-field" style="display: none;">
            <input type="number" id="trailing-distance" placeholder="Trailing Distance ($)" step="0.01">
        </div>
        
        <input type="number" id="order-size" value="0.1" placeholder="Size" step="0.01">
        <div style="display: flex; gap: 5px;">
            <input type="number" id="stop-loss" placeholder="Stop Loss" step="0.01" style="width: 50%;">
            <input type="number" id="take-profit" placeholder="Take Profit" step="0.01" style="width: 50%;">
        </div>
        <div style="display: flex; gap: 5px;">
            <input type="number" id="leverage" value="10" placeholder="Leverage" min="1" max="100" style="width: 50%;">
            <select id="margin-mode" style="width: 50%;">
                <option value="cross">Cross</option>
                <option value="isolated">Isolated</option>
            </select>
        </div>
        
        <select id="tif">
            <option value="GTC">GTC (Good Till Cancelled)</option>
            <option value="IOC">IOC (Immediate or Cancel)</option>
            <option value="FOK">FOK (Fill or Kill)</option>
            <option value="POST_ONLY">POST_ONLY</option>
        </select>
        
        <button class="btn-trade btn-buy" onclick="submitCryptoOrder('buy')">Buy / Long</button>
        <button class="btn-trade btn-sell" onclick="submitCryptoOrder('sell')">Sell / Short</button>
        <p style="font-size: 10px; color: #848e9c;">Market orders simulate slippage based on order book depth. Latency is simulated (50-250ms).</p>
    `;
    
    const optionsChain = document.getElementById('options-chain');
    if (optionsChain) optionsChain.innerHTML = '<h4>Options Chain (Dynamic IV)</h4><div id="options-data">Loading...</div>';
    
    fetchOptionsChain(symbol);
}

async function fetchOptionsChain(symbol) {
    const res = await fetch(`/api/options?symbol=${symbol}`);
    if (!res.ok) return;
    const data = await res.json();
    const chainDiv = document.getElementById('options-data');
    if(!chainDiv) return;
    
    if (!data.chain || data.chain.length === 0) {
        chainDiv.innerHTML = '<div style="font-size: 12px; color: #848e9c;">Generating chain...</div>';
        return;
    }

    const expiry = data.chain[0].expiry;
    const allStrikes = data.chain.filter(c => c.expiry === expiry);
    const midIndex = Math.floor(allStrikes.length / 2);
    const calls = allStrikes.slice(midIndex - 2, midIndex + 3);
    
    chainDiv.innerHTML = `
        <div class="data-row" style="font-weight: bold; color: #f0b90b;">
            <span>Spot: ${data.spot.toFixed(2)}</span>
            <span>Expiry: ${expiry}</span>
        </div>
        <table>
            <tr><th>Strike</th><th>IV</th><th>Call</th><th>Put</th></tr>
            ${calls.map(c => `
                <tr>
                    <td>${c.strike}</td>
                    <td>${(c.iv * 100).toFixed(1)}%</td>
                    <td style="color: #0ecb81">${c.call_premium.toFixed(2)}</td>
                    <td style="color: #f6465d">${c.put_premium.toFixed(2)}</td>
                </tr>
            `).join('')}
        </table>
    `;
}

window.toggleCryptoOrderFields = function() {
    const type = document.getElementById('order-type').value;
    const triggerField = document.getElementById('trigger-price-field');
    const trailingField = document.getElementById('trailing-dist-field');
    
    if (triggerField) triggerField.style.display = ['limit', 'stop_market'].includes(type) ? 'block' : 'none';
    if (trailingField) trailingField.style.display = type === 'trailing_stop' ? 'block' : 'none';
}

window.submitCryptoOrder = async (side) => {
    const order_type = document.getElementById('order-type').value;
    const size = parseFloat(document.getElementById('order-size').value);
    const leverage = parseInt(document.getElementById('leverage').value);
    const margin_mode = document.getElementById('margin-mode').value;
    const tif = document.getElementById('tif').value;
    
    const payload = { 
        asset_class: 'crypto', 
        symbol: window.currentSymbol, 
        side, size, order_type, leverage, margin_mode, tif
    };
    
    const trigger = document.getElementById('trigger-price');
    if (trigger && trigger.value) payload.trigger_price = parseFloat(trigger.value);
    
    const trail = document.getElementById('trailing-distance');
    if (trail && trail.value) payload.trailing_distance = parseFloat(trail.value);
    const sl = document.getElementById('stop-loss');
    const tp = document.getElementById('take-profit');
    if (sl && sl.value) payload.stop_loss = parseFloat(sl.value);
    if (tp && tp.value) payload.take_profit = parseFloat(tp.value);
    const response = await fetch('/api/order', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
    });
    
    if(response.ok) console.log("Crypto order submitted");
}

export function renderCryptoData(cryptoState) {
    const symData = cryptoState[window.currentSymbol];
    if (!symData) return;
    
    const asksDiv = document.getElementById('crypto-asks');
    const bidsDiv = document.getElementById('crypto-bids'); 
    if (asksDiv && bidsDiv) {
        asksDiv.innerHTML = symData.order_book.asks.slice(0, 8).map(a => 
            `<div class="data-row ask"><span>${a.price}</span><span>${a.size}</span></div>`
        ).join('');
        
        bidsDiv.innerHTML = symData.order_book.bids.slice(0, 8).map(b => 
            `<div class="data-row bid"><span>${b.price}</span><span>${b.size}</span></div>`
        ).join('');
    }
}