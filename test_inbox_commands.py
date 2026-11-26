#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import contextlib
import io
import json
import sys
import unittest
from unittest import mock

from bili_digest.ingest import bili_inbox as inbox_mod
from bili_digest.ingest import inbox_cli

class CommandRouting(unittest.TestCase):

    def test_help_is_an_alias_too(self):
        self.assertEqual(inbox_cli.run_command("/?"), inbox_cli.run_command("/help"))
        self.assertEqual(inbox_cli.run_command("/0"), inbox_cli.run_command("/help"))

    def test_plain_text_is_not_a_command(self):

        for text in ("库里有没有讲 Ollama 的", "这个视频讲了什么", ""):
            self.assertIsNone(inbox_cli.run_command(text))

    def test_leading_space_is_tolerated(self):

        seen = []

        def fake(*argv, **kwargs):
            seen.append(argv)
            return "ok"

        with mock.patch.object(inbox_cli, "_kb", fake):
            inbox_cli.run_command("   /s Docker")
        self.assertEqual(seen, [("search", "Docker")])
        self.assertIsNone(inbox_cli.run_command("   库里有没有讲 Ollama 的"))

    def test_unknown_slash_falls_through(self):

        self.assertIsNone(inbox_cli.run_command("/nope"))

        self.assertEqual(inbox_cli.run_command("/"), inbox_cli.run_command("/help"))

class ArgumentHandling(unittest.TestCase):

    def test_missing_argument_shows_usage(self):
        for code in ("s", "e", "w"):
            text = inbox_cli.run_command("/" + code)
            self.assertIn("用法", text, "/%s 缺参数时应给用法" % code)

    def test_command_calls_the_matching_subcommand(self):
        seen = []

        def fake(*argv, **kwargs):
            seen.append(argv)
            return "ok"

        with mock.patch.object(inbox_cli, "_kb", fake):
            inbox_cli.run_command("/s Docker")
            inbox_cli.run_command("/t")
        self.assertEqual(seen[0], ("search", "Docker"))
        self.assertEqual(seen[1], ("stats",))

    def test_numeric_alias_reaches_the_same_subcommand(self):
        seen = []

        def fake(*argv, **kwargs):
            seen.append(argv)
            return "ok"

        with mock.patch.object(inbox_cli, "_kb", fake):
            inbox_cli.run_command("/1 Docker")
            inbox_cli.run_command("/s Docker")
        self.assertEqual(seen[0], seen[1], "/1 和 /s 应落到同一条 kb 子命令")

    def test_trends_needs_a_mode(self):
        seen = []

        def fake(*argv, **kwargs):
            seen.append(argv)
            return "ok"

        with mock.patch.object(inbox_cli, "_kb", fake):
            inbox_cli.run_command("/k")
            inbox_cli.run_command("/k 8")
            inbox_cli.run_command("/k Ollama")
        self.assertEqual(seen[0], ("trends", "--hot", "8"))
        self.assertEqual(seen[1], ("trends", "--hot", "8"))
        self.assertEqual(seen[2], ("trends", "--first", "Ollama"))

    def test_highlights_without_target_explains_itself(self):
        self.assertIn("用法", inbox_cli.run_command("/h"))

class LookupDetection(unittest.TestCase):

    def test_recognised(self):
        for text in ("库里有没有讲 Docker 的", "库中收录了什么", "帮我查一下库里有没有 Ollama"):
            self.assertTrue(inbox_cli.looks_like_lookup(text), text)

    def test_left_to_the_model(self):
        for text in ("库里条目最多的主题是什么", "这个视频讲了什么", "库里讲弹幕文化的有几条"):
            self.assertFalse(inbox_cli.looks_like_lookup(text), text)

class PullRespectsAllowlist(unittest.TestCase):

    def test_strangers_are_skipped(self):
        msgs = [
            {'msg_id': 'ok-1', 'talker': '3546713230936922', 'text': '你好', 'msg_type': 1, 'ts': 1000},
            {'msg_id': 'sys-1', 'talker': '12076317', 'text': '/t', 'msg_type': 1, 'ts': 2000},
        ]
        seen = []

        def fake_fetch(*a, **k):
            return msgs

        class Args:
            mentions_only = False
            dm_only = True
            all = True
            include_shares = False

        captured = []
        with mock.patch.object(inbox_mod, 'fetch_dms', fake_fetch), \
             mock.patch.object(inbox_mod, 'allowed_talkers', lambda: ['3546713230936922']), \
             mock.patch.object(inbox_cli, '_kb', lambda *a, **k: seen.append(a) or 'x'), \
             mock.patch.object(inbox_cli, '_emit', lambda payload: captured.append(payload)):
            inbox_cli.cmd_pull(Args())
        out = captured[-1]
        self.assertEqual(len(out['items']), 1, '名单外的人不该进 items')
        self.assertEqual(out['items'][0]['sender'], '3546713230936922')
        self.assertEqual(out['skipped'], 1)
        self.assertEqual(seen, [], '名单外的命令不该执行')

