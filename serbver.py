# server.py
from flask import Flask, jsonify
import subprocess
import threading
import os
import sys
import logging

app = Flask(__name__)

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@app.route('/')
def home():
    return jsonify({
        "status": "running",
        "service": "World Cup Poller",
        "endpoints": {
            "/poll": "Trigger the poller to check matches",
            "/scrape": "Trigger the scraper to fetch fixtures",
            "/health": "Health check"
        }
    })

@app.route('/health')
def health():
    return jsonify({"status": "healthy"})

@app.route('/poll')
def trigger_poller():
    """Run the poller in the background."""
    def run_poller():
        try:
            logger.info("🚀 Poller triggered via /poll endpoint")
            # Run poller.py as a subprocess
            result = subprocess.run(
                [sys.executable, 'poller.py'],
                capture_output=True,
                text=True,
                timeout=300  # 5 minutes max
            )
            logger.info(f"✅ Poller finished: {result.returncode}")
            if result.stdout:
                logger.info(f"📤 Poller output: {result.stdout[:500]}")
            if result.stderr:
                logger.error(f"❌ Poller error: {result.stderr[:500]}")
        except subprocess.TimeoutExpired:
            logger.error("❌ Poller timed out after 5 minutes")
        except Exception as e:
            logger.error(f"❌ Poller failed: {e}")
    
    # Run in background thread so HTTP response is fast
    thread = threading.Thread(target=run_poller)
    thread.start()
    
    return jsonify({
        "status": "started",
        "message": "Poller triggered in background",
        "endpoint": "/poll"
    })

@app.route('/scrape')
def trigger_scraper():
    """Run the scraper in the background."""
    def run_scraper():
        try:
            logger.info("📋 Scraper triggered via /scrape endpoint")
            result = subprocess.run(
                [sys.executable, 'scraper.py'],
                capture_output=True,
                text=True,
                timeout=300
            )
            logger.info(f"✅ Scraper finished: {result.returncode}")
        except Exception as e:
            logger.error(f"❌ Scraper failed: {e}")
    
    thread = threading.Thread(target=run_scraper)
    thread.start()
    
    return jsonify({
        "status": "started",
        "message": "Scraper triggered in background",
        "endpoint": "/scrape"
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    logger.info(f"🚀 Starting server on port {port}")
    app.run(host='0.0.0.0', port=port)