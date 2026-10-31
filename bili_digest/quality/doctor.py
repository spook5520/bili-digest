#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

from bili_digest import paths

import argparse
import json
import os
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent

OK, BAD, SKIP = "✓", "✗", "-"
results = []

def note(name: str, status: str, detail: str = "", fix: str = ""):
    results.append((name, status, detail, fix))
    mark = {"ok": OK, "bad": BAD, "skip": SKIP}[status]
    print(f"  [{mark}] {name:<12} {detail}")
    if fix and status in ("bad", "skip"):
        for line in fix.splitlines():
            print(f"        → {line.strip()}")

def main() -> int:
    ap = argparse.ArgumentParser(description="配置自检")
    ap.add_argument("--no-llm", action="store_true", help="跳过 LLM 连通性检查")
    a = ap.parse_args()

    print("═══ 配置自检 ═══\n")

    print("  ── 依赖 ──")
    for mod, hint in (("numpy", "pip install numpy"),
                      ("jieba", "pip install jieba"),
                      ("fastembed", "pip install fastembed"),
                      ("usearch", "pip install usearch")):
        try:
            m = __import__(mod)
            v = getattr(m, "__version__", "?")
            note(mod, "ok", f"{v}")
        except ImportError:
            note(mod, "bad", "未安装", f"{hint}（或 pip install -r requirements.txt）")

    print("\n  ── 数据 ──")
    from bili_digest import paths
    d = paths.DATA_DIR
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".doctor-write-probe"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        note("数据目录", "ok", str(d))
    except OSError as e:
        note("数据目录", "bad", f"{d} 不可写（{type(e).__name__}）",
             "设 KB_DATA_DIR 指向一个可写目录")

    db_ok = paths.DB.exists()
    note("知识库", "ok" if db_ok else "skip",
         f"{paths.DB.name} {'已建' if db_ok else '尚未创建'}",
         "" if db_ok else "python kb.py init")

    print("\n  ── 嵌入 ──")
    try:
        from fastembed import TextEmbedding
        cache = str(paths.MODELS_DIR)
        downloaded = paths.MODELS_DIR.is_dir() and any(
            paths.MODELS_DIR.iterdir()) if paths.MODELS_DIR.is_dir() else False
        if not downloaded:
            note("嵌入模型", "skip", "尚未下载（首次用到时会自动下载约 90MB）",
                 f"想先下好：python -c \"from fastembed import TextEmbedding;"
                 f" TextEmbedding('{paths.EMBED_MODEL}', cache_dir='{cache}')\"\n"
                 "下载慢就设 KB_HF_ENDPOINT 换镜像")
        else:
            import os as _os
            _os.environ.setdefault("HF_ENDPOINT", paths.HF_ENDPOINT)
            m = TextEmbedding(model_name=paths.EMBED_MODEL, cache_dir=cache)
            vec = list(m.embed(["自检"]))[0]
            dim = len(vec)
            if dim == paths.EMBED_DIM:
                note("嵌入模型", "ok", f"{paths.EMBED_MODEL} · {dim} 维")
            else:
                note("嵌入模型", "bad",
                     f"维度不符：模型 {dim} · 配置 {paths.EMBED_DIM}",
                     f"改 KB_EMBED_DIM={dim} 并重建索引（python build_kb_index.py --rebuild）")
    except Exception as e:
        note("嵌入模型", "bad", f"{type(e).__name__}: {str(e)[:60]}",
             "检查网络 / KB_HF_ENDPOINT；或先跳过（检索需要它）")

    if db_ok and paths.VEC.exists():
        try:
            from bili_digest.retrieve import rag_core as rc
            keys = rc.load_index_keys(paths.VEC)
            note("向量索引", "ok",
                 f"{paths.VEC.name} · {len(keys) if keys else '?'} 条"
                 f" · {paths.VEC.stat().st_size/1024/1024:.1f}MB")
        except Exception as e:
            note("向量索引", "bad", f"{type(e).__name__}: {str(e)[:60]}",
                 "python build_kb_index.py --rebuild")
    else:
        note("向量索引", "skip", "尚未建立",
             "抓取并提炼后跑 python build_kb_index.py")

    print("\n  ── 凭据 ──")
    try:
        from bili_digest.ingest import fetcher as bd
        cred = bd.load_credential()
        s = cred.get("sessdata") or ""
        has_uid = bool(cred.get("dedeuserid"))
        note("B站登录态", "ok",
             f"SESSDATA {'已取到' if s else '缺失'}（{len(s)} 字符）"
             f" · DedeUserID {'有' if has_uid else '缺'}"
             f"{'（收藏夹接口需要）' if not has_uid else ''}")
        if s and not has_uid:
            note("  DedeUserID", "bad", "缺（收藏夹同步必需）",
                 "客户端 hive 里通常会带；只给 --sessdata 时用不了收藏夹同步，"
                 "但单视频抓取可以")
    except SystemExit as e:
        note("B站登录态", "skip", str(e).splitlines()[0],
             "\n".join(str(e).splitlines()[1:]))
    except Exception as e:
        note("B站登录态", "bad", f"{type(e).__name__}: {str(e)[:60]}", "")

    print("\n  ── LLM ──")
    if a.no_llm:
        note("LLM", "skip", "已跳过（--no-llm）")
    else:
        try:
            from bili_digest.distill import extract
            key, info = extract._load_api_key()
            base, model = info.split("|", 1)
            import urllib.error
            import urllib.request
            body = json.dumps({
                "model": model,
                "messages": [{"role": "user", "content": "回复 OK"}],
                "max_tokens": 8, "stream": False,
            }).encode("utf-8")
            req = urllib.request.Request(f"{base}/chat/completions", data=body)
            req.add_header("Content-Type", "application/json")
            req.add_header("Authorization", f"Bearer {key}")
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read().decode("utf-8"))
            txt = (d.get("choices") or [{}])[0].get("message", {}).get("content", "")
            note("LLM 连通", "ok", f"{model} @ {base} → {str(txt)[:20]!r}")
        except SystemExit as e:
            note("LLM", "skip", str(e).splitlines()[0],
                 "\n".join(str(e).splitlines()[1:3]))
        except urllib.error.HTTPError as e:
            hint = "key 无效或余额不足" if e.code in (401, 402, 403) else "端点返回错误"
            note("LLM 连通", "bad", f"HTTP {e.code} {e.reason} ← {hint}",
                 "检查 KB_LLM_KEY / KB_LLM_BASE / KB_LLM_MODEL")
        except Exception as e:
            note("LLM 连通", "bad", f"{type(e).__name__}: {str(e)[:60]}",
                 f"检查 KB_LLM_BASE 能否访问（当前 {locals().get('base', '?')}）")

    bad = [r for r in results if r[1] == "bad"]
    skip = [r for r in results if r[1] == "skip"]
    print(f"\n═══ {len(results)-len(bad)-len(skip)} 项通过 · "
          f"{len(skip)} 项待办 · {len(bad)} 项失败 ═══")
    if bad:
        print("\n  失败项怎么修（按上面 → 的提示）；全部项见 .env.example")
    if skip:
        print("  待办项通常是「还没做到那一步」，不一定是配置错误")
    return 1 if bad else 0

if __name__ == "__main__":
    sys.exit(main())
