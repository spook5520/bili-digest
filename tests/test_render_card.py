from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PIL import Image

from bili_digest.ingest import render_card as rc

class TestRenderTextCard(unittest.TestCase):

    def test_size_matches_content(self):
        short = rc.render_text_card("T", "一行")
        long_ = rc.render_text_card("T", "\n".join("第 %d 行" % i for i in range(20)))
        self.assertEqual(short.width, rc.WIDTH)
        self.assertEqual(long_.width, rc.WIDTH)
        self.assertGreater(long_.height, short.height)

    def test_max_lines_truncation(self):
        body = "\n".join("第 %d 行内容" % i for i in range(200))
        img = rc.render_text_card("T", body)

        self.assertLess(img.height, 60 * 36 + 260,
                        "200 行没有触发截断，图会无限长")

    def test_empty_body_does_not_crash(self):
        img = rc.render_text_card("T", "")
        self.assertGreater(img.height, 0)

    def test_footer_uses_cjk_font_not_boxes(self):
        img = rc.render_text_card("标题", "正文", footer="中文页脚测试")

        band = img.crop((rc.PAD, img.height - 48, rc.WIDTH - rc.PAD, img.height - 8))
        colors = {px for _n, px in band.getcolors(maxcolors=100000) or []}
        self.assertGreater(len(colors), 3,
                           "页脚区域颜色过少，疑似中文渲成方框")

    def test_body_is_not_blank(self):
        img = rc.render_text_card("标题", "这是一段必须被画出来的中文正文")
        band = img.crop((rc.PAD, 80, rc.WIDTH - rc.PAD, img.height - 60))
        colors = {px for _n, px in band.getcolors(maxcolors=100000) or []}
        self.assertGreater(len(colors), 3, "正文区域没有前景像素")

class TestCharWidthInvariant(unittest.TestCase):

    def test_char_dropped_shows_as_width_delta(self):
        f = rc._f("reg", 24)
        probe = Image.new("RGB", (rc.WIDTH, 10))
        d = rc.ImageDraw.Draw(probe)
        w_full = rc._w(d, "fastembed", f)
        w_drop = rc._w(d, "fastembd", f)
        self.assertGreater(w_full - w_drop, 8,
                           "少一个 e 的宽度差不该这么小，判据失效")

    def test_full_string_keeps_its_width(self):
        f = rc._f("reg", 24)
        probe = Image.new("RGB", (rc.WIDTH, 10))
        d = rc.ImageDraw.Draw(probe)
        line = "fastembed 按批内最长 pad"
        parts = ["fastembed", " ", "按批内最长", " ", "pad"]
        self.assertEqual(rc._w(d, line, f),
                         sum(rc._w(d, t, f) for t in parts),
                         "整行宽与分段和不符，拼接过程吞了字符")

class TestSaveCard(unittest.TestCase):

    def test_saves_png_and_reads_back(self):
        d = tempfile.mkdtemp(prefix="bd-card-")
        path = os.path.join(d, "c.png")
        img = rc.render_text_card("T", "正文")
        out = rc.save_card(img, path)
        self.assertTrue(os.path.isfile(out))
        with Image.open(out) as reopened:
            self.assertEqual(reopened.format, "PNG")
            self.assertEqual(reopened.size, img.size)

    def test_does_not_write_into_production_paths(self):
        from bili_digest.paths import DB
        d = tempfile.mkdtemp(prefix="bd-card-")
        out = rc.save_card(rc.render_text_card("T", "x"), os.path.join(d, "g.png"))
        self.assertNotEqual(os.path.dirname(os.path.abspath(out)),
                            os.path.dirname(os.path.abspath(str(DB))))

class TestReplyLimitContract(unittest.TestCase):

    def test_reply_limit_is_known_and_small(self):
        from bili_digest.ingest import bili_inbox as bi
        limit = getattr(bi, "REPLY_LIMIT", None)
        self.assertIsNotNone(limit, "REPLY_LIMIT 不见了 —— 截断逻辑失去依据")
        self.assertLess(limit, 1000, "私信上限异常大，图片方案的前提变了")

if __name__ == "__main__":
    unittest.main(verbosity=2)
