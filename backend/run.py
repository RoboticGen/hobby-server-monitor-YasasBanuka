"""
Local development server script for Windows/macOS.
Uses the built-in wsgiref server since gunicorn is Linux-only.
"""
import os
import sys
import subprocess

from dotenv import load_dotenv

# Ensure the backend directory is in the path
sys.path.insert(0, os.path.dirname(__file__))

load_dotenv(".env")

from hsm.app import create_app
from wsgiref.simple_server import make_server

if __name__ == "__main__":
    app = create_app()
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", 8000))
    
    print(f"Starting development server on http://{host}:{port} ...")
    print("Starting background metric collector...")
    print("Press Ctrl+C to stop.")
    
    # Auto-start the collector for local dev convenience
    collector_proc = subprocess.Popen([sys.executable, "-m", "hsm.collector.main"])
    
    with make_server(host, port, app) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down.")
            collector_proc.terminate()
            collector_proc.wait()
