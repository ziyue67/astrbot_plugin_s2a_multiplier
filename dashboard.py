"""Render a Sub2API channel-status dashboard as a PNG image.

The dashboard is a self-contained dark panel designed to read well inside a
QQ chat bubble: a header, a row of KPI cards, then one row per group with a
health pill, success rate, latency, cache rate and a recent-bucket strip.

Pillow is imported lazily so that the text-only commands keep working on
deployments that never install it.
"""

from __future__ import annotations

import datetime as _dt
import glob
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from io import BytesIO

try:  # Pillow is an optional runtime dependency.
    from PIL import Image, ImageDraw, ImageFont

    PILLOW_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without Pillow.
    Image = ImageDraw = ImageFont = None  # type: ignore[assignment]
    PILLOW_AVAILABLE = False

from multiplier_core import (
    Sub2APIGroupHealth,
    build_health_overview,
    format_cache_rate,
    format_health_label,
)

WIDTH = 1080
PAD = 32
ROW_HEIGHT = 78
MAX_BUCKETS = 30

BG = "#0F172A"
PANEL = "#1E293B"
PANEL_ALT = "#172033"
BORDER = "#334155"
TEXT = "#E2E8F0"
TEXT_MUTED = "#94A3B8"
ACCENT = "#38BDF8"

STATE_COLORS = {
    "健康": "#34D399",
    "警告": "#FBBF24",
    "异常": "#F87171",
    "无数据": "#64748B",
}

# Fonts that actually carry CJK glyphs. Rendering Chinese with a Latin-only
# font produces tofu boxes, so we refuse to draw unless one of these is found.
CJK_FONT_PATTERNS = (
    "msyh",
    "msjh",
    "simhei",
    "simsun",
    "dengxian",
    "pingfang",
    "hiragino",
    "notosanscjk",
    "notoserifcjk",
    "sourcehansans",
    "sourcehanserif",
    "wqy",
    "droidsansfallback",
    "arialuni",
    "uming",
    "ukai",
    "misans",
    "harmonyos",
)

FONT_CANDIDATES = (
    # Windows
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/msjh.ttc",
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    # Linux
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
)

FONT_GLOBS = (
    "C:/Windows/Fonts/*.tt[cf]",
    "/usr/share/fonts/**/*CJK*.tt[cf]",
    "/usr/share/fonts/**/*CJK*.otf",
    "/usr/share/fonts/**/*wqy*.tt[cf]",
    "/usr/share/fonts/**/*uming*.tt[cf]",
)


class DashboardUnavailable(RuntimeError):
    """Raised when the image cannot be rendered in this environment."""


@dataclass(frozen=True)
class _Fonts:
    title: object
    section: object
    body: object
    small: object
    value: object


def _is_cjk_font(path: str) -> bool:
    name = os.path.basename(path).lower().replace(" ", "").replace("-", "")
    return any(pattern in name for pattern in CJK_FONT_PATTERNS)


def find_cjk_font_path() -> str | None:
    """Locate a CJK-capable TrueType font, or return None."""

    for candidate in FONT_CANDIDATES:
        if os.path.exists(candidate) and _is_cjk_font(candidate):
            return candidate

    for pattern in FONT_GLOBS:
        for found in sorted(glob.glob(pattern, recursive=True)):
            if _is_cjk_font(found):
                return found
    return None


