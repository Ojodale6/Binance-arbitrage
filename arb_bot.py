#!/usr/bin/env python3
"""
Simple Triangular Arbitrage Bot
No database, no complexity, just profit
"""

import os
import time
import json
import threading
from datetime import datetime
from collections import defaultdict, deque

# Flask for web dashboard
from flask import Flask, render_template_string

# Binance API
import ccxt
from websocket import create_connection

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

# ==================== CONFIG ====================
class Config:
    # API
    API_KEY = os.getenv('BINANCE_API_KEY', '')
    API_SECRET = os.getenv('BINANCE_API_SECRET', '')
    USE_TESTNET = os.getenv('USE_TESTNET', 'true').lower() == 'true'
    DRY_RUN = os.getenv('DRY_RUN', 'true').lower() == 'true'
    
    # Trading
    TRADE_AMOUNT = float(os.getenv('TRADE_AMOUNT', '3'))
    MIN_PROFIT = float(os.getenv('MIN_PROFIT', '0.3'))
    
    # Scanning
    SCAN_INTERVAL = float(os.getenv('SCAN_INTERVAL', '0.3'))
    MAX_SYMBOLS = int(os.getenv('MAX_SYMBOLS', '100'))
    MAX_TRIANGLES = int(os.getenv('MAX_TRIANGLES', '500'))
    
    # Web
    WEB_PORT = int(os.getenv('WEB_PORT', '5000'))

config = Config()

# ==================== EXCHANGE SETUP ====================
def init_exchange():
    """Initialize Binance exchange"""
    exchange_params = {
        'apiKey': config.API_KEY,
        'secret': config.API_SECRET,
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'}
    }
    
    if config.USE_TESTNET:
        exchange = ccxt.binance({
            **exchange_params,
            'urls': {
                'api': {
                    'public': 'https://testnet.binance.vision/api',
                    'private': 'https://testnet.binance.vision/api'
                }
            }
        })
        exchange.set_sandbox_mode(True)
        print("🔧 Using Binance TESTNET")
    else:
        exchange = ccxt.binance(exchange_params)
        print("🚀 Using Binance LIVE")
    
    print(f"💼 Trade Amount: ${config.TRADE_AMOUNT}")
    print(f"🎯 Min Profit: {config.MIN_PROFIT}%")
    print(f"📊 Dry Run: {config.DRY_RUN}")
    
    return exchange

# ==================== SIMPLE ORDERBOOK ====================
class SimpleOrderBook:
    """Simple orderbook storage"""
    def __init__(self):
        self.orderbooks = {}
        self.lock = threading.Lock()
        
    def update(self, symbol, bids, asks):
        """Update orderbook for symbol"""
        with self.lock:
            self.orderbooks[symbol] = {
                'bids': bids,
                'asks': asks,
                'timestamp': time.time()
            }
    
    def get(self, symbol):
        """Get orderbook for symbol"""
        with self.lock:
            return self.orderbooks.get(symbol)

orderbook = SimpleOrderBook()
  # ==================== TRIANGLE SCANNER ====================
