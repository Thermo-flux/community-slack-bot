#!/usr/bin/env python3
"""
Launcher script to run the Community Slack Bot from project root.
Usage:
    python run_bot.py
"""

import sys
from pathlib import Path

# Add src to Python path
SRC_DIR = Path(__file__).resolve().parent / "src"
sys.path.insert(0, str(SRC_DIR))

from bot import main

if __name__ == "__main__":
    main()
