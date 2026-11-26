from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

SELFHEAL = Path(__file__).with_name("selfheal.py")
ROOT = Path(__file__).resolve().parents[2]

def extract_actions(path: Path) -> list:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Name) and fn.id == "add"):
            continue
        if len(node.args) < 3:
            continue
        aid = node.args[0]
        act = node.args[2]
        name = aid.value if isinstance(aid, ast.Constant) else "?"
        if isinstance(act, ast.Constant) and act.value is None:
            out.append((name, None))
        elif isinstance(act, ast.List):
            toks = [e.value for e in act.elts if isinstance(e, ast.Constant)]
            out.append((name, toks))
    return out

def cli_subcommands() -> set:
    main_py = ROOT / "bili_digest" / "cli" / "main.py"
    if not main_py.exists():
        return set()
    tree = ast.parse(main_py.read_text(encoding="utf-8"))
    subs = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_parser" and node.args
                and isinstance(node.args[0], ast.Constant)):
            subs.add(node.args[0].value)
    return subs

def check(act) -> tuple:
    if act is None:
        return True, "只报告，无动作"
    if not act:
        return False, "空动作"
    head = act[0]
    if head == "bili_digest":
        sub = act[1] if len(act) > 1 else ""
        subs = cli_subcommands()
        return (sub in subs), ("CLI 子命令 %r %s" % (sub, "已注册" if sub in subs else "未注册"))
    if head.endswith(".py"):
        p = (ROOT / head)
        return p.exists(), ("脚本 %s" % ("存在" if p.exists() else "不存在"))
    if "." in head:
        try:
            spec = importlib.util.find_spec(head)
        except Exception as e:
            return False, "import 失败：%s" % type(e).__name__
        if spec is None:
            return False, "模块不存在"
        origin = getattr(spec, "origin", None) or ""
        if not origin.endswith(".py"):
            return False, "不是 .py 模块"
        src_txt = Path(origin).read_text(encoding="utf-8", errors="replace")
        has_main = ('__name__ == "__main__"' in src_txt
                    or "__name__ == '__main__'" in src_txt)
        if not has_main:
            return False, "模块存在但没有 __main__ 块，-m 跑它什么都不做"
        return True, "模块 + __main__ 块"
    return False, "无法判定的形式：%r" % head

def main() -> int:
    acts = extract_actions(SELFHEAL)
    bad = 0
    print("动作覆盖审计 —— %d 条" % len(acts))
    print()
    for name, act in acts:
        ok, why = check(act)
        mark = "OK  " if ok else "FAIL"
        shown = " ".join(act) if act else "（只报告）"
        print("  [%s] %-18s %-44s %s" % (mark, name, shown, why))
        if not ok:
            bad += 1
    print()
    print("%d 条动作，%d 条不可执行" % (len(acts), bad))
    return 1 if bad else 0

if __name__ == "__main__":
    sys.exit(main())
