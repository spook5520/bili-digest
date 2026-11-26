#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import math
import random
import re
import hashlib
import unittest

from bili_digest import kb_native as N
from bili_digest.schema import core_schema as CS
from bili_digest.ingest import fetcher as F
from bili_digest.retrieve import rag_core as RC

class TestNativeParity(unittest.TestCase):
    def test_make_uid(self):
        for p, i in [("bilibili", "BV1x"), ("", ""), ("a:b", "c/d"), ("x", "y:z")]:
            self.assertEqual(N.make_uid(p, i), CS.make_uid(p, i), (p, i))

    def test_parse_uid(self):
        for u in ["bilibili:BV1x", "nocolon", "", "a:b:c", ":x", "x:"]:
            self.assertEqual(N.parse_uid(u), CS.parse_uid(u), u)

    def test_bare_id(self):
        for u in ["bilibili:BV1x", "BV1x", "", "a:b:c", ":x", "x:"]:
            self.assertEqual(N.bare_id(u), CS.bare_id(u), u)

    def test_info_len(self):
        samples = ["", "哈", "abc你好！ 123", "！！！？？？", "😀😀", "Radiohead",
                   "中文English123混合", "\u4e00" * 100, "\u9fff", "\u4e8c", "a"]
        rnd = random.Random(42)
        pool = "abcXYZ0123你好世界！？。， \t\n😀 Radiohead—"
        for _ in range(500):
            samples.append("".join(rnd.choice(pool) for _ in range(rnd.randint(0, 30))))
        for s in samples:
            self.assertEqual(N.info_len(s), CS.info_len(s), repr(s))

    def test_block_hash(self):

        cases = []
        offs = [None, 0, 0.0, 1, 1.5, 12.25, "0.0", "1.5", "abc", -3.0, 1e3]
        seqs = [None, 0, 1, "2", 100]
        for off in offs:
            for seq in seqs:
                cases.append(("subtitle", "测试文本😀", off, seq))
                cases.append(("danmaku", "", off, seq))
        for kind, text, off, seq in cases:
            self.assertEqual(
                N.block_hash(kind, text, off, seq),
                CS.block_hash(kind, text, off, seq),
                (kind, text, off, seq),
            )

    def test_block_hash_real_db(self):

        from bili_digest.paths import DB
        import sqlite3
        con = sqlite3.connect(str(DB))
        rows = con.execute(
            "SELECT kind, text, offset_sec, seq FROM content_block "
            "WHERE offset_sec IS NOT NULL LIMIT 2000").fetchall()
        con.close()
        for kind, text, off, seq in rows:
            self.assertEqual(
                N.block_hash(kind, text, off, seq),
                CS.block_hash(kind, text, off, seq),
                (kind, text[:20], off, seq),
            )

    def test_norm_text(self):
        samples = ["", "你好！！！   aaa  ？？？", "前前前排", "aabbcc",
                   r"。！？，、；：,.!?;:~…《》()（）[]【】\"'“”‘’\-—",
                   "这这个视频好好好看", "    ", "😀😀😀", "ab\nab\nab"]
        rnd = random.Random(7)
        pool = "你好啊abc123！？。， \n\t—《》“”"
        for _ in range(500):
            samples.append("".join(rnd.choice(pool) for _ in range(rnd.randint(0, 40))))
        for s in samples:
            self.assertEqual(N.norm_text(s), F.norm_text(s), repr(s))

    def test_short_weight(self):
        sc, sp = RC.SHORT_CHARS, RC.SHORT_PENALTY
        for s in ["", "   ", "a" * (sc - 1), "a" * sc, "a" * (sc + 1), "  短  ", "中文" * 20]:
            self.assertEqual(N.short_weight(s, sc, sp), RC.short_weight(s), repr(s))

    def test_cliff(self):
        rnd = random.Random(11)
        cases = [[], [0.9], [0.9, 0.3], [0.9, 0.85, 0.84], [1.0, 1.0, 1.0],
                 [0.5, 0.25, 0.24, 0.23]]
        for _ in range(300):
            n = rnd.randint(0, 10)
            scores = [round(rnd.uniform(0.01, 1.0), 3) for _ in range(n)]
            cases.append(scores)
        for scores in cases:
            hits = [{"score": s} for s in scores]
            exp = RC.cliff(hits)
            cut, ratio, flat = N.cliff(scores, 0.6, 2)
            got = {"cut": cut, "ratio": round(ratio, 3) if ratio is not None else None,
                   "flat": flat}
            self.assertEqual(got, exp, scores)

if __name__ == "__main__":
    unittest.main(verbosity=2)
