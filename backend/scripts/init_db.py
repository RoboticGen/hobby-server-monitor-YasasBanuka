"""
Database initialization script.

Run this once on a fresh machine to create the SQLite schema.
Safe to run multiple times — all CREATE statements use IF NOT EXISTS.

Usage:
  python scripts/init_db.py

This is the "automated way to init an empty database" required by the task.
No manual SQL needed.
"""
import sys
import os

# Allow running from the backend/ directory
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

from hsm.db import init_db
from hsm.config import Config

if __name__ == "__main__":
    print(f"Initializing database at: {Config.DB_PATH}")
    init_db()
    print("Done. Database is ready.")
