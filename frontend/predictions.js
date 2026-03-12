const API_BASE = window.location.origin;

// ---- Theme Toggle ----
function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('cw-theme', theme);
}

// Load saved theme (default: dark)
const savedTheme = localStorage.getItem('cw-theme') || 'dark';
applyTheme(savedTheme);

document.addEventListener('DOMContentLoaded', () => {
    const themeToggle = document.getElementById('themeToggle');
    if (themeToggle) {
        themeToggle.addEventListener('click', () => {
            const current = document.documentElement.getAttribute('data-theme') || 'dark';
            applyTheme(current === 'dark' ? 'light' : 'dark');
        });
    }

    updateClock();
    setInterval(updateClock, 1000);
    fetchPredictions();
});

function updateClock() {
    const clockEl = document.getElementById('clock');
    if (clockEl) {
        const now = new Date();
        const utcStr = now.toUTCString().replace('GMT', 'UTC');
        clockEl.textContent = utcStr.substring(0, utcStr.length - 4) + ' UTC';
    }
}

function escapeHtml(unsafe) {
    if (!unsafe) return '';
    return unsafe
        .toString()
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

function timeAgo(dateString) {
    const date = new Date(dateString);
    const now = new Date();
    const diffSeconds = Math.floor((now - date) / 1000);

    if (diffSeconds < 60) return `${diffSeconds}s ago`;
    const diffMinutes = Math.floor(diffSeconds / 60);
    if (diffMinutes < 60) return `${diffMinutes}m ago`;
    const diffHours = Math.floor(diffMinutes / 60);
    if (diffHours < 24) return `${diffHours}h ago`;
    const diffDays = Math.floor(diffHours / 24);
    return `${diffDays}d ago`;
}

function formatTime(dateString) {
    const d = new Date(dateString);
    return d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false }) + ' UTC';
}

async function fetchPredictions() {
    try {
        const res = await fetch(`${API_BASE}/api/predictions`);
        const json = await res.json();
        const loader = document.getElementById('predictionsLoader');
        const content = document.getElementById('predictionsContent');
        const emptyState = document.getElementById('emptyState');

        if (loader) loader.style.display = 'none';

        if (json.status === 'success' && json.data.length > 0) {
            content.style.display = 'block';
            emptyState.style.display = 'none';
            renderPredictions(json.data, content);
        } else {
            content.style.display = 'none';
            emptyState.style.display = 'flex';
        }
    } catch (e) {
        console.error('Error fetching predictions:', e);
        const err = document.createElement('div');
        err.style.color = 'var(--bearish)';
        err.textContent = 'Failed to load predictions. Ensure backend is running.';
        document.getElementById('predictionsContent').appendChild(err);
    }
}

