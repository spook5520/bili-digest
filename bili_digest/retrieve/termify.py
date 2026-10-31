# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

from bili_digest import paths

PROMPT = """把下面这句话改写成【技术术语化的检索查询】。

要求：
- 保留原意，但用【技术表达】替换生活化说法
  例：「东西存太多会爆掉」→「内存不足 / 缓存容量上限」
  例：「不常用的存着没用」→「冷数据缓存效益低」
  例：「让 AI 挪位置还被攻击」→「AI 位置更新后仍被攻击的时序问题」
- 输出 3 个不同角度的改写
- 若原句已含术语，保留它
- 输出为 JSON：{{"variants": ["改写1", "改写2", "改写3"]}}
  （注意 call_llm 强制 json_object 模式，prompt 里必须出现 json 字样，
   否则服务端直接 400 —— 这一点实测踩过两次。）

原句：{q}
"""

def termify(query: str, n: int = 3, verbose: bool = False) -> list:
    q = (query or "").strip()
    if not q:
        return []
    try:
        from bili_digest.distill import extract
        import json as _json
        raw = extract.call_llm(PROMPT.format(q=q))
        outs = []
        try:
            j = _json.loads(raw or "{}")
            outs = [str(v).strip() for v in (j.get("variants") or [])]
        except Exception:

            outs = [l.strip(" -0123456789.	") for l in (raw or "").splitlines()]
        outs = [v for v in outs if len(v) >= 4 and v != q][:n]
        if verbose:
            print("    改写：%s" % " | ".join(outs))
        return [q] + outs
    except Exception as e:
        if verbose:
            print("    （改写不可用：%s）" % type(e).__name__)
        return [q]

def main() -> int:
    import argparse
    from bili_digest import paths as _p
    _p.enable_utf8_stdout()
    ap = argparse.ArgumentParser(description="口语查询 → 术语化")
    ap.add_argument("query")
    a = ap.parse_args()
    for v in termify(a.query, verbose=True):
        print("  ·", v)

if __name__ == "__main__":
    main()
