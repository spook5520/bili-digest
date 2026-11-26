#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bili_digest.quality.selfheal import preflight

class Preflight(unittest.TestCase):
    def test_real_module_passes(self):
        self.assertEqual(preflight(["bili_digest.retrieve.keywords", "--build"]), (True, ""))

    def test_seg_boundary_lives_in_distill_not_ingest(self):

        self.assertTrue(preflight(["bili_digest.distill.seg_boundary", "--build"])[0])
        ok, err = preflight(["bili_digest.ingest.seg_boundary"])
        self.assertFalse(ok, "ingest 下没有 seg_boundary，这里必须判失败")
        self.assertIn("模块不存在", err)

    def test_cli_subcommand_is_not_checked(self):

        self.assertEqual(preflight(["bili_digest", "index"]), (True, ""))

    def test_missing_module_rejected(self):
        self.assertFalse(preflight(["bili_digest.nope.nothere"])[0])

    def test_flags_ignored(self):

        self.assertTrue(preflight(["--build", "bili_digest.retrieve.keywords"])[0])

if __name__ == "__main__":
    unittest.main()
