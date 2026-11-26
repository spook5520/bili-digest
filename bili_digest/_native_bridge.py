#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os as _os

NATIVE = None
try:
    if _os.environ.get("KB_NATIVE", "1") != "0":
        from bili_digest import kb_native as _MOD
        NATIVE = _MOD
except (ImportError, OSError):

    NATIVE = None

def enabled() -> bool:
    return NATIVE is not None
