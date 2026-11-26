from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from bili_digest.paths import DB as REAL_DB
from bili_digest.schema import core_schema as cs
from bili_digest.schema.time import now_sec

def _assert_not_production(path: str) -> None:
    p = os.path.abspath(path)
    real = os.path.abspath(str(REAL_DB))
    if p == real:
        raise AssertionError("测试试图使用生产库：%s" % p)
    if not p.startswith(os.path.abspath(tempfile.gettempdir())):
        raise AssertionError("测试库不在临时目录内：%s" % p)
    if "knowledge.db" in os.path.basename(p):
        raise AssertionError("测试库名疑似生产库：%s" % p)

def make_temp_db() -> sqlite3.Connection:
    d = tempfile.mkdtemp(prefix="bd-test-")
    path = os.path.join(d, "t.db")
    _assert_not_production(path)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(cs.DDL)
    con.commit()
    try:
        cs.ensure_fts(con)
    except SystemExit:

        raise unittest.SkipTest("缺少 jieba 切词引擎，无法建 block_fts")
    return con

def log_fail(con, scope: str, detail: str, ts: int | None = None) -> None:
    con.execute(
        "INSERT INTO job_log(scope, step, status, detail, ts, actor) "
        "VALUES(?,?,?,?,?,?)",
        (scope, "fetch", "fail", detail,
         ts if ts is not None else now_sec() - 100, "test"))
    con.commit()

class TestFailKinds(unittest.TestCase):

    def setUp(self):
        self.con = make_temp_db()

    def tearDown(self):
        self.con.close()

    def test_each_kind_lands_in_its_bucket(self):
        from bili_digest.quality.health import fail_kinds
        cases = [
            ("bilibili:A", "取视频信息失败：62012", "gone"),
            ("bilibili:B", "未能从参数中识别 BV 号", "gone"),
            ("sh-C", "...python.exe: No module nam", "env"),
            ("bilibili:D", "TimeoutError: The read operation timed out", "transient"),
            ("bilibili:E", "HTTP Error 412: Precondition Failed", "transient"),
            ("bilibili:F", "KB_BILI_HIVE 未配置，请设置登录态", "auth"),
            ("bilibili:G", "完全看不懂的失败", "other"),
        ]
        for scope, detail, _want in cases:
            log_fail(self.con, scope, detail)
        got = fail_kinds(self.con)
        self.assertEqual(got["gone"], 2, got)
        self.assertEqual(got["env"], 1, got)
        self.assertEqual(got["transient"], 2, got)
        self.assertEqual(got["auth"], 1, got)
        self.assertEqual(got["other"], 1, got)
        self.assertEqual(got["total"], 7, got)

    def test_auth_wins_over_transient_when_both_match(self):
        from bili_digest.quality.health import fail_kinds
        log_fail(self.con, "x", "HTTP 412 Precondition Failed：登录态可能已过期")
        got = fail_kinds(self.con)
        self.assertEqual(got["auth"], 1,
                         "落到 transient 说明 auth 不再优先：%r" % (got,))
        self.assertEqual(got["transient"], 0, got)

    def test_repaired_scope_leaves_the_count(self):
        from bili_digest.quality.health import fail_kinds
        log_fail(self.con, "bilibili:Z", "取视频信息失败：62012")
        self.con.execute(
            "INSERT INTO job_log(scope, step, status, detail, ts, actor) "
            "VALUES(?,?,?,?,?,?)",
            ("bilibili:Z", "fetch", "skip", "已标失效", time.time(), "test"))
        self.con.commit()
        self.assertEqual(fail_kinds(self.con)["total"], 0)

    def test_is_derived_from_constants_not_literals(self):
        from bili_digest.quality.health import FAIL_DO, FAIL_KINDS
        kinds = {k for k, _ in FAIL_KINDS} | {"other"}
        self.assertEqual(kinds, set(FAIL_DO), "FAIL_KINDS 与 FAIL_DO 键不一致")

