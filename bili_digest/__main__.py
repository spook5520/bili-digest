#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bili_digest.cli.main import main

if __name__ == "__main__":
    main()
