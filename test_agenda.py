#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bili_digest.quality import agenda

def _db(rows):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE query_log (question TEXT)")
    con.execute("CREATE TABLE open_question (question TEXT)")
    con.executemany("INSERT INTO query_log(question) VALUES(?)", [(q,) for q in rows])
    return con

class SuggestFromLog(unittest.TestCase):
    def test_case_and_space_variants_merge(self):
        con = _db(["Frida hook"] * 16 + ["frida hook"] * 23)
        out = agenda.suggest_from_log(con, min_times=3)
        con.close()
        self.assertEqual(len(out), 1, "Frida hook / frida hook 应合并成一条")
        self.assertEqual(out[0]["n"], 39)

    def test_high_df_words_do_not_drop_a_real_question(self):
        con = _db(["安卓逆向 hook"] * 35)
        out = agenda.suggest_from_log(con, min_times=3)
        con.close()
        self.assertEqual(len(out), 1, "高频词组成的问题不能整条消失")
        self.assertEqual(out[0]["q"], "安卓逆向 hook")

    def test_pure_stopwords_still_filtered(self):
        con = _db(["怎么"] * 5)
        out = agenda.suggest_from_log(con, min_times=3)
        con.close()
        self.assertEqual(out, [])

    def test_distinct_questions_stay_separate(self):
        con = _db(["Frida"] * 10 + ["Frida hook 怎么用"] * 10)
        out = agenda.suggest_from_log(con, min_times=3)
        con.close()
        self.assertEqual(len(out), 2)

if __name__ == "__main__":
    unittest.main()
