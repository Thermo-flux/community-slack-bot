#!/usr/bin/env python3
"""
Launcher script to run the test suite.
Usage:
    python run_tests.py [pytest_args...]
"""

import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent

if __name__ == "__main__":
    cmd = [sys.executable, "-m", "pytest", "-v", *sys.argv[1:]]
    print(f"Running test suite in {ROOT_DIR}...")
    sys.exit(subprocess.call(cmd, cwd=str(ROOT_DIR)))