class TestMarkGone(unittest.TestCase):

    def setUp(self):
        self.con = make_temp_db()

    def tearDown(self):
        self.con.close()

    def test_only_gone_is_marked(self):
        from bili_digest.quality.mark_gone import mark
        log_fail(self.con, "bilibili:GONE", "取视频信息失败：62012")
        log_fail(self.con, "sh-ENV", "...python.exe: No module nam")
        log_fail(self.con, "bilibili:AUTH", "KB_BILI_HIVE 未配置")
        res = mark(self.con, apply=True)
        self.assertEqual(res["gone"], 1, res)
        self.assertEqual(res["scopes"], ["bilibili:GONE"], res)

        for scope in ("sh-ENV", "bilibili:AUTH"):
            n = self.con.execute(
                "SELECT COUNT(*) FROM job_log WHERE scope=? AND status='skip'",
                (scope,)).fetchone()[0]
            self.assertEqual(n, 0, "%s 被误标 skip" % scope)

    def test_dry_run_writes_nothing(self):
        from bili_digest.quality.mark_gone import mark
        log_fail(self.con, "bilibili:GONE2", "取视频信息失败：62012")
        before = self.con.execute("SELECT COUNT(*) FROM job_log").fetchone()[0]
        res = mark(self.con, apply=False)
        after = self.con.execute("SELECT COUNT(*) FROM job_log").fetchone()[0]
        self.assertEqual(res["gone"], 1)
        self.assertEqual(before, after, "干跑不该写库")

    def test_marked_scope_no_longer_counted(self):
        from bili_digest.quality.mark_gone import mark
        from bili_digest.quality.health import fail_kinds
        log_fail(self.con, "bilibili:CLOSE", "取视频信息失败：62012")
        self.assertEqual(fail_kinds(self.con)["total"], 1)
        mark(self.con, apply=True)
        self.assertEqual(fail_kinds(self.con)["total"], 0)

class TestAddBlockIdempotent(unittest.TestCase):

    def setUp(self):
        self.con = make_temp_db()

    def tearDown(self):
        self.con.close()

    def test_same_content_twice_yields_one_row(self):
        from bili_digest.schema.core_schema import add_block
        uid = "bilibili:TEST"
        b1 = add_block(self.con, uid, "subtitle_seg", "测试文本", offset_sec=1.0)
        b2 = add_block(self.con, uid, "subtitle_seg", "测试文本", offset_sec=1.0)
        self.assertIsNotNone(b1)
        self.assertIsNone(b2, "第二次应返回 None（已存在）")
        n = self.con.execute(
            "SELECT COUNT(*) FROM content_block WHERE item_uid=? AND kind='subtitle_seg'",
            (uid,)).fetchone()[0]
        self.assertEqual(n, 1)

    def test_fts_is_written_for_seg(self):
        from bili_digest.schema.core_schema import add_block
        bid = add_block(self.con, "bilibili:T2", "subtitle_seg", "语义分段测试内容")
        n = self.con.execute("SELECT COUNT(*) FROM block_fts WHERE rowid=?", (bid,)).fetchone()[0]
        self.assertEqual(n, 1, "seg 没进 FTS —— 只能走向量路")

    def test_subtitle_is_excluded_from_fts(self):
        from bili_digest.schema.core_schema import add_block
        bid = add_block(self.con, "bilibili:T3", "subtitle", "字幕原文测试")
        n = self.con.execute("SELECT COUNT(*) FROM block_fts WHERE rowid=?", (bid,)).fetchone()[0]
        self.assertEqual(n, 0, "subtitle 进了 FTS —— 与 NO_FTS_KINDS 矛盾")

class TestStatusIn(unittest.TestCase):

    def test_valid(self):
        self.assertEqual(cs.status_in(cs.STATUS_REPAIRED), "('ok','skip')")

    def test_rejects_quote_injection(self):
        with self.assertRaises(ValueError):
            cs.status_in(["ok'; DROP TABLE job_log--"])

if __name__ == "__main__":
    unittest.main(verbosity=2)
