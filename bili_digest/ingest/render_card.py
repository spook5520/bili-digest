from __future__ import annotations

import re

import os
from typing import Iterable, Sequence

from PIL import Image, ImageDraw, ImageFont

WIDTH = 900
PAD = 56

BG = (13, 16, 20)
SURFACE = (22, 27, 34)
TITLE = (242, 244, 247)
NUM = (255, 255, 255)
LABEL = (150, 160, 175)
MUTED = (110, 120, 135)
ACCENT = (91, 157, 255)
TRACK = (38, 45, 56)

_REG = r"C:\Windows\Fonts\msyh.ttc"
_BOLD = r"C:\Windows\Fonts\msyhbd.ttc"
_MONO = r"C:\Windows\Fonts\consola.ttf"
_fonts: dict[str, str] = {}

def _pick(kind: str) -> str:
    if kind not in _fonts:
        cands = {"reg": (_REG, "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
                 "bold": (_BOLD, "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc"),

                 "mono": (_MONO, "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")}[kind]
        for c in cands:
            if os.path.isfile(c):
                _fonts[kind] = c
                break
        else:
            raise RuntimeError("找不到字体 %s；没有它渲染出来会全是方框" % kind)
    return _fonts[kind]

def _f(kind: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(_pick(kind), size)

def _w(d: ImageDraw.ImageDraw, text: str, font) -> int:
    if not text:
        return 0
    box = d.textbbox((0, 0), text, font=font)
    return box[2] - box[0]

def _fit(d: ImageDraw.ImageDraw, text: str, kind: str, size: int, max_w: int, floor: int = 18):
    while size > floor:
        f = _f(kind, size)
        if _w(d, text, f) <= max_w:
            return f
        size -= 2
    return _f(kind, floor)

def _draw_value(d: ImageDraw.ImageDraw, x_right: int, y: int, value: str, size: int) -> None:
    import re as _re
    m = _re.match(r"^([0-9][0-9.,]*)\s*(.*)$", value.strip())
    if not m:
        d.text((x_right - _w(d, value, _f("reg", size)), y), value, font=_f("reg", size), fill=NUM)
        return
    num, unit = m.group(1), m.group(2)
    nf, uf = _f("mono", size), _f("reg", size)
    nw = _w(d, num, nf)
    gap = 8 if unit else 0
    uw = _w(d, unit, uf) if unit else 0
    x = x_right - (nw + gap + uw)
    d.text((x, y), num, font=nf, fill=NUM)
    if unit:
        d.text((x + nw + gap, y + 1), unit, font=uf, fill=LABEL)

def _clip(d: ImageDraw.ImageDraw, text: str, font, max_w: int) -> str:
    if _w(d, text, font) <= max_w:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _w(d, text[:mid] + "…", font) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + "…"

def render_card(title: str, rows: Sequence[tuple[str, str]], *, headline: str = "",
                subline: str = "", note: str = "", footer: str | None = None,
                stamp: str | None = None) -> Image.Image:
    probe = Image.new("RGB", (WIDTH, 10))
    pd = ImageDraw.Draw(probe)
    inner = WIDTH - PAD * 2

    title_f = _f("reg", 30)
    stamp_f = _f("mono", 22)
    head_f = _f("bold", 96) if headline else None
    sub_f = _f("reg", 26)
    label_f = _f("reg", 26)
    value_f = _f("mono", 26)
    note_f = _f("reg", 24)
    foot_f = _f("mono", 20)

    bar_h = 10
    row_h = 56
    y = PAD
    h = PAD + 44
    if headline:
        h += 118
    if subline:
        h += 44
    h += 28 + max(1, len(rows)) * row_h
    if note:
        h += 16 + 34
    if footer:
        h += 18 + 30
    h += PAD - 16

    img = Image.new("RGB", (WIDTH, h), BG)
    d = ImageDraw.Draw(img)

    d.text((PAD, y), title, font=title_f, fill=TITLE)
    if stamp:
        d.text((WIDTH - PAD - _w(d, stamp, stamp_f), y + 4), stamp, font=stamp_f, fill=MUTED)
    y += 44

    if headline:
        y += 8
        d.text((PAD, y), headline, font=head_f, fill=NUM)
        y += 118
    if subline:
        d.text((PAD, y), _clip(d, subline, sub_f, inner), font=sub_f, fill=LABEL)
        y += 44

    y += 16

    nums: list[float] = []
    for _label, value in rows:
        digits = "".join(ch for ch in value if ch.isdigit() or ch == ".")
        try:
            scale = 10000.0 if "万" in value else 1.0
            nums.append(float(digits) * scale if digits else 0.0)
        except ValueError:
            nums.append(0.0)
    top = max(nums) if nums else 0.0

    bar_x = PAD + 96
    bar_w_max = inner - 96 - 130
    for (label, value), n in zip(rows, nums):
        d.text((PAD, y), _clip(d, label, label_f, 88), font=label_f, fill=LABEL)

        d.rounded_rectangle((bar_x, y + 14, bar_x + bar_w_max, y + 14 + bar_h), radius=bar_h // 2, fill=TRACK)
        if top > 0 and n > 0:
            bw = max(int(bar_w_max * (n / top)), 6)
            d.rounded_rectangle((bar_x, y + 14, bar_x + bw, y + 14 + bar_h), radius=bar_h // 2, fill=ACCENT)
        _draw_value(d, WIDTH - PAD, y + 2, value, 26)
        y += row_h

    if note:
        y += 16
        d.text((PAD, y), _clip(d, note, note_f, inner), font=note_f, fill=MUTED)
        y += 34

    if footer:
        y += 18
        d.text((PAD, y), footer, font=foot_f, fill=MUTED)
    return img

def save_card(img: Image.Image, path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    img.save(path, "PNG", optimize=True)
    return path

def render_list_card(title: str, items: Sequence[dict], *, sub: str = "",
                     footer: str | None = None, stamp: str | None = None) -> Image.Image:
    probe = Image.new("RGB", (WIDTH, 10))
    pd = ImageDraw.Draw(probe)
    inner = WIDTH - PAD * 2

    title_f = _f("reg", 30)
    stamp_f = _f("mono", 22)
    sub_f = _f("reg", 24)
    n_f = _f("mono", 28)
    tag_f = _f("reg", 22)
    body_f = _f("reg", 25)
    bv_f = _f("mono", 20)
    foot_f = _f("mono", 20)

    row_h = 68
    y = PAD
    h = PAD + 44
    if sub:
        h += 38
    h += 20 + max(1, len(items)) * row_h
    if footer:
        h += 20 + 30
    h += PAD - 16

    img = Image.new("RGB", (WIDTH, h), BG)
    d = ImageDraw.Draw(img)

    d.text((PAD, y), title, font=title_f, fill=TITLE)
    if stamp:
        d.text((WIDTH - PAD - _w(d, stamp, stamp_f), y + 4), stamp, font=stamp_f, fill=MUTED)
    y += 44
    if sub:
        d.text((PAD, y), _clip(d, sub, sub_f, inner), font=sub_f, fill=LABEL)
        y += 38
    y += 20

    for it in items:

        n_txt = str(it.get("n", ""))
        d.text((PAD, y + 2), n_txt.rjust(2), font=n_f, fill=ACCENT)

        d.text((PAD + 44, y + 4), _clip(d, str(it.get("tag", "")), tag_f, 190), font=tag_f, fill=LABEL)
        body_x = PAD + 44 + 200
        d.text((body_x, y + 4), _clip(d, str(it.get("text", "")), body_f, WIDTH - PAD - body_x), font=body_f, fill=TITLE)
        y += row_h

    if footer:
        y += 20
        d.text((PAD, y), footer, font=foot_f, fill=MUTED)
    return img

def render_text_card(title: str, body: str, *, footer: str | None = None,
                     stamp: str | None = None) -> Image.Image:
    probe = Image.new("RGB", (WIDTH, 10))
    pd = ImageDraw.Draw(probe)
    inner = WIDTH - PAD * 2

    title_f = _f("reg", 30)
    stamp_f = _f("mono", 22)
    body_f = _f("reg", 24)

    foot_f = _f("reg", 20)

    lines: list[str] = []
    for para in (body or "").split("\n"):
        if not para.strip():
            lines.append("")
            continue
        cur = ""
        for token in re.findall(r"[A-Za-z0-9_\-\./:]+|.", para):
            cand = cur + token
            if _w(pd, cand, body_f) <= inner:
                cur = cand
            else:
                if cur:
                    lines.append(cur)
                cur = token.strip() if token.strip() else token
        if cur:
            lines.append(cur)

    line_h = 36
    head_h = PAD + 44 + (38 if stamp else 0) + 12
    foot_h = (26 + 30) if footer else 0
    h = head_h + max(1, len(lines)) * line_h + foot_h + PAD - 16

    MAX_LINES = 60
    clipped = len(lines) > MAX_LINES
    if clipped:
        lines = lines[:MAX_LINES]
        h = head_h + MAX_LINES * line_h + foot_h + PAD - 16

    img = Image.new("RGB", (WIDTH, h), BG)
    d = ImageDraw.Draw(img)

    y = PAD
    d.text((PAD, y), _clip(d, title, title_f, inner), font=title_f, fill=TITLE)
    if stamp:
        d.text((WIDTH - PAD - _w(d, stamp, stamp_f), y + 4), stamp, font=stamp_f, fill=MUTED)
    y += 44 + (38 if stamp else 0) + 12

    for ln in lines:
        if ln:
            d.text((PAD, y), ln, font=body_f, fill=TITLE)
        y += line_h

    if clipped:
        d.text((PAD, y), "……（已截断，完整内容见视频/原文）", font=body_f, fill=MUTED)
    if footer:
        y += 26
        d.text((PAD, y), footer, font=foot_f, fill=MUTED)
    return img
