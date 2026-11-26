#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CRATE = HERE / "bili_digest" / "_native"
PKG = HERE / "bili_digest"

def main() -> int:
    if not shutil.which("cargo"):
        print("未找到 cargo：请先安装 Rust 工具链（https://rustup.rs）")
        return 1

    env = dict(os.environ)
    env["PYO3_PYTHON"] = sys.executable
    print(f"编译目标 Python: {sys.executable}")
    print(f"crate 目录: {CRATE}")

    r = subprocess.run(
        ["cargo", "build", "--release"],
        cwd=str(CRATE), env=env,
    )
    if r.returncode != 0:
        print("cargo build 失败")
        return r.returncode

    dll = CRATE / "target" / "release" / "kb_native.dll"
    if not dll.exists():
        dll = CRATE / "target" / "release" / "libkb_native.so"
    if not dll.exists():
        print("未找到编译产物")
        return 1

    suffix = ".pyd" if sys.platform == "win32" else ".so"
    dest = PKG / f"kb_native{suffix}"
    shutil.copy2(dll, dest)
    print(f"已生成: {dest}")
    print("验证：py -3.12 -c 'from bili_digest import kb_native; print(kb_native.__version__)'")
    return 0

if __name__ == "__main__":
    sys.exit(main())