class MenuShape(unittest.TestCase):

    def test_help_all_differs_and_discloses_aliases(self):
        short, full = inbox_cli.HELP_TEXT, inbox_cli.run_command("/help all")
        self.assertNotEqual(short, full)
        self.assertIn("别名", full)
        for alias, target in inbox_cli.ALIASES.items():
            self.assertIn("%s=%s" % (alias, target), full,
                          "别名 %s=%s 没写进 /help all，老用户会以为它失效了" % (alias, target))

        self.assertNotIn("1=s", short)

    def test_groups_are_a_subset_without_duplicates(self):

        grouped = [code for _title, codes in inbox_cli.GROUPS for code in codes]
        self.assertEqual(len(grouped), len(set(grouped)), "命令在分组里重复了")
        self.assertTrue(set(grouped) <= set(inbox_cli.COMMANDS), "分组里有不存在的命令")

    def test_menu_lists_core_and_full_lists_everything(self):
        short = inbox_cli.run_command("/help")
        for code in ("s", "i", "a"):
            self.assertIn("/" + code, short, "主菜单里少了核心命令 /%s" % code)
        full = inbox_cli.run_command("/help all")
        for code in inbox_cli.COMMANDS:
            self.assertIn("/" + code, full, "/help all 里少了 /%s —— 那是偷偷砍功能" % code)

    def test_menu_mentions_natural_language(self):

        text = inbox_cli.run_command("/help")
        self.assertIn("直接说人话", text)

    def test_menu_has_no_ascii_table(self):

        text = inbox_cli.run_command("/help")
        for ch in "┌└│─┼":
            self.assertNotIn(ch, text, "菜单里出现了表格字符 %s" % ch)

class DiscoverCommands(unittest.TestCase):

    def test_search_lists_candidates(self):
        seen = []

        def fake(*argv, **kwargs):
            seen.append(argv)
            return "ok"

        with mock.patch.object(inbox_cli, "_kb", fake):
            inbox_cli.run_command("/i 强化学习")
        self.assertEqual(seen, [("discover", "--keyword", "强化学习")])

    def test_adopt_takes_serial_numbers(self):
        seen = []

        def fake(*argv, **kwargs):
            seen.append(argv)
            return "ok"

        with mock.patch.object(inbox_cli, "_kb", fake):
            inbox_cli.run_command("/a 1,3")
        self.assertEqual(seen, [("discover", "--adopt", "1,3")])

    def test_missing_argument_explains_the_pair(self):

        self.assertIn("用法", inbox_cli.run_command("/i"))
        self.assertIn("用法", inbox_cli.run_command("/a"))
        self.assertIn("/a", inbox_cli.run_command("/i"))

class SearchParsing(unittest.TestCase):

    def test_parses_numbered_items(self):
        text = ("  混合检索 命中 2 条（top-k=2）\n"
                "  [1] [观众评论 1 inuink] 前半句 · BVxxxxxxxxxx\n"
                "  [2] [AI 洞察] 另一句 · BVxxxxxxxxxx")
        items = inbox_cli.parse_search(text)
        self.assertEqual([i["n"] for i in items], [1, 2])
        self.assertEqual(items[0]["bv"], "BVxxxxxxxxxx")
        self.assertEqual(items[1]["tag"], "AI 洞察")

    def test_continuation_line_joins_previous(self):
        text = "  [1] [AI 洞察] 第一段\n  第二段接在后面"
        items = inbox_cli.parse_search(text)
        self.assertEqual(len(items), 1)
        self.assertIn("第二段", items[0]["text"])

    def test_emoji_in_tag_is_stripped(self):

        text = "  [1] [观众评论 👍3 某人] 正文 · BVxxxxxxxxxx"
        self.assertNotIn("👍", inbox_cli.parse_search(text)[0]["tag"])

if __name__ == "__main__":
    unittest.main(verbosity=2)
