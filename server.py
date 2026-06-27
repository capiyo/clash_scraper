"""
Web server to keep the Render service alive and trigger poller via /poll endpoint.
"""
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
    """Run the poller in the background and stream logs."""
    def run_poller():
        try:
            logger.info("🚀 Poller triggered via /poll endpoint")
            
            # Run poller and stream output in real-time
            process = subprocess.Popen(
                [sys.executable, 'poller.py'],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1  # Line buffered
            )
            
            # Read and log each line as it comes
            for line in process.stdout:
                logger.info(f"📤 {line.strip()}")
            
            # Wait for process to complete
            process.wait()
            logger.info(f"✅ Poller finished with code: {process.returncode}")
            
        except Exception as e:
            logger.error(f"❌ Poller failed: {e}")
    
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
            process = subprocess.Popen(
                [sys.executable, 'scraper.py'],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            
            for line in process.stdout:
                logger.info(f"📤 {line.strip()}")
            
            process.wait()
            logger.info(f"✅ Scraper finished with code: {process.returncode}")
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