#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest

from bili_digest.ingest import render_card as rc

def _available():
    try:
        rc._pick("reg")
        return True
    except Exception:
        return False

@unittest.skipUnless(_available(), "本机没有中文字体，渲染断言不成立")
class CardShape(unittest.TestCase):

    def test_renders_with_expected_width(self):
        img = rc.render_card("标题", [("甲", "10 万"), ("乙", "3 万")])
        self.assertEqual(img.width, rc.WIDTH)
        self.assertGreater(img.height, 100)

    def test_headline_takes_more_vertical_space_than_body(self):

        plain = rc.render_card("标题", [("甲", "10")])
        with_head = rc.render_card("标题", [("甲", "10")], headline="1296", subline="个视频")
        self.assertGreater(with_head.height, plain.height)

    def test_empty_rows_still_renders(self):

        img = rc.render_card("标题", [], note="空库")
        self.assertEqual(img.width, rc.WIDTH)

    def test_digits_and_cjk_units_both_appear(self):

        img = rc.render_card("标题", [("字幕", "20.4 万")])
        colors = img.getcolors(maxcolors=100000) or []
        self.assertGreater(len(colors), 3, "整张图只有两三种颜色，八成是没画出内容")

    def test_text_card_wraps_and_grows(self):
        short = rc.render_text_card("标题", "一句话")
        long = rc.render_text_card("标题", "这句话会长很多。" * 20)
        self.assertEqual(short.width, rc.WIDTH)
        self.assertGreater(long.height, short.height, "长文本必须让卡片变高")

    def test_text_card_caps_height(self):

        img = rc.render_text_card("标题", "很长的一段话。" * 400)
        self.assertLess(img.height, 3000, "超长文本没有被截断")

    def test_text_card_handles_empty_and_cjk_footer(self):

        img = rc.render_text_card("标题", "", footer="长回复转图 · bili-digest")
        self.assertEqual(img.width, rc.WIDTH)

    def test_list_card_keeps_one_row_per_item(self):
        items = [{"n": i, "tag": "观众评论", "text": "正文" * 20, "bv": "BVTestExample"} for i in range(1, 4)]
        img = rc.render_list_card("搜到 3 条", items)
        self.assertEqual(img.width, rc.WIDTH)
        plain = rc.render_list_card("搜到 1 条", items[:1])
        self.assertGreater(img.height, plain.height, "行数增加时卡片必须变高")

    def test_list_card_survives_missing_bv(self):
        img = rc.render_list_card("搜到 1 条", [{"n": 1, "tag": "AI 洞察", "text": "正文", "bv": ""}])
        self.assertEqual(img.width, rc.WIDTH)

    def test_long_text_is_clipped_not_overflowed(self):
        img = rc.render_card("标" * 80, [("标签" * 40, "1")])
        self.assertEqual(img.width, rc.WIDTH)
        self.assertLess(img.height, 900, "长文本把卡片撑成了长图")

if __name__ == "__main__":
    unittest.main(verbosity=2)
