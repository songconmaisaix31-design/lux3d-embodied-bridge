#!/usr/bin/env python3
"""Launcher so `python bridge.py <command>` works from a clean checkout.

No installation step, no third-party packages: src/ is simply put on sys.path.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from lux3d_bridge.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
