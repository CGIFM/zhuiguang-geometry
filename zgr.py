#!/usr/bin/env python3
"""追光几何 入口：python zgr.py <命令>"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from zgr.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