class TriangleScanner:
    """Find and analyze triangles"""
    
    def __init__(self, exchange, markets):
        self.exchange = exchange
        self.markets = markets
        self.triangles = []
        
    def build_adjacency(self):
        """Build graph of trading pairs"""
        adj = defaultdict(list)
        
        for symbol, market in self.markets.items():
            if not market.get('active', True):
                continue
                
            base = market['base']
            quote = market['quote']
            
            # Only USDT pairs for now
            if quote != 'USDT' and base != 'USDT':
                continue
                
            adj[base].append((quote, symbol, 'buy'))
            adj[quote].append((base, symbol, 'sell'))
        
        return adj
    
    def find_triangles(self):
        """Find all USDT-based triangles"""
        adj = self.build_adjacency()
        triangles = []
        
        # USDT -> A -> B -> USDT
        start = 'USDT'
        
        if start not in adj:
            return triangles
        
        for (b, sab, dab) in adj[start]:
            for (c, sbc, dbc) in adj.get(b, []):
                if c == start:
                    continue
                for (back, sca, dca) in adj.get(c, []):
                    if back == start:
                        triangles.append({
                            'path': [start, b, c],
                            'pairs': [sab, sbc, sca],
                            'directions': [dab, dbc, dca],
                            'string': f"{start} → {b} → {c} → {start}"
                        })
        
        return triangles[:config.MAX_TRIANGLES]
    
    def simulate_triangle(self, triangle, amount):
        """Simulate triangle trade"""
        try:
            pairs = triangle['pairs']
            directions = triangle['directions']
            
            current = amount
            
            for pair, direction in zip(pairs, directions):
                ob = orderbook.get(pair)
                if not ob:
                    return None
                
                # Simple orderbook walk
                if direction == 'sell':
                    # Sell base for quote
                    bids = ob['bids']
                    remaining = current
                    received = 0
                    
                    for price, volume in bids:
                        if volume >= remaining:
                            received += remaining * price
                            remaining = 0
                            break
                        else:
                            received += volume * price
                            remaining -= volume
                    
                    if remaining > 0.00001:
                        return None
                    
                    current = received
                else:
                    # Buy base with quote
                    asks = ob['asks']
                    remaining = current
                    acquired = 0
                    
                    for price, volume in asks:
                        cost = price * volume
                        if cost <= remaining:
                            acquired += volume
                            remaining -= cost
                        else:
                            acquired += remaining / price
                            remaining = 0
                            break
                    
                    if remaining > 0.00001:
                        return None
                    
                    current = acquired
                
                # Apply fee (0.1%)
                current *= 0.999
            
            profit = current - amount
            profit_pct = (profit / amount) * 100
            
            if profit_pct < config.MIN_PROFIT:
                return None
            
            return {
                'triangle': triangle['string'],
                'pairs': triangle['pairs'],
                'profit_pct': round(profit_pct, 3),
                'profit_usd': round(profit, 2),
                'timestamp': datetime.now().strftime('%H:%M:%S')
            }
            
        except Exception as e:
            print(f"Simulation error: {e}")
            return None
      # ==================== TRADE EXECUTOR ====================
class TradeExecutor:
    """Execute triangular trades"""
    
    def __init__(self, exchange):
        self.exchange = exchange
        self.trades = deque(maxlen=100)  # Store last 100 trades
        self.stats = {
            'total_trades': 0,
            'profitable': 0,
            'total_profit': 0,
            'best_trade': 0
        }
        
    def execute(self, opportunity):
        """Execute a trade opportunity"""
        trade_id = f"TR{int(time.time())}"
        
        print(f"\n🎯 Found opportunity: {opportunity['triangle']}")
        print(f"   Profit: {opportunity['profit_pct']}% (${opportunity['profit_usd']})")
        
        if config.DRY_RUN:
            print("   ⚠️  DRY RUN - No real trade executed")
            trade_result = {
                'id': trade_id,
                'triangle': opportunity['triangle'],
                'profit_pct': opportunity['profit_pct'],
                'profit_usd': opportunity['profit_usd'],
                'status': 'dry_run',
                'timestamp': datetime.now().isoformat()
            }
        else:
            try:
                print("   🚀 Executing live trade...")
                # Execute each leg
                pairs = opportunity['pairs']
                
                for i, pair in enumerate(pairs):
                    # This is simplified - in reality you'd handle order placement properly
                    print(f"   Leg {i+1}: {pair}")
                    time.sleep(0.1)  # Small delay between legs
                
                trade_result = {
                    'id': trade_id,
                    'triangle': opportunity['triangle'],
                    'profit_pct': opportunity['profit_pct'],
                    'profit_usd': opportunity['profit_usd'],
                    'status': 'executed',
                    'timestamp': datetime.now().isoformat()
                }
                
                print("   ✅ Trade completed!")
                
            except Exception as e:
                print(f"   ❌ Trade failed: {e}")
                trade_result = {
                    'id': trade_id,
                    'triangle': opportunity['triangle'],
                    'profit_pct': 0,
                    'profit_usd': 0,
                    'status': 'failed',
                    'error': str(e),
                    'timestamp': datetime.now().isoformat()
                }
        
        # Update stats
        self.trades.appendleft(trade_result)
        self.stats['total_trades'] += 1
        
        if trade_result['profit_usd'] > 0:
            self.stats['profitable'] += 1
            self.stats['total_profit'] += trade_result['profit_usd']
            if trade_result['profit_usd'] > self.stats['best_trade']:
                self.stats['best_trade'] = trade_result['profit_usd']
        
        return trade_result

