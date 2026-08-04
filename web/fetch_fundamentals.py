# -*- coding: utf-8 -*-
"""兼容入口；正式实现位于仓库 scripts/fetch_fundamentals.py。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.fetch_fundamentals import main


if __name__ == "__main__":
    main()
