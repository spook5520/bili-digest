from __future__ import annotations

import ast
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def check_gitattributes() -> list:
    p = os.path.join(ROOT, ".gitattributes")
    if not os.path.exists(p):
        return [(".gitattributes", 0, "缺失：换行符规则又只能靠人记了")]
    txt = io.open(p, encoding="utf-8").read()
    out = []
    if "* text=auto eol=lf" not in txt and "eol=lf" not in txt:
        out.append((".gitattributes", 0, "没有 eol=lf：存量 CRLF 不会被规范化"))
    if not re.search(r"^\*\.py\s+text\s+eol=lf", txt, re.M):
        out.append((".gitattributes", 0, "缺少 *.py text eol=lf（PEP 8 要求 LF）"))
    return out

def check_no_literal_status_sets() -> list:
    bad = []
    targets = {
        "('ok','skip')": "STATUS_REPAIRED",
        '("ok","skip")': "STATUS_REPAIRED",
        "('ok','fail','skip')": "STATUS_ATTEMPTED",
        "('ok', 'skip')": "STATUS_REPAIRED",
    }
    for dirpath, _dirs, files in os.walk(os.path.join(ROOT, "bili_digest")):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            fp = os.path.join(dirpath, fn)
            rel = os.path.relpath(fp, ROOT).replace(os.sep, "/")

            src_txt = io.open(fp, encoding="utf-8", errors="replace").read()
            skip_lines = set()
            try:
                tree_ = ast.parse(src_txt)
            except SyntaxError:
                continue
            for node in ast.walk(tree_):
                if not isinstance(node, (ast.Module, ast.FunctionDef,
                                         ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                if not node.body:
                    continue
                first = node.body[0]
                if (isinstance(first, ast.Expr)
                        and isinstance(first.value, ast.Constant)
                        and isinstance(first.value.value, str)):
                    end = getattr(first, "end_lineno", first.lineno) or first.lineno
                    skip_lines.update(range(first.lineno, end + 1))
            for i, line in enumerate(src_txt.splitlines(), 1):
                if i in skip_lines:
                    continue
                if line.strip().startswith("#"):
                    continue
                for lit, const in targets.items():
                    if lit in line and ("IN" in line or "=" in line) and "STATUS_" not in line:
                        bad.append((rel, i, "写死状态集 %s，应改用 %s" % (lit, const)))
    return bad

def check_action_modules_executable() -> list:
    sys.path.insert(0, ROOT)
    try:
        from bili_digest.quality import selfheal as _sh_audit
    except Exception:
        pass
    src_path = os.path.join(ROOT, "bili_digest", "quality", "selfheal.py")
    tree = ast.parse(io.open(src_path, encoding="utf-8").read())
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "add":
            if len(node.args) >= 3:
                a = node.args[2]
                if isinstance(a, ast.List) and a.elts:
                    first = a.elts[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        tok = first.value
                        if "." in tok and not tok.endswith(".py"):
                            mods.add(tok)
    bad = []
    for mod in sorted(mods):
        path = os.path.join(ROOT, *mod.split(".")) + ".py"
        if not os.path.exists(path):
            bad.append((os.path.relpath(path, ROOT), 0, "模块不存在：%s" % mod))
            continue
        txt = io.open(path, encoding="utf-8").read()
        if '__name__ == "__main__"' not in txt and "__name__ == '__main__'" not in txt:
            bad.append((os.path.relpath(path, ROOT), 0,
                        "模块没有 __main__ 块，-m 跑它什么也不做：%s" % mod))
    return bad

def check_no_unclosed_text_write() -> list:
    bad = []
    for dirpath, _dirs, files in os.walk(os.path.join(ROOT, "bili_digest")):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            fp = os.path.join(dirpath, fn)
            rel = os.path.relpath(fp, ROOT).replace(os.sep, "/")
            try:
                tree = ast.parse(io.open(fp, encoding="utf-8", errors="replace").read())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                name = getattr(f, "id", None) or getattr(f, "attr", None)
                if name not in ("open",):
                    continue
                if len(node.args) < 2:
                    continue
                mode = node.args[1]
                if not (isinstance(mode, ast.Constant) and isinstance(mode.value, str)):
                    continue
                if "w" not in mode.value or "b" in mode.value:
                    continue
                if any(k.arg == "newline" for k in node.keywords):
                    continue
                bad.append((rel, node.lineno, "文本写未指定 newline，Windows 会写 CRLF"))
    return bad

def check_fts_kinds_single_source() -> list:
    bad = []
    types_p = os.path.join(ROOT, "bili_digest", "schema", "types.py")
    rag_p = os.path.join(ROOT, "bili_digest", "retrieve", "rag_core.py")
    if not (os.path.exists(types_p) and os.path.exists(rag_p)):
        return bad
    rag = io.open(rag_p, encoding="utf-8").read()
    if "_DEFAULT_EXCLUDED" in rag and "NO_FTS_KINDS" not in rag:
        bad.append(("bili_digest/retrieve/rag_core.py", 0,
                    "_DEFAULT_EXCLUDED 没有从 NO_FTS_KINDS 派生（两套口径）"))
    return bad

CHECKS = (
    ("gitattributes", check_gitattributes),
    ("状态集字面量", check_no_literal_status_sets),
    ("动作模块可执行", check_action_modules_executable),
    ("文本写 newline", check_no_unclosed_text_write),
    ("FTS kind 同源", check_fts_kinds_single_source),
)

def run(verbose: bool = True) -> int:
    total = 0
    for name, fn in CHECKS:
        try:
            bad = fn()
        except Exception as e:
            bad = [("<检查自身>", 0, "%s: %s" % (type(e).__name__, e))]
        total += len(bad)
        if verbose:
            mark = "OK  " if not bad else "FAIL"
            print("[%s] %s   %d 处" % (mark, name, len(bad)))
            for f, ln, why in bad:
                loc = "%s:%d" % (f, ln) if ln else f
                print("        %s  %s" % (loc, why))
    if verbose:
        print()
        print("不变量违规合计 %d 处" % total)
    return 0 if total == 0 else 1

def main() -> int:
    quiet = "--quiet" in sys.argv
    return run(verbose=not quiet)

if __name__ == "__main__":
    sys.exit(main())