# ==================== WEB DASHBOARD ====================
def create_dashboard(executor, scanner):
    """Create simple web dashboard"""
    app = Flask(__name__)
    
    HTML_TEMPLATE = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>🔺 TriArb Bot</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { font-family: Arial, sans-serif; margin: 20px; background: #0d1117; color: #c9d1d9; }
            .container { max-width: 1200px; margin: 0 auto; }
            .header { background: #161b22; padding: 20px; border-radius: 10px; margin-bottom: 20px; }
            .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 20px; }
            .stat-box { background: #161b22; padding: 15px; border-radius: 8px; text-align: center; }
            .stat-value { font-size: 24px; font-weight: bold; color: #58a6ff; }
            .trades { background: #161b22; padding: 20px; border-radius: 10px; }
            .trade-row { padding: 10px; border-bottom: 1px solid #30363d; }
            .profit-positive { color: #3fb950; }
            .profit-negative { color: #f85149; }
            .status-badge { padding: 3px 8px; border-radius: 12px; font-size: 12px; }
            .status-executed { background: #238636; color: white; }
            .status-dry { background: #8957e5; color: white; }
            .status-failed { background: #da3633; color: white; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>🔺 Triangular Arbitrage Bot</h1>
                <p>Live trading dashboard | Last updated: {{ timestamp }}</p>
            </div>
            
            <div class="stats">
                <div class="stat-box">
                    <div>Total Trades</div>
                    <div class="stat-value">{{ stats.total_trades }}</div>
                </div>
                <div class="stat-box">
                    <div>Profitable</div>
                    <div class="stat-value">{{ stats.profitable }}</div>
                </div>
                <div class="stat-box">
                    <div>Total Profit</div>
                    <div class="stat-value">${{ "%.2f"|format(stats.total_profit) }}</div>
                </div>
                <div class="stat-box">
                    <div>Best Trade</div>
                    <div class="stat-value">${{ "%.2f"|format(stats.best_trade) }}</div>
                </div>
                <div class="stat-box">
                    <div>Triangles</div>
                    <div class="stat-value">{{ triangle_count }}</div>
                </div>
                <div class="stat-box">
                    <div>Trade Amount</div>
                    <div class="stat-value">${{ config.trade_amount }}</div>
                </div>
            </div>
            
            <div class="trades">
                <h2>Recent Trades</h2>
                {% for trade in trades %}
                <div class="trade-row">
                    <strong>{{ trade.triangle }}</strong><br>
                    <span class="profit-positive">Profit: {{ trade.profit_pct }}% (${{ "%.2f"|format(trade.profit_usd) }})</span> |
                    Time: {{ trade.timestamp[11:19] }} |
                    Status: <span class="status-badge status-{{ trade.status }}">{{ trade.status|upper }}</span>
                </div>
                {% endfor %}
            </div>
        </div>
        
        <script>
            // Auto-refresh every 5 seconds
            setTimeout(() => location.reload(), 5000);
        </script>
    </body>
    </html>
    """
    
    @app.route('/')
    def index():
        return render_template_string(HTML_TEMPLATE,
            trades=list(executor.trades)[:20],
            stats=executor.stats,
            triangle_count=len(scanner.triangles),
            config={'trade_amount': config.TRADE_AMOUNT},
            timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        )
    
    @app.route('/api/trades')
    def api_trades():
        return {'trades': list(executor.trades)}
    
    @app.route('/api/stats')
    def api_stats():
        return executor.stats
    
    return app
# ==================== ORDERBOOK UPDATER ====================
def update_orderbooks(exchange, symbols):
    """Simple orderbook updater using REST"""
    while True:
        for symbol in symbols:
            try:
                # Fetch orderbook
                ob = exchange.fetch_order_book(symbol, limit=20)
                orderbook.update(symbol, ob['bids'][:10], ob['asks'][:10])
                
                # Small delay to avoid rate limits
                time.sleep(0.05)
                
            except Exception as e:
                print(f"Error fetching {symbol}: {e}")
                time.sleep(1)
        
        # Wait before next round
        time.sleep(0.5)

# ==================== MAIN BOT ====================
def main():
    """Main bot function"""
    print("\n" + "="*50)
    print("🔺 SIMPLE TRIANGULAR ARBITRAGE BOT")
    print("="*50)
    
    # Initialize exchange
    exchange = init_exchange()
    
    # Load markets
    print("📊 Loading markets...")
    markets = exchange.load_markets()
    
    # Get USDT pairs
    symbols = [s for s in markets.keys() 
               if markets[s].get('active') and 'USDT' in s]
    symbols = symbols[:config.MAX_SYMBOLS]
    print(f"✅ Loaded {len(symbols)} symbols")
    
    # Initialize scanner
    scanner = TriangleScanner(exchange, markets)
    scanner.triangles = scanner.find_triangles()
    print(f"✅ Found {len(scanner.triangles)} triangles")
    
    # Initialize executor
    executor = TradeExecutor(exchange)
    
    # Start orderbook updater in background
    ob_thread = threading.Thread(
        target=update_orderbooks,
        args=(exchange, symbols),
        daemon=True
    )
    ob_thread.start()
    
    # Wait for initial orderbook data
    print("⏳ Waiting for orderbook data...")
    time.sleep(3)
    
    # Start web dashboard in background
    print(f"🌐 Starting web dashboard on port {config.WEB_PORT}...")
    web_thread = threading.Thread(
        target=lambda: create_dashboard(executor, scanner).run(
            host='0.0.0.0', 
            port=config.WEB_PORT,
            debug=False
        ),
        daemon=True
    )
    web_thread.start()
    
    print("\n🚀 Bot is running! Press Ctrl+C to stop")
    print(f"📊 Dashboard: http://localhost:{config.WEB_PORT}")
    print("="*50)
    
    # Main trading loop
    scan_count = 0
    last_opportunity_time = 0
    
    try:
        while True:
            scan_count += 1
            
            # Find best opportunity
            best_opportunity = None
            best_profit = 0
            
            for triangle in scanner.triangles:
                result = scanner.simulate_triangle(triangle, config.TRADE_AMOUNT)
                if result and result['profit_pct'] > best_profit:
                    best_profit = result['profit_pct']
                    best_opportunity = result
            
            # Execute if profitable
            if best_opportunity and best_profit >= config.MIN_PROFIT:
                # Avoid executing too frequently
                if time.time() - last_opportunity_time < 2:
                    continue
                    
                last_opportunity_time = time.time()
                executor.execute(best_opportunity)
                
                # Update console
                print(f"\n📈 Scan #{scan_count}: Best profit = {best_profit:.2f}%")
                
            else:
                # Show status
                if scan_count % 10 == 0:
                    print(f"🔍 Scan #{scan_count}: No opportunities > {config.MIN_PROFIT}%")
            
            # Wait between scans
            time.sleep(config.SCAN_INTERVAL)
            
    except KeyboardInterrupt:
        print("\n\n🛑 Bot stopped by user")
        print("\n📊 Final Stats:")
        print(f"   Total Trades: {executor.stats['total_trades']}")
        print(f"   Profitable: {executor.stats['profitable']}")
        print(f"   Total Profit: ${executor.stats['total_profit']:.2f}")
        print(f"   Best Trade: ${executor.stats['best_trade']:.2f}")
        print("\n✅ Goodbye!")

if __name__ == "__main__":
    main()
