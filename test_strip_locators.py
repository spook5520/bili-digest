#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest

from bili_digest.distill.extract import strip_locators

class StripsLocators(unittest.TestCase):

    def test_timestamp(self):
        self.assertEqual(strip_locators('新增了 210 条记录（弹幕 05:12）'), '新增了 210 条记录')

    def test_block_id(self):
        self.assertEqual(strip_locators('结论成立（块 220188 的原文）'), '结论成立')

    def test_ordinal(self):
        self.assertEqual(strip_locators('讲了 Docker（第 3 条）'), '讲了 Docker')

    def test_see_also(self):
        self.assertEqual(strip_locators('见第 2 段（见第 2 段）'), '见第 2 段')

class LeavesContentAlone(unittest.TestCase):

    def test_year(self):
        self.assertEqual(strip_locators('视频发布于 2024 年（2024）'), '视频发布于 2024 年（2024）')

    def test_bare_numbers(self):
        self.assertEqual(strip_locators('全库 1296 条，知识科普 210 条'), '全库 1296 条，知识科普 210 条')

    def test_none_text(self):
        self.assertEqual(strip_locators(None), '')

if __name__ == "__main__":
    unittest.main(verbosity=2)