def render_dashboard(
    instance_name: str,
    health_items: Iterable[Sub2APIGroupHealth],
    *,
    monitor_range: str = "24h",
    generated_at: _dt.datetime | None = None,
) -> bytes:
    """Render the dashboard and return PNG bytes.

    Raises:
        DashboardUnavailable: Pillow is missing, or no CJK font was found.
    """

    if not PILLOW_AVAILABLE:
        raise DashboardUnavailable("未安装 Pillow，无法生成渠道状态图")

    font_path = find_cjk_font_path()
    if font_path is None:
        raise DashboardUnavailable("未找到中文字体，无法生成渠道状态图")

    items: Sequence[Sub2APIGroupHealth] = tuple(health_items)
    overview = build_health_overview(items)
    stamp = generated_at or _dt.datetime.now().astimezone()

    rows = max(1, len(items))
    kpi_block = 108
    header_block = 96
    height = PAD + header_block + kpi_block + 44 + rows * ROW_HEIGHT + PAD

    image = Image.new("RGB", (WIDTH, height), BG)
    draw = ImageDraw.Draw(image)
    fonts = _load_fonts(font_path)

    cursor = PAD
    cursor = _draw_header(draw, fonts, instance_name, monitor_range, stamp, cursor)
    cursor = _draw_kpi_cards(draw, fonts, overview, cursor)
    _draw_group_rows(draw, fonts, items, cursor)

    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _load_fonts(path: str) -> _Fonts:
    def load(size: int):
        try:
            return ImageFont.truetype(path, size)
        except OSError:  # pragma: no cover - unreadable font file.
            return ImageFont.load_default()

    return _Fonts(
        title=load(30),
        section=load(19),
        body=load(20),
        small=load(16),
        value=load(26),
    )


def _draw_header(
    draw,
    fonts: _Fonts,
    instance_name: str,
    monitor_range: str,
    stamp: _dt.datetime,
    top: int,
) -> int:
    draw.text((PAD, top), "Sub2API 渠道状态", font=fonts.title, fill=TEXT)

    badge = f"{instance_name} · {monitor_range}"
    badge_width = draw.textlength(badge, font=fonts.small)
    draw.text(
        (WIDTH - PAD - badge_width, top + 12),
        badge,
        font=fonts.small,
        fill=TEXT_MUTED,
    )

    caption = f"生成时间 {stamp.strftime('%Y-%m-%d %H:%M:%S')}"
    caption_width = draw.textlength(caption, font=fonts.small)
    draw.text(
        (WIDTH - PAD - caption_width, top + 36),
        caption,
        font=fonts.small,
        fill=TEXT_MUTED,
    )

    line_y = top + 62
    draw.line([(PAD, line_y), (WIDTH - PAD, line_y)], fill=BORDER, width=1)
    return line_y + 22


def _draw_kpi_cards(draw, fonts: _Fonts, overview, top: int) -> int:
    cards = [
        ("分组", str(overview.total), ACCENT),
        ("健康", str(overview.healthy), STATE_COLORS["健康"]),
        ("警告", str(overview.warning), STATE_COLORS["警告"]),
        ("异常", str(overview.critical), STATE_COLORS["异常"]),
        (
            "平均缓存率",
            "无数据"
            if overview.average_cache_rate is None
            else f"{overview.average_cache_rate * 100:.1f}%",
            ACCENT,
        ),
    ]

    gap = 14
    card_width = (WIDTH - PAD * 2 - gap * (len(cards) - 1)) // len(cards)
    height = 92

    for index, (label, value, color) in enumerate(cards):
        left = PAD + index * (card_width + gap)
        draw.rounded_rectangle(
            [left, top, left + card_width, top + height],
            radius=14,
            fill=PANEL,
            outline=BORDER,
            width=1,
        )
        draw.text((left + 18, top + 14), label, font=fonts.small, fill=TEXT_MUTED)
        draw.text((left + 18, top + 40), value, font=fonts.value, fill=color)

    return top + height + 34


def _draw_group_rows(
    draw, fonts: _Fonts, items: Sequence[Sub2APIGroupHealth], top: int
) -> None:
    if not items:
        draw.rounded_rectangle(
            [PAD, top, WIDTH - PAD, top + 90],
            radius=14,
            fill=PANEL,
            outline=BORDER,
            width=1,
        )
        draw.text(
            (PAD + 24, top + 32),
            "没有可用的渠道监控数据（站点可能未启用 V2 被动监控）",
            font=fonts.body,
            fill=TEXT_MUTED,
        )
        return

    for index, item in enumerate(items):
        _draw_group_row(draw, fonts, item, top + index * ROW_HEIGHT, index)


