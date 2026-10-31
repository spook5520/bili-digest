#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

from bili_digest import paths

import json
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import numpy as np
import onnxruntime as ort

class OnnxEmbedder:

    QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："

    def __init__(self, model_dir: str | Path, int8: bool = True,
                 max_length: int = 512, threads: int | None = None,
                 with_instruction: bool = False):
        self.with_instruction = with_instruction
        d = Path(model_dir)
        onnx_file = d / ("model_int8.onnx" if int8 else "model.onnx")
        if not onnx_file.exists():
            raise SystemExit(f"找不到 ONNX 模型：{onnx_file}")

        so = ort.SessionOptions()
        if threads:
            so.intra_op_num_threads = threads
            so.inter_op_num_threads = 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        self.sess = ort.InferenceSession(str(onnx_file), so,
                                         providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.sess.get_inputs()}
        self.outputs = [o.name for o in self.sess.get_outputs()]

        from tokenizers import Tokenizer
        self.tok = Tokenizer.from_file(str(d / "tokenizer.json"))
        self.tok.enable_truncation(max_length=max_length)

        self.tok.enable_padding(pad_id=0, pad_token="[PAD]")

        cfg = json.loads((d / "config.json").read_text(encoding="utf-8"))
        self.dim = cfg.get("hidden_size", 768)

    def _run(self, texts: list[str]) -> np.ndarray:
        enc = self.tok.encode_batch(texts)
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.inputs:
            feed["token_type_ids"] = np.zeros_like(ids)

        out = self.sess.run(None, feed)

        idx = 0
        for i, name in enumerate(self.outputs):
            if "last_hidden" in name.lower():
                idx = i
                break
        hidden = out[idx]

        cls = hidden[:, 0, :]
        norm = np.linalg.norm(cls, axis=1, keepdims=True) + 1e-9
        return (cls / norm).astype(np.float32)

    def embed_documents(self, texts: list[str], batch: int = 32,
                        progress=None) -> np.ndarray:
        return self._batch(texts, batch, progress)

    def embed_queries(self, texts: list[str], batch: int = 32) -> np.ndarray:
        if self.with_instruction:
            texts = [self.QUERY_INSTRUCTION + t for t in texts]
        return self._batch(texts, batch, None)

    def _batch(self, texts, batch, progress, bucket: bool = True):
        import numpy as np
        n = len(texts)
        if not bucket or n <= batch:
            order = list(range(n))
        else:

            order = sorted(range(n), key=lambda i: len(texts[i]))
        out = [None] * n
        for i in range(0, n, batch):
            idx = order[i:i + batch]
            part = [texts[j] for j in idx]
            vecs = self._run(part)
            for k, j in enumerate(idx):
                out[j] = vecs[k]
            if progress and (i // batch) % 10 == 0:
                progress(i, n)
        return np.vstack(out) if out else np.zeros((0, self.dim), dtype=np.float32)

if __name__ == "__main__":
    import time
    d = sys.argv[1] if len(sys.argv) > 1 else "models/bge-base-onnx"
    print(f"  加载 {d}")
    t0 = time.time()
    m = OnnxEmbedder(d)
    print(f"  维度 {m.dim}  加载 {time.time()-t0:.1f}s")
    print(f"  输入 {sorted(m.inputs)}")
    print(f"  输出 {m.outputs}")

    docs = ["造枪最重要的是合法", "显卡维修 5090 显存", "若娜瓦在剧情里的表现"]
    vecs = m.embed_documents(docs)
    print(f"  编码 {len(docs)} 条 → {vecs.shape}")
    q = m.embed_queries(["造枪需要学什么"])
    sims = (vecs @ q.T).ravel()
    for i, s in enumerate(sims):
        print(f"    {s:.4f}  {docs[i]}")

    sample = ["这是一条测试文本，用来测量编码吞吐。"] * 64
    t0 = time.time()
    m.embed_documents(sample, batch=32)
    dt = time.time() - t0
    print(f"  吞吐 {len(sample)/dt:.0f} 条/秒  (200万条 ≈ {2_000_000/(len(sample)/dt)/3600:.1f} 小时)")