function renderPredictions(predictions, container) {
    const _symMap = {
        "BTC-USD": "Bitcoin", "ETH-USD": "Ethereum", "SOL-USD": "Solana",
        "XRP-USD": "Ripple", "ADA-USD": "Cardano", "DOGE-USD": "Dogecoin",
        "AVAX-USD": "Avalanche", "LINK-USD": "Chainlink", "DOT-USD": "Polkadot",
        "LTC-USD": "Litecoin", "UNI-USD": "Uniswap", "SHIB-USD": "Shiba Inu",
        "MATIC-USD": "Polygon",
        "EURUSD=X": "EUR/USD", "USDJPY=X": "USD/JPY", "GBPUSD=X": "GBP/USD",
        "USDCHF=X": "USD/CHF", "AUDUSD=X": "AUD/USD", "USDCAD=X": "USD/CAD",
        "NZDUSD=X": "NZD/USD", "DX-Y.NYB": "US Dollar Index (DXY)",
        "GC=F": "Gold", "CL=F": "Crude Oil", "SI=F": "Silver",
        "^GSPC": "S&P 500", "NQ=F": "NASDAQ", "^DJI": "Dow Jones",
        "^N225": "Nikkei 225", "^GDAXI": "DAX", "^FTSE": "FTSE 100"
    };

    const formatSymbol = sym => _symMap[sym] || sym;
    const formatPrice = p => {
        if (!p) return '0.00';
        const v = parseFloat(p);
        if (v < 0.01) return v.toFixed(6);
        if (v < 200) return v.toFixed(4);
        return v.toFixed(2);
    };

    let html = '<div class="predictions-list">';
    
    predictions.forEach(p => {
        const status = (p.status || 'pending').toLowerCase();
        const dir = (p.direction || 'Neutral').toLowerCase();
        const isBull = dir === 'bullish' || dir === 'positive' || dir === 'up';
        const isBear = dir === 'bearish' || dir === 'negative' || dir === 'down';
        const dirEmoji = isBull ? '📈' : (isBear ? '📉' : '➖');

        // Map status to our new classes
        const statusCls = status === 'expired' ? 'missed' 
                       : status === 'wrong' ? 'missed' 
                       : status === 'overperformed' ? 'underrated'
                       : status === 'underperformed' ? 'overstated'
                       : status;

        const finalMove = p.final_move_pct != null ? parseFloat(p.final_move_pct).toFixed(2) : (p.last_move_pct != null ? parseFloat(p.last_move_pct).toFixed(2) : '0.00');
        const mfeRaw = p.mfe_pct != null ? parseFloat(p.mfe_pct) : 0;
        
        // MFE & Target Signs
        const mfeSign = isBear ? -1 : 1;
        const mfeDisplay = (mfeSign * mfeRaw).toFixed(2);
        const mfePrefix = mfeSign * mfeRaw > 0 ? '+' : '';
        
        const targetPctRaw = p.predicted_move_pct ? parseFloat(p.predicted_move_pct) : 0;
        let targetNum = isBear ? -targetPctRaw : targetPctRaw; // Real % move
        const targetDisplay = (isBear ? '-' : '+') + targetPctRaw;
        
        // Absolute colors
        const curPct = parseFloat(finalMove);
        const moveColor = curPct > 0 ? 'var(--bullish)' : (curPct < 0 ? 'var(--bearish)' : 'var(--text-muted)');
        const mfeColor = mfeSign * mfeRaw > 0 ? 'var(--bullish)' : (mfeSign * mfeRaw < 0 ? 'var(--bearish)' : 'inherit');
        const barColor = isBull ? 'var(--bullish)' : isBear ? 'var(--bearish)' : 'var(--text-muted)';
        const biasBorderColor = isBull ? 'rgba(0, 212, 170, 0.4)' : isBear ? 'rgba(255, 71, 87, 0.4)' : 'rgba(255, 193, 7, 0.4)';
        const dirCls = isBull ? 'dir-bullish' : isBear ? 'dir-bearish' : 'dir-neutral';

        // Target Price Logic
        const startPriceFloat = parseFloat(p.start_price || 0);
        const currentPriceFloat = parseFloat(p.final_price || p.last_price || p.start_price);
        const targetPriceFloat = startPriceFloat * (1 + (targetNum / 100));

        // Progress to Target (0 to 100%) - based on max favorable
        let mfeProgressPct = 0;
        if (targetPctRaw > 0) {
            mfeProgressPct = Math.max(0, Math.min(100, (mfeRaw / targetPctRaw) * 100));
        }

        // News linkage
        const sourceLinkHTML = p.news_title ? `
            <a href="${p.news_link ? escapeHtml(p.news_link) : '#'}" class="prediction-source-link" target="_blank" rel="noopener noreferrer">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M4 22h14a2 2 0 0 0 2-2V7l-5-5H6a2 2 0 0 0-2 2v4"></path>
                    <path d="M14 2v4a2 2 0 0 0 2 2h4"></path>
                    <path d="m3 15 4-4 4 4"></path>
                    <path d="M7 11v11"></path>
                </svg>
                ${escapeHtml(p.news_title.substring(0, 80) + (p.news_title.length > 80 ? '...' : ''))}
            </a>
        ` : '';

        html += `
            <div class="forex-pair-card" style="border-left: 4px solid ${biasBorderColor}; margin-bottom: 24px; padding: 18px; border-radius: 8px; box-shadow: 0 4px 14px rgba(0,0,0,0.08); background: var(--bg-card); border-top: 1px solid var(--border); border-right: 1px solid var(--border); border-bottom: 1px solid var(--border); transition: all 0.2s ease;">
                <!-- Header -->
                <div style="display: flex; justify-content: space-between; align-items: flex-start;">
                    <div>
                        <div style="display: flex; align-items: center; gap: 8px;">
                            <span class="forex-pair-name" style="font-size:1.15rem; font-weight:700;">${escapeHtml(p.asset_display_name || formatSymbol(p.asset))}</span>
                            ${p.direction ? `<span class="forex-pair-dir ${dirCls}" style="padding:2px 8px; font-size:0.65rem; border-radius:12px; background: var(--bg-secondary); border: 1px solid var(--border);">${escapeHtml(p.direction.toUpperCase())}</span>` : ''}
                        </div>
                        <div style="font-size: 0.8rem; color: var(--text-muted); margin-top: 6px;">
                            Duration: <strong style="color: var(--text-secondary);">${escapeHtml(p.expected_duration_label)}</strong>
                             · Generated ${timeAgo(p.created_at)}
                        </div>
                    </div>
                    <span class="pred-status-full pred-status-${statusCls}" style="padding: 4px 12px; font-size:0.75rem;">${(statusCls).toUpperCase()}</span>
                </div>

                <!-- Source Catalyst Link -->
                ${sourceLinkHTML}

                <!-- 3-Column Stats Grid -->
                <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-top: 24px; padding: 16px; background: var(--bg-secondary); border-radius: 8px; border: 1px solid var(--border);">
                    <div>
                        <div style="font-size:0.65rem; color:var(--text-muted); text-transform:uppercase; letter-spacing:0.5px; margin-bottom: 4px;">Start Price</div>
                        <div style="font-family:'JetBrains Mono',monospace; font-size:1rem; font-weight:700; color:var(--text-primary);">$${formatPrice(startPriceFloat)}</div>
                        <div style="font-size: 0.75rem; color: var(--text-muted); margin-top: 4px;">Entry</div>
                    </div>
                    <div>
                        <div style="font-size:0.65rem; color:var(--text-muted); text-transform:uppercase; letter-spacing:0.5px; margin-bottom: 4px;">${status === 'pending' ? 'Current Price' : 'Final Price'}</div>
                        <div style="font-family:'JetBrains Mono',monospace; font-size:1rem; font-weight:700; color:var(--text-primary);">$${formatPrice(currentPriceFloat)}</div>
                        <div style="font-size: 0.75rem; color: ${moveColor}; margin-top: 4px; font-weight: 600;">${curPct > 0 ? '+' : ''}${finalMove}%</div>
                    </div>
                    <div>
                        <div style="font-size:0.65rem; color:var(--text-muted); text-transform:uppercase; letter-spacing:0.5px; margin-bottom: 4px;">Target Price</div>
                        <div style="font-family:'JetBrains Mono',monospace; font-size:1rem; font-weight:700; color:${barColor};">$${formatPrice(targetPriceFloat)}</div>
                        <div style="font-size: 0.75rem; color: ${barColor}; margin-top: 4px; font-weight: 600;">${targetDisplay}%</div>
                    </div>
                </div>

                <!-- Max Favorable Progress Bar -->
                <div style="margin-top: 24px;">
                    <div style="display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 8px;">
                        <div style="font-size: 0.8rem; color: var(--text-secondary);">
                            Max Favorable Excursion <span style="color: var(--text-muted); font-size: 0.7rem; margin-left:4px;">(Best Outcome)</span>
                        </div>
                        <div style="font-family:'JetBrains Mono',monospace; font-size: 1.05rem; font-weight: 700; color: ${mfeColor};">
                            ${mfePrefix}${mfeDisplay}%
                        </div>
                    </div>
                    
                    <div style="position: relative; height: 8px; background: rgba(128,128,128,0.15); border-radius: 4px; overflow: hidden; box-shadow: inset 0 1px 3px rgba(0,0,0,0.1);">
                        <div style="position: absolute; left: 0; top: 0; height: 100%; width: ${mfeProgressPct}%; background: ${barColor}; opacity: ${mfeProgressPct >= 100 ? '1' : '0.8'}; border-radius: 4px;"></div>
                    </div>
                    <div style="display: flex; justify-content: space-between; margin-top: 8px; font-size: 0.7rem; color: var(--text-muted);">
                        <span>0%</span>
                        <span>Progress strictly towards Target (${targetDisplay}%)</span>
                        <span>100%</span>
                    </div>
                </div>
            </div>
        `;
    });
    html += '</div>';

    container.innerHTML = html;
}