def _draw_group_row(
    draw, fonts: _Fonts, item: Sub2APIGroupHealth, top: int, index: int
) -> None:
    height = ROW_HEIGHT - 12
    fill = PANEL if index % 2 == 0 else PANEL_ALT
    draw.rounded_rectangle(
        [PAD, top, WIDTH - PAD, top + height],
        radius=12,
        fill=fill,
        outline=BORDER,
        width=1,
    )

    label = format_health_label(item.overall)
    color = STATE_COLORS.get(label, STATE_COLORS["无数据"])

    # Health pill.
    pill_x, pill_w = PAD + 18, 76
    pill_h = 28
    pill_y = top + (height - pill_h) // 2
    draw.rounded_rectangle(
        [pill_x, pill_y, pill_x + pill_w, pill_y + pill_h],
        radius=pill_h // 2,
        fill=color,
    )
    label_width = draw.textlength(label, font=fonts.small)
    draw.text(
        (pill_x + (pill_w - label_width) / 2, pill_y + 5),
        label,
        font=fonts.small,
        fill=BG,
    )

    # Group name and platform.
    name = item.group_name or item.group_id or "未命名分组"
    draw.text((pill_x + pill_w + 18, top + 12), name, font=fonts.body, fill=TEXT)
    if item.platform:
        draw.text(
            (pill_x + pill_w + 18, top + 38),
            item.platform,
            font=fonts.small,
            fill=TEXT_MUTED,
        )

    # Metrics columns, laid out from the right edge so the strip fits snugly.
    strip_x = WIDTH - PAD - 18 - MAX_BUCKETS * 11
    _draw_metric(draw, fonts, "可用率", _format_success_rate(item), strip_x - 300, top)
    _draw_metric(draw, fonts, "平均延迟", _format_latency(item), strip_x - 190, top)
    _draw_metric(draw, fonts, "缓存率", format_cache_rate(item), strip_x - 88, top)
    _draw_strip(draw, item, strip_x, top + 20, height - 40)


def _draw_metric(
    draw, fonts: _Fonts, label: str, value: str, left: float, top: int
) -> None:
    draw.text((left, top + 12), label, font=fonts.small, fill=TEXT_MUTED)
    draw.text((left, top + 36), value, font=fonts.body, fill=TEXT)


def _draw_strip(
    draw, item: Sub2APIGroupHealth, left: float, top: int, height: int
) -> None:
    """Draw the recent-bucket strip; empty buckets stay dim."""

    buckets = item.buckets[-MAX_BUCKETS:]
    slot = 11
    bar_width = 7
    slots = max(len(buckets), MAX_BUCKETS)

    for index in range(slots):
        if index >= len(buckets):
            x = left + index * slot
            draw.rounded_rectangle(
                [x, top, x + bar_width, top + height],
                radius=2,
                fill="#243044",
            )
            continue

        bucket = buckets[index]
        if bucket.is_empty:
            color = "#243044"
        else:
            color = STATE_COLORS.get(format_health_label(bucket.overall), ACCENT)
        x = left + index * slot
        draw.rounded_rectangle(
            [x, top, x + bar_width, top + height],
            radius=2,
            fill=color,
        )


def _format_success_rate(item: Sub2APIGroupHealth) -> str:
    rate = item.success_rate
    return "无数据" if rate is None else f"{rate * 100:.1f}%"


def _format_latency(item: Sub2APIGroupHealth) -> str:
    if item.ttft_avg_ms is None:
        return "无数据"
    if item.ttft_avg_ms >= 1000:
        return f"{item.ttft_avg_ms / 1000:.2f}s"
    return f"{item.ttft_avg_ms:.0f}ms"
