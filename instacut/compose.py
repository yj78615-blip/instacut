"""[4] 말풍선 합성 — raw 그림 + 한국어 텍스트 → out 최종 컷

생성 직후 자동으로 돌지 않는다. 사용자가 raw/ 를 보고 나서 부른다 (PRD F-4).
raw/ 는 절대 덮어쓰지 않는다. 텍스트를 몇 번 고쳐도 그림은 그대로 남아야 한다.
"""

from __future__ import annotations

import math
import os
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .head import overlaps, subject_box

# 회피 강도는 좌표의 출처에 따라 다르다. 추측(fallback)까지 엄격히 지키면
# 말풍선이 전부 아래로 몰려 시선 흐름(P-6)이 깨진다.
TOLERANCE = {
    "gemini": 0.06,    # 몸 전체를 정확히 안다 — 엄격히 피한다
    "haar": 0.12,      # 얼굴만 안다 — 몸은 걸쳐도 된다
    "fallback": 0.40,  # 추측일 뿐이다 — 느슨하게
}

FONT_PATH = r"C:\Windows\Fonts\malgun.ttf"
FONT_BOLD = r"C:\Windows\Fonts\malgunbd.ttf"

# 인스타 4:5 캐러셀 규격
OUT_W, OUT_H = 1080, 1350

# 말풍선 후보 자리 (x, y, 폭) — 이미지 크기 대비 비율.
#
# 위 두 자리 / 아래 두 자리. **첫 말풍선은 반드시 위, 다음은 반드시 아래**여서
# 그 사이를 그림이 채운다 (말풍선 → 그림 → 말풍선).
#
# 자리를 계단식(좌 0.03 / 우 0.26 / 좌 0.50 / 우 0.73)으로 두어도 봤는데,
# 왼쪽 위가 인물에 막히면 다음 자리가 y 0.26 이라 말풍선이 얼굴보다 아래에서 시작했다.
# 위/아래 두 단이어야 "말풍선이 그림보다 먼저"가 지켜진다.
ZONES = {
    "left-upper": (0.045, 0.05, 0.42),
    "right-upper": (0.535, 0.05, 0.42),
    "left-lower": (0.045, 0.70, 0.42),
    "right-lower": (0.535, 0.70, 0.42),
}

# 나레이션이 있는 컷만 위쪽 자리를 내려 나레이션 박스를 피한다.
# 전 컷을 내려놓으면 첫 말풍선이 불필요하게 아래로 처져서 두 말풍선 사이가 좁아지고,
# 그 사이에 인물이 들어갈 자리가 없어진다.
NARR_SHIFT = 0.15

UPPER = ["left-upper", "right-upper"]
LOWER = ["left-lower", "right-lower"]

# **시선은 좌우 반전 S 자로 흐른다 (PRD P-6): 말풍선 → 그림 → 말풍선.**
#
# 순서에서 말풍선이 그림보다 먼저다. 첫 말풍선이 그림 위에 놓이고, 다음이 그림 아래에 놓여야
# 독자의 눈이 대사 → 그림 → 대사 순으로 지나간다.
#
# 자리가 다 막혔을 때의 폴백 순서. 정상 경로는 UPPER → LOWER 로 간다.
# 그림의 빈 곳을 찾아 그때그때 놓는 방식도 써봤지만, 컷마다 말풍선이 다른 데 붙어서
# 캐러셀이 산만해졌다. 자리가 예측 가능한 편이 낫다. 이 순서를 흔드는 것은 P-7(머리 회피)뿐이다.
ZONE_PRIORITY = ["left-upper", "right-upper", "left-lower", "right-lower"]

# 그림이 캔버스 전체를 채운다. 흰 띠는 없다.
#
# 예전에는 나레이션을 그림 밖 띠로 뺐다 — 인물이 프레임을 꽉 채워서 안에 놓을 자리가 없었기 때문이다.
# 이제 ControlNet 이 자리를 비워주므로 (render.planned_zones → pose.place_subject)
# 나레이션도 그림 위에 얹을 수 있고, 흰 여백을 만들 이유가 없어졌다.
ART_W, ART_H = OUT_W, OUT_H

# 나레이션이 차지하는 상단 가로 영역 (x0, y0, x1, y1) — 인물이 피해야 할 자리
NARRATION_ZONE = (0.03, 0.025, 0.97, 0.17)

ZONE_H = 0.24  # 자리 탐색용 높이 근사치

FONT_MAX, FONT_MIN = 46, 26
PAD = 26  # 말풍선 안쪽 여백
LINE_GAP = 1.35

# 대사에 문장이 여럿이면 말풍선을 문장 단위로 쪼개 세로로 이어붙인다.
# **문장 중간에서는 절대 끊지 않는다** — "본 것 / 같았다." 처럼 갈리면 읽기가 끊긴다.
# 문장 하나가 아무리 길어도 말풍선 하나로 둔다.
SENTENCE = re.compile(r"[^.!?…]+[.!?…]*")
# 겹쳐 그려야 이어진 것처럼 보인다 (뒤 조각의 흰 배경이 앞 조각 테두리를 덮는다).
# 타원은 사각 박스에 내접해 위아래 끝이 좁아지므로 훨씬 많이 겹쳐야 시각적으로 붙는다.
OVERLAP_RECT = 10
OVERLAP_ELLIPSE = 0.22  # 조각 높이 대비 비율

# 생각 꼬리는 인물까지 이어진다.
#
# 짧게 두면 인물과 말풍선 사이의 배경(나무)에서 끊겨 **그 배경이 생각하는 것처럼** 보인다.
# 실제로 그렇게 만들었다가 두 번 지적받았다.
# 큰 점 세 개로 길게 이었을 때는 배경 위에 흩어져 어색했으므로,
# 말풍선 바로 아래에 점 세 개. 인물까지 이어 붙이지 않는다 —
# 방향만 가리키면 읽히고, 화면을 가로지르면 배경 위에 점이 흩어져 어수선해진다.
TAIL_DOTS = (0.04, 0.16, 0.27)  # 말풍선 → 인물 사이의 상대 위치. 앞쪽 1/4 에서 끝난다
TAIL_SIZES = (19, 16, 13)  # 인물 쪽으로 갈수록 작아진다
TAIL_REACH = 1.0  # 얼굴까지
TAIL_INSET = 14.0  # 꼬리 뿌리를 말풍선 안쪽으로 넣는 깊이 (테두리를 덮어 이어 보이게)


def fit_to_canvas(img: Image.Image, w: int = OUT_W, h: int = OUT_H) -> Image.Image:
    """생성 해상도를 인스타 규격으로 맞춘다. 비율이 다르면 중앙 기준으로 자른다."""
    src_ratio = img.width / img.height
    dst_ratio = w / h

    if src_ratio > dst_ratio:  # 원본이 더 넓다 → 좌우를 자른다
        new_w = int(img.height * dst_ratio)
        left = (img.width - new_w) // 2
        img = img.crop((left, 0, left + new_w, img.height))
    elif src_ratio < dst_ratio:  # 원본이 더 높다 → 위아래를 자른다
        new_h = int(img.width / dst_ratio)
        top = (img.height - new_h) // 2
        img = img.crop((0, top, img.width, top + new_h))

    return img.resize((w, h), Image.LANCZOS)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: float) -> list[str]:
    """어절 단위 줄바꿈. 어절 하나가 폭을 넘으면 글자 단위로 쪼갠다."""
    lines: list[str] = []
    line = ""

    for word in text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= max_w:
            line = trial
            continue

        if line:
            lines.append(line)
            line = ""

        if draw.textlength(word, font=font) <= max_w:
            line = word
            continue

        # 어절 하나가 통째로 넘친다 → 글자 단위 폴백
        for ch in word:
            if draw.textlength(line + ch, font=font) <= max_w or not line:
                line += ch
            else:
                lines.append(line)
                line = ch

    if line:
        lines.append(line)
    return lines or [""]


def _fit_text(draw, text: str, max_w: float, max_h: float, bold: bool = False):
    """박스에 맞을 때까지 폰트를 줄인다. 최소 크기에서도 넘치면 그대로 반환."""
    path = FONT_BOLD if bold and Path(FONT_BOLD).exists() else FONT_PATH

    for size in range(FONT_MAX, FONT_MIN - 1, -2):
        font = ImageFont.truetype(path, size)
        lines = _wrap(draw, text, font, max_w)
        if len(lines) * size * LINE_GAP <= max_h:
            return font, lines, size

    font = ImageFont.truetype(path, FONT_MIN)
    return font, _wrap(draw, text, font, max_w), FONT_MIN


def _draw_lines(draw, lines, font, size, x, y, fill="black", center_w=None):
    """center_w 를 주면 그 폭 안에서 각 줄을 가운데 정렬한다 (말풍선 관행)."""
    for i, line in enumerate(lines):
        lx = x if center_w is None else x + (center_w - draw.textlength(line, font=font)) / 2
        draw.text((lx, y + i * size * LINE_GAP), line, font=font, fill=fill)


def split_sentences(text: str) -> list[str]:
    """대사를 문장 단위로 쪼갠다. 말줄임표(...)는 한 덩어리로 본다."""
    parts = [p.strip() for p in SENTENCE.findall(text) if p.strip()]
    return parts or [text]


def _zone_box(zone: str, w: int, art_top: int, art_h: int, shift: float = 0.0) -> tuple[float, float, float, float]:
    """말풍선 자리를 픽셀 좌표로. shift 는 나레이션이 있을 때 위쪽 자리를 내리는 양."""
    zx, zy, zw = ZONES[zone]
    if zone in UPPER:
        zy += shift
    return (zx * w, art_top + zy * art_h, (zx + zw) * w, art_top + (zy + ZONE_H) * art_h)


def _to_art_ratio(box, w: int, art_top: int, art_h: int):
    """캔버스 픽셀 좌표를 그림 영역 대비 비율로. head_box 와 같은 좌표계로 맞춘다."""
    return (box[0] / w, (box[1] - art_top) / art_h, box[2] / w, (box[3] - art_top) / art_h)


def _pick_zone(
    used: set[str],
    w: int,
    art_top: int,
    art_h: int,
    head=None,
    prev=None,
    shift: float = 0.0,
    tolerance: float = 0.12,
) -> str | None:
    """시선 흐름대로 자리를 고른다 (P-6).

    **그림도 흐름의 한 지점이다.** 두 번째 이후 말풍선(prev 가 있을 때)은
    인물 머리보다 아래 자리를, 그리고 직전과 좌우 반대편을 우선한다.
    그래야 말풍선 → 그림 → 말풍선 순으로 시선이 지나간다.

    **머리와 겹치는 자리는 건너뛴다 (P-7 > P-6).**
    쓸 자리가 하나도 없으면 None — 호출자가 그림 밖으로 밀어낸다. 얼굴을 덮느니 밖으로 나간다.

    `head` 는 상자 하나이거나 여러 개다. 대화 컷에서 주인공만 피했더니
    말풍선이 점원 얼굴을 덮었다 — 화면에 있는 사람은 전부 피해야 한다.
    """
    if head is None:
        heads = []
    elif isinstance(head[0], (int, float)):
        heads = [head]  # 상자 하나
    else:
        heads = [b for b in head if b]
    if prev is None:
        # 첫 말풍선은 그림보다 먼저 읽혀야 한다 — 무조건 위쪽 자리
        order = UPPER + LOWER
    else:
        # 그림을 지난 뒤이므로 아래쪽 자리. 직전과 좌우 반대편이어야 곡선이 된다
        opposite = [z for z in LOWER if z.split("-")[0] != prev.split("-")[0]]
        order = opposite + [z for z in LOWER if z not in opposite] + UPPER

    for name in order:
        if name in used:
            continue
        box = _to_art_ratio(_zone_box(name, w, art_top, art_h, shift), w, art_top, art_h)
        # 자리의 일부가 스치는 정도는 허용한다. 8% 로 뒀더니 머리 모서리가
        # 8.6% 걸쳤다고 위쪽 자리를 통째로 버리고 말풍선이 그림 아래로 밀려났다.
        # 말풍선은 자리 안에서 실제 텍스트 크기만큼만 그려지므로 이 정도는 안 닿는다.
        # 검출에 실패해 가정값을 쓸 때는 호출자가 tolerance 를 크게 준다 (아래 참조)
        if any(overlaps(box, h, tolerance=tolerance) for h in heads):
            continue
        return name
    return None






def _narration(draw, texts: list[str], w: int, h: int) -> None:
    """나레이션은 그림 위 상단 가로 박스. ControlNet 이 이 자리를 비워둔다."""
    x0, y0, x1, y1 = (NARRATION_ZONE[0] * w, NARRATION_ZONE[1] * h, NARRATION_ZONE[2] * w, NARRATION_ZONE[3] * h)
    font, lines, size = _fit_text(draw, "  ".join(texts), (x1 - x0) - PAD * 2, (y1 - y0) - PAD)
    text_h = len(lines) * size * LINE_GAP
    box_h = text_h + PAD * 1.2

    draw.rounded_rectangle([x0, y0, x1, y0 + box_h], radius=12, fill=(255, 255, 255, 240), outline="black", width=3)
    _draw_lines(draw, lines, font, size, x0 + PAD, y0 + PAD * 0.55)


# 꼬리 뿌리가 타원에서 차지하는 각도(라디안).
# 넓히면 뭉툭한 뿔이 되어 만화 말풍선처럼 보이지 않는다 — 좁고 뾰족해야 한다.
TAIL_ROOT_ANGLE = 0.08


def _balloon_outline(box, tip=None, root_half: float = TAIL_ROOT_ANGLE) -> list[tuple[float, float]]:
    """말풍선 외곽선을 점 목록으로. 꼬리가 있으면 그 구간을 꼬리로 대체한다.

    타원과 꼬리를 따로 그리면 꼬리의 검은 변이 타원 안쪽까지 들어가 삐져나온다.
    **하나의 닫힌 경로**로 만들어야 외곽선이 자연스럽게 이어진다 — 만화에서 그리는 방식이다.
    """
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    rx, ry = (x1 - x0) / 2, (y1 - y0) / 2

    if tip is None:
        steps = 96
        return [
            (cx + rx * math.cos(2 * math.pi * i / steps), cy + ry * math.sin(2 * math.pi * i / steps))
            for i in range(steps)
        ]

    # 꼬리가 나갈 방향의 각도를 비우고 그 자리에 tip 을 꽂는다
    ang = math.atan2((tip[1] - cy) / max(ry, 1e-6), (tip[0] - cx) / max(rx, 1e-6))
    steps = 88
    span = 2 * math.pi - 2 * root_half
    pts = [
        (cx + rx * math.cos(a), cy + ry * math.sin(a))
        for a in (ang + root_half + span * i / steps for i in range(steps + 1))
    ]
    pts.append(tip)
    return pts


def _draw_smooth(img, pts, line_width: int = 3, scale: int = 4) -> None:
    """닫힌 도형을 4배로 그려 줄인다.

    Pillow 에는 안티앨리어싱이 없어 곡선과 대각선이 계단처럼 거칠다.
    """
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    pad = line_width * 2 + 6
    ox, oy = int(min(xs)) - pad, int(min(ys)) - pad
    w, h = int(max(xs)) - ox + pad, int(max(ys)) - oy + pad
    if w <= 0 or h <= 0:
        return

    big = Image.new("RGBA", (w * scale, h * scale), (255, 255, 255, 0))
    d = ImageDraw.Draw(big)
    local = [((px - ox) * scale, (py - oy) * scale) for px, py in pts]
    d.polygon(local, fill=(255, 255, 255, 255))
    # joint="curve" 만으로는 꼬리 끝처럼 각이 예리한 곳에서 선이 벌어져 보인다.
    # 꼭짓점마다 선 두께만 한 원을 찍어 이음매를 메운다.
    lw = line_width * scale
    d.line(local + [local[0]], fill=(0, 0, 0, 255), width=lw, joint="curve")
    r = lw / 2
    for px, py in local:
        d.ellipse([px - r, py - r, px + r, py + r], fill=(0, 0, 0, 255))
    img.alpha_composite(big.resize((w, h), Image.LANCZOS), (ox, oy))


def _edge_point(box, target, ellipse: bool) -> tuple[float, float]:
    """말풍선 중심에서 target 방향으로 나아가다 테두리와 만나는 점.

    꼬리를 여기서 뻗어야 인물을 정확히 겨눈다. 예전에는 좌/우 × 위/옆 네 방향만
    있어서 대각선에 있는 인물에게는 방향만 대충 맞았다.
    """
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    rx, ry = max(1.0, (x1 - x0) / 2), max(1.0, (y1 - y0) / 2)
    dx, dy = target[0] - cx, target[1] - cy
    dist = max(1e-6, (dx * dx + dy * dy) ** 0.5)
    ux, uy = dx / dist, dy / dist

    if ellipse:
        # 타원 방정식으로 교점을 구한다
        denom = max(1e-6, ((ux / rx) ** 2 + (uy / ry) ** 2) ** 0.5)
        t = 1.0 / denom
    else:
        # 사각형은 먼저 닿는 변까지의 거리
        t = min(rx / abs(ux) if abs(ux) > 1e-6 else 1e9, ry / abs(uy) if abs(uy) > 1e-6 else 1e9)
    return (cx + ux * t, cy + uy * t)


def _balloon(
    draw,
    text: str,
    zone: str,
    w: int,
    art_top: int,
    art_h: int,
    thought: bool,
    shift: float = 0.0,
    head: tuple | None = None,
):
    """말풍선을 그린다. 대사가 길면 여러 개로 쪼개 세로로 이어붙인다.

    꼬리는 말하는 인물을 향한다. 말풍선이 화면 중간보다 아래에 있으면
    인물이 그보다 위에 있다는 뜻이므로 꼬리를 위로 붙인다.
    """
    zx, zy, zw = ZONES[zone]
    if zone in UPPER:
        zy += shift
    box_w = w * zw
    # 말풍선은 기본적으로 둥근 형태다. 대사와 생각은 모양이 아니라 **꼬리**로 구분한다
    # (대사는 뾰족한 삼각형, 생각은 점 세 개) — 만화 관행이기도 하다.
    # 타원은 안쪽 가용 폭이 사각형보다 좁으므로 텍스트를 먼저 좁게 잡아야 밖으로 새지 않는다
    text_limit = (box_w - PAD * 2) * 0.68
    font, _, size = _fit_text(draw, text, text_limit, art_h * 0.44, bold=True)

    # 폰트는 전체 기준으로 한 번 정하고, 줄바꿈은 문장별로 따로 한다.
    # 문장 하나가 여러 줄이어도 그 문장은 한 말풍선에 통째로 들어간다.
    groups = [_wrap(draw, s, font, text_limit) for s in split_sentences(text)]

    # 조각마다 크기를 따로 잰다 — 줄 수가 다르면 크기도 달라야 자연스럽다
    boxes = []
    for g in groups:
        tw = max(draw.textlength(ln, font=font) for ln in g)
        th = len(g) * size * LINE_GAP
        # 텍스트 박스를 감싸는 타원은 사각형보다 커야 한다
        bw, bh = (tw + PAD * 2) * 1.45, (th + PAD * 1.4) * 1.55
        boxes.append((bw, bh, tw, th))

    def _overlap(bh: float) -> float:
        return bh * OVERLAP_ELLIPSE  # 타원은 위아래 끝이 좁아 많이 겹쳐야 이어 보인다

    total_h = sum(b[1] for b in boxes) - sum(_overlap(b[1]) for b in boxes[:-1])
    max_bw = max(b[0] for b in boxes)

    x_left = max(w * 0.02, min(w * zx, w - max_bw - w * 0.03))
    y = max(art_top + art_h * 0.02, min(art_top + art_h * zy, art_top + art_h - total_h - art_h * 0.03))

    # 꼬리가 겨눌 지점 — 인물의 얼굴께(상자 위쪽 1/3)를 향한다.
    # 인물 위치를 모르면 자리 반대편에 있다고 가정한다.
    if head:
        target = ((head[0] + head[2]) / 2 * w, art_top + (head[1] + (head[3] - head[1]) * 0.3) * art_h)
        head_px = (head[0] * w, art_top + head[1] * art_h, head[2] * w, art_top + head[3] * art_h)
    else:
        target = (w * (0.25 if zone.startswith("right") else 0.75), art_top + art_h * 0.5)
        head_px = None

    to_left = target[0] < x_left + max_bw / 2  # 인물이 말풍선보다 왼쪽에 있나
    # 말풍선이 인물보다 아래면 꼬리를 위로 붙인다
    tail_up = (y + total_h / 2) > target[1]

    # 꼬리가 위로 가면 첫 조각에, 옆/아래로 가면 마지막 조각에 붙인다
    tail_idx = 0 if tail_up else len(boxes) - 1
    # 도형을 모아 한 번에 그린다 — 텍스트는 도형 위에 올라가야 한다
    shapes: list[list[tuple[float, float]]] = []
    texts_to_draw: list[tuple[list[str], float, float, float]] = []

    for i, (g, (bw, bh, tw, th)) in enumerate(zip(groups, boxes)):
        last = i == tail_idx
        # 이어붙일 때 조각들을 가운데로 맞춰야 한 덩어리로 읽힌다
        x0 = x_left + (max_bw - bw) / 2
        x1, y1 = x0 + bw, y + bh

        # 모양은 둘 다 타원. 대사와 생각은 꼬리로 구분한다.
        # 대사 꼬리는 외곽선을 공유해야 삐져나오지 않으므로 도형에 포함해 그린다.
        tip = None
        if last and not thought:
            ex, ey = _edge_point((x0, y, x1, y1), target, ellipse=True)
            dx, dy = target[0] - ex, target[1] - ey
            dist = max(1.0, (dx * dx + dy * dy) ** 0.5)
            # 짧게 유지한다 — 방향만 가리키면 되고, 길면 화면을 가로질러 어수선해진다
            tip_len = max(20.0, min(38.0, dist * 0.4))
            tip = (ex + dx / dist * tip_len, ey + dy / dist * tip_len)

        shapes.append(_balloon_outline((x0, y, x1, y1), tip))

        if last and thought:  # 생각 — 점이 인물 앞까지 이어진다
            ex, ey = _edge_point((x0, y, x1, y1), target, ellipse=True)
            # 인물 상자 경계에서 멈춘다. target 은 상자 **안쪽**(얼굴께)이라
            # 거기까지 가면 마지막 점이 얼굴 위에 올라가 그림을 가린다.
            stop = _edge_point(head_px, (ex, ey), ellipse=False) if head_px else target
            dx, dy = stop[0] - ex, stop[1] - ey
            for frac, r in zip(TAIL_DOTS, TAIL_SIZES):
                step = frac * TAIL_REACH
                cx, cy = ex + dx * step, ey + dy * step
                shapes.append(_balloon_outline((cx - r, cy - r, cx + r, cy + r)))

        texts_to_draw.append((g, x0 + (bw - tw) / 2, y + (bh - th) / 2, tw))
        y += bh - _overlap(bh)

    return shapes, texts_to_draw, font, size


def _locate_speakers(img, roles: set[str]) -> dict[str, tuple]:
    """화자가 둘 이상인 컷에서만 인물별 위치를 묻는다. 실패하면 빈 dict."""
    try:
        from . import gemini

        if not os.environ.get(gemini.ENV_KEY):
            return {}
        found = gemini.locate_people(img, sorted(roles))
        if found:
            print(f"     화자 위치: {', '.join(found)}")
        return found
    except Exception as e:  # 네트워크·쿼터·형식 — 없으면 주인공에게 건다
        print(f"     화자 위치 질의 실패 ({type(e).__name__}) — 주인공 기준으로 답니다")
        return {}


def compose_cut(
    raw_path: Path,
    texts: list[dict],
    out_path: Path,
    head: tuple | None = None,
) -> tuple[Path, tuple, bool]:
    """컷 하나에 말풍선을 얹는다. 왼쪽부터 순서대로 (PRD F-4)."""
    src = Image.open(raw_path).convert("RGB")
    narrations = [
        (t.get("content") or "").strip()
        for t in texts
        if t.get("type") == "narration" and (t.get("content") or "").strip()
    ]
    balloons = [
        t for t in texts if t.get("type") != "narration" and (t.get("content") or "").strip()
    ]

    # 그림이 캔버스 전체를 채운다 — 흰 띠 없음.
    # RGBA 로 두는 이유: 꼬리를 4배로 그려 부드럽게 얹을 때 alpha 합성이 필요하다
    img = fit_to_canvas(src, ART_W, ART_H).convert("RGBA")

    # 인물 위치를 먼저 알아야 자리를 고를 수 있다 (P-7)
    source = "saved" if head is not None else ""
    if head is None:
        head, source = subject_box(img)

    # 예전에는 인물이 왼쪽에 서면 그림을 좌우로 뒤집어 왼쪽 위 자리를 비웠다.
    # "그림에 글자가 없으니 어색하지 않다" 는 전제였는데, 간판이 나오는 순간 깨진다 —
    # STORE 가 거울상으로 찍혔다. `_pick_zone` 이 머리와 겹치는 자리를 건너뛰므로
    # 인물이 왼쪽이면 알아서 오른쪽 위를 고른다. 뒤집을 이유가 없다.
    head_used = head

    draw = ImageDraw.Draw(img, "RGBA")

    # 나레이션이 있는 컷만 위쪽 말풍선을 내린다. 없으면 첫 말풍선이 화면 위에 붙어
    # 두 번째 말풍선과 사이가 벌어지고, 그 사이를 인물이 채운다.
    shift = NARR_SHIFT if narrations else 0.0

    if narrations:
        _narration(draw, narrations, ART_W, ART_H)

    # 좌표를 얼마나 믿을 수 있느냐에 따라 회피 강도를 정한다.
    # 저장된 값(saved)은 사용자가 고쳤을 수 있으므로 가장 정확한 것으로 취급한다.
    tolerance = TOLERANCE.get(source, TOLERANCE["gemini"])

    # 화자가 여럿이면 각자에게 꼬리를 건다. 시나리오가 화자를 알려주므로(speaker)
    # 그림에서 누가 말하는지 추측할 필요가 없다.
    # `tail` 은 사람이 손으로 정한 꼬리 대상 — 화자 매칭이 빗나갔을 때 덮어쓴다
    speakers = {(t.get("tail") or t.get("speaker") or "").strip() for t in balloons}
    speakers.discard("")
    # 주인공 혼자 말하는 컷은 이미 상자를 알고 있으니 물을 필요가 없다.
    # 화자가 하나여도 그게 주인공이 아니면 물어야 한다 — 안 그러면 점원이 말하는데
    # 꼬리가 주인공을 겨눈다.
    people = _locate_speakers(img, speakers) if speakers - {"주인공"} else {}

    # 화면에 있는 사람을 전부 피한다. 주인공 상자만 봤을 때는 말풍선이 점원 얼굴을 덮었다.
    all_heads = [head_used] + [b for b in people.values() if b]

    used: set[str] = set()
    prev_zone: str | None = None
    for t in balloons:
        zone = (
            t.get("pos")
            if t.get("pos") in ZONES
            else _pick_zone(used, ART_W, 0, ART_H, all_heads, prev_zone, shift, tolerance)
        )
        if zone is None:  # 얼굴을 피할 자리가 없다 — 가장 덜 겹치는 자리에 놓는다
            zone = next((z for z in ZONE_PRIORITY if z not in used), ZONE_PRIORITY[-1])
        used.add(zone)
        # 이 대사의 화자가 어디 있는지 알면 그쪽으로, 모르면 주인공에게
        who = (t.get("tail") or t.get("speaker") or "").strip()
        speaker_box = people.get(who, head_used)
        shapes, texts_to_draw, font, size = _balloon(
            draw, t["content"].strip(), zone, ART_W, 0, ART_H, t.get("type") == "thought", shift, speaker_box
        )
        # 도형을 먼저 부드럽게 얹고, 텍스트를 그 위에 쓴다
        for pts in shapes:
            _draw_smooth(img, pts)
        draw = ImageDraw.Draw(img, "RGBA")  # alpha_composite 뒤에는 다시 잡아야 한다
        for lines, tx, ty, cw in texts_to_draw:
            _draw_lines(draw, lines, font, size, tx, ty, center_w=cw)
        prev_zone = zone

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out_path, "PNG")
    return out_path, head, source


def compose_project(project_dir: Path, project: dict, only: int | None = None) -> list[Path]:
    made = []
    for cut in project["cuts"]:
        if only is not None and cut["index"] != only:
            continue
        raw = project_dir / cut["raw_image"]
        if not raw.exists():
            print(f"  {cut['index']:2d}번 컷: raw 그림이 없습니다 — render 먼저 실행하세요")
            continue
        # 좌표는 한 번만 구한다. 기록해두면 재합성이 공짜고(API 재호출 없음),
        # 틀렸을 때 사용자가 project.json 에서 고칠 수 있다.
        saved = cut.get("subject_box") or cut.get("head_box")  # head_box 는 예전 이름
        path, box, source = compose_cut(
            raw, cut["texts"], project_dir / cut["out_image"], tuple(saved) if saved else None
        )
        if not saved:
            cut["subject_box"] = [round(v, 3) for v in box]
            cut.pop("head_box", None)
        made.append(path)
        tag = {"gemini": "  (인물 전체)", "haar": "  (얼굴만)", "fallback": "  (검출 실패 → 가정값)"}.get(source, "")
        print(f"  {cut['index']:2d}번 컷 → {cut['out_image']}{tag}")
    return made


def _demo() -> None:
    """자체 검사 — 줄바꿈·폰트 축소·규격 변환·말풍선이 캔버스를 벗어나지 않는지."""
    import tempfile

    canvas = Image.new("RGB", (OUT_W, OUT_H), "white")
    draw = ImageDraw.Draw(canvas)

    # 줄바꿈: 어절 단위로 끊기고, 각 줄이 폭 안에 들어와야 한다
    font = ImageFont.truetype(FONT_PATH, 40)
    lines = _wrap(draw, "심장 터질 것 같아 정말로 긴장된다", font, 300)
    assert len(lines) > 1, lines
    assert all(draw.textlength(ln, font=font) <= 300 for ln in lines), lines

    # 공백 없는 긴 문자열도 글자 단위로 쪼개져 폭을 지켜야 한다
    long_lines = _wrap(draw, "가" * 60, font, 300)
    assert len(long_lines) > 1
    assert all(draw.textlength(ln, font=font) <= 300 for ln in long_lines)

    # 폰트 축소: 긴 글은 작은 폰트로 떨어져야 한다
    _, _, big = _fit_text(draw, "짧은 대사", 400, 300)
    _, _, small = _fit_text(draw, "아주 긴 대사입니다 " * 6, 400, 300)
    assert small < big, (big, small)

    # 규격 변환: 어떤 입력 비율이든 정확히 1080x1350
    for size in ((896, 1152), (1024, 1024), (1600, 900)):
        assert fit_to_canvas(Image.new("RGB", size, "gray")).size == (OUT_W, OUT_H)

    # 합성: 나레이션 + 대사 2개가 겹치지 않고 캔버스 안에 들어가야 한다
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        Image.new("RGB", (896, 1152), (255, 0, 0)).save(tmp / "raw.png")
        out, _, _src = compose_cut(
            tmp / "raw.png",
            [
                {"type": "narration", "content": "첫 출근 날 아침."},
                {"type": "dialogue", "content": "심장 터질 것 같아..."},
                {"type": "thought", "content": "괜찮을 거야."},
            ],
            tmp / "out.png",
        )
        assert out.exists()
        result = Image.open(out).convert("RGB")
        assert result.size == (OUT_W, OUT_H)

        # 그림이 캔버스 전체를 채워야 한다 — 흰 띠가 없어야 한다.
        # 네 귀퉁이 근처가 모두 그림(빨강)이면 여백이 없는 것이다.
        for x, y in ((6, 6), (OUT_W - 6, 6), (6, OUT_H - 6), (OUT_W - 6, OUT_H - 6)):
            assert result.getpixel((x, y)) == (255, 0, 0), f"({x},{y})에 흰 여백이 있습니다"

        # 흰 말풍선이 실제로 그려졌는지 (배경이 빨강이므로 순백은 말풍선뿐)
        whites = sum(c for c, px in result.getcolors(1_000_000) if px == (255, 255, 255))
        assert whites > 10_000, f"말풍선이 그려지지 않은 것 같습니다 (흰 픽셀 {whites})"

    # 말풍선은 문장 단위로만 쪼갠다 — 문장 중간에서 끊으면 읽기가 끊긴다
    assert split_sentences("분명 아까 저 나무를 본 것 같았다.") == ["분명 아까 저 나무를 본 것 같았다."]
    assert split_sentences("정말 큰일이다. 어떡하지.") == ["정말 큰일이다.", "어떡하지."]
    assert split_sentences("괜찮을까? 모르겠다!") == ["괜찮을까?", "모르겠다!"]
    assert split_sentences("심장 터질 것 같아...") == ["심장 터질 것 같아..."]  # 말줄임표는 한 덩어리
    assert split_sentences("마침표 없는 대사") == ["마침표 없는 대사"]
    # 아무리 길어도 한 문장이면 하나
    long_one = "분명 아까 저 나무를 본 것 같았고 표시를 해둘 걸 그랬다는 생각이 계속 들었다."
    assert len(split_sentences(long_one)) == 1

    # 이어붙인 말풍선은 하나짜리보다 세로로 길어야 한다 (실제로 조각이 늘어났는지)
    with tempfile.TemporaryDirectory() as tmp3:
        tmp3 = Path(tmp3)
        Image.new("RGB", (1024, 1024), (255, 0, 0)).save(tmp3 / "raw.png")

        def white_rows(texts):
            out, _, _src = compose_cut(tmp3 / "raw.png", texts, tmp3 / f"o{len(texts[0]['content'])}.png")
            im = Image.open(out).convert("RGB")
            # 자리 우선순위가 바뀌면 말풍선이 좌우 어디로든 갈 수 있으니 폭 전체를 훑는다
            return sum(
                1
                for y in range(OUT_H)
                if any(im.getpixel((x, y)) == (255, 255, 255) for x in range(20, OUT_W - 20, 12))
            )

        one = white_rows([{"type": "dialogue", "content": "짧은 대사다."}])
        two = white_rows([{"type": "dialogue", "content": "짧은 대사다. 문장이 하나 더 붙었다."}])
        assert two > one, (one, two)  # 문장이 늘면 조각이 늘어 세로로 길어진다

    # 꼬리 시작점은 말풍선 테두리 위에 있고, 인물 쪽을 향해야 한다
    box_t = (100.0, 100.0, 300.0, 200.0)
    for tgt in ((600.0, 150.0), (50.0, 150.0), (200.0, 500.0), (200.0, 10.0), (500.0, 450.0)):
        for is_ellipse in (False, True):
            ex, ey = _edge_point(box_t, tgt, is_ellipse)
            # 테두리 위(중심과 target 사이)에 있어야 한다
            assert 99 <= ex <= 301 and 99 <= ey <= 201, (ex, ey, tgt, is_ellipse)
            # 중심에서 target 방향으로 나아간 점이어야 한다
            cx, cy = 200.0, 150.0
            assert (ex - cx) * (tgt[0] - cx) >= -1 and (ey - cy) * (tgt[1] - cy) >= -1, (ex, ey, tgt)

    # 대각선에 있는 인물에게는 꼬리도 대각선으로 나가야 한다 (예전엔 4방향뿐이었다)
    diag = _edge_point(box_t, (500.0, 450.0), False)
    assert diag[0] > 200 and diag[1] > 150, diag

    # 생각 꼬리 — 말풍선 바로 아래 점 세 개로 방향만 가리킨다
    assert list(TAIL_DOTS) == sorted(TAIL_DOTS), "점이 멀어지는 순서여야 합니다"
    assert len(TAIL_DOTS) == len(TAIL_SIZES)
    assert list(TAIL_SIZES) == sorted(TAIL_SIZES, reverse=True), "인물 쪽으로 갈수록 작아져야 합니다"
    # 말풍선에서 시작해야 어느 말풍선의 꼬리인지 읽힌다
    assert TAIL_DOTS[0] * TAIL_REACH <= 0.06, f"첫 점이 말풍선에서 떨어져 있습니다: {TAIL_DOTS[0]}"
    # 인물까지 이어 붙이면 배경 위에 점이 흩어지고 마지막 점이 얼굴을 덮는다
    assert TAIL_DOTS[-1] * TAIL_REACH <= 0.35, f"꼬리가 인물까지 뻗습니다: {TAIL_DOTS[-1]}"
    # 점이 벌어지면 세 개가 한 궤적으로 안 읽힌다
    gaps = [b - a for a, b in zip(TAIL_DOTS, TAIL_DOTS[1:])]
    assert max(gaps) <= 0.14, f"점 간격이 넓습니다: {max(gaps):.2f}"
    # 점이 작으면 간격이 좁아도 끊겨 보인다
    assert min(TAIL_SIZES) >= 6, f"가장 작은 점이 너무 작습니다: {min(TAIL_SIZES)}"

    # 대사 꼬리 뿌리는 말풍선 안쪽에서 시작해야 테두리를 덮어 한 덩어리로 보인다.
    # 테두리(width 3) 를 확실히 덮으려면 그보다 충분히 깊어야 한다.
    assert TAIL_INSET > 8, f"뿌리가 얕아 테두리가 드러납니다: {TAIL_INSET}"

    # 아래쪽 자리는 화면 중간보다 확실히 아래여야 한다 (그래야 꼬리가 위로 붙는다)
    assert ZONES[LOWER[0]][1] + ZONE_H / 2 > 0.5, "아래 자리가 중간보다 위에 있습니다"

    # 나레이션이 있으면 위쪽 말풍선이 나레이션 박스 아래로 내려가야 한다
    assert NARRATION_ZONE[3] <= ZONES[UPPER[0]][1] + NARR_SHIFT + 0.02, "나레이션이 말풍선 자리를 침범합니다"
    # 나레이션이 없으면 첫 말풍선이 화면 위쪽에 붙는다 (아래로 처지면 인물이 들어갈 사이가 없다)
    assert ZONES[UPPER[0]][1] < 0.10, ZONES[UPPER[0]][1]

    # P-6: 첫 말풍선은 언제나 왼쪽 위
    assert _pick_zone(set(), OUT_W, 0, OUT_H) == "left-upper"

    # 여러 개면 시선 흐름대로 채운다 — 왼쪽 위 다음은 대각선 아래(그림을 건너 오른쪽 아래)
    used_seq, order = set(), []
    for _ in range(4):
        z = _pick_zone(used_seq, OUT_W, 0, OUT_H)
        used_seq.add(z)
        order.append(z)
    assert order == ZONE_PRIORITY, order

    # 위 두 자리는 같은 높이, 아래 두 자리도 같은 높이여야 한다 (말풍선 먼저 / 그림 / 말풍선)
    assert len({ZONES[z][1] for z in UPPER}) == 1
    assert len({ZONES[z][1] for z in LOWER}) == 1
    assert ZONES[UPPER[0]][1] < ZONES[LOWER[0]][1]

    # 말풍선 → 그림 → 말풍선: 첫 번째는 반드시 위, 두 번째는 반드시 아래 + 반대편
    for head_pos in (
        (0.50, 0.12, 0.66, 0.32),  # 오른쪽에 선 인물
        (0.55, 0.05, 0.74, 0.42),  # 오른쪽에 크게 선 인물
    ):
        first = _pick_zone(set(), OUT_W, 0, OUT_H, head_pos)
        assert first == "left-upper", f"첫 말풍선은 왼쪽 위여야 합니다: {first} (머리 {head_pos})"
        second = _pick_zone({first}, OUT_W, 0, OUT_H, head_pos, prev=first)
        assert second in LOWER, f"두 번째가 그림을 지나지 않았습니다: {second}"
        assert second.split("-")[0] != first.split("-")[0], f"좌우가 안 바뀜: {first} → {second}"

    # 그림은 절대 좌우로 뒤집지 않는다 — 간판 글자가 거울상이 된다.
    # 인물이 왼쪽에 있으면 말풍선이 오른쪽으로 비켜야 한다.
    with tempfile.TemporaryDirectory() as tmp4:
        tmp4 = Path(tmp4)
        # 왼쪽에 인물, 오른쪽 위에 글자 — 글자가 뒤집히면 바로 드러난다
        src4 = Image.new("RGB", (1024, 1024), "white")
        d4 = ImageDraw.Draw(src4)
        d4.rectangle([0, 0, 400, 1023], fill=(30, 30, 30))
        d4.rectangle([900, 40, 1000, 140], fill=(200, 0, 0))  # 오른쪽 위 표식
        src4.save(tmp4 / "raw.png")
        out4, _, _ = compose_cut(
            tmp4 / "raw.png",
            [{"type": "dialogue", "content": "오른쪽 위로 비켜야 한다."}],
            tmp4 / "out4.png",
            head=(0.10, 0.05, 0.35, 0.40),  # 인물이 왼쪽
        )
        res4 = Image.open(out4).convert("RGB")
        mid = OUT_H // 2
        left_dark = sum(1 for x in range(20, 400, 10) if sum(res4.getpixel((x, mid))) < 200)
        right_dark = sum(1 for x in range(OUT_W - 400, OUT_W - 20, 10) if sum(res4.getpixel((x, mid))) < 200)
        assert left_dark > right_dark, f"그림이 뒤집혔습니다 (좌 {left_dark} / 우 {right_dark})"
    assert _pick_zone(used_seq, OUT_W, 0, OUT_H) is None  # 자리를 다 쓰면 없다고 한다

    # P-7 이 P-6 을 이긴다: 머리가 왼쪽 위에 있으면 1순위를 포기한다
    head_left = (0.0, 0.0, 0.5, 0.4)
    picked = _pick_zone(set(), OUT_W, 0, OUT_H, head_left)
    assert picked != "left-upper", f"머리 위에 말풍선을 놓으려 했습니다: {picked}"
    assert not overlaps(_to_art_ratio(_zone_box(picked, OUT_W, 0, OUT_H), OUT_W, 0, OUT_H), head_left, 0.04)

    # P-7: 머리가 화면을 다 덮으면(클로즈업) 놓을 자리가 없다고 답해야 한다 — 밖으로 내보내려고
    assert _pick_zone(set(), OUT_W, 0, OUT_H, (0.0, 0.0, 1.0, 1.0)) is None

    # 대화 컷 — 상자를 여러 개 주면 전부 피해야 한다.
    # 주인공만 피했을 때 말풍선이 점원 얼굴을 덮었다.
    clerk, hero = (0.05, 0.05, 0.45, 0.45), (0.55, 0.05, 0.95, 0.45)
    assert _pick_zone(set(), OUT_W, 0, OUT_H, clerk) == "right-upper"  # 한 명만 피하면 오른쪽 위
    both = _pick_zone(set(), OUT_W, 0, OUT_H, [clerk, hero])
    assert both not in UPPER, f"위쪽이 둘 다 막혔는데 그 자리를 골랐습니다: {both}"
    for box in (clerk, hero):
        assert not overlaps(_to_art_ratio(_zone_box(both, OUT_W, 0, OUT_H), OUT_W, 0, OUT_H), box, 0.04)

    # `tail` 은 화자 매칭이 빗나갔을 때 사람이 손으로 덮어쓰는 값 — speaker 보다 우선한다.
    # 두 필드가 다 있으면 tail 이 이긴다. 없으면 speaker 로 떨어진다.
    def who(t):
        return (t.get("tail") or t.get("speaker") or "").strip()

    assert who({"speaker": "주인공", "tail": "점원"}) == "점원"
    assert who({"speaker": "주인공"}) == "주인공"
    assert who({"speaker": "주인공", "tail": ""}) == "주인공"  # 빈 값은 지정이 아니다
    assert who({}) == ""

    # 검출 실패 시 가정값(상단 절반)이 들어와도 위쪽 자리를 쓸 수 있어야 한다.
    # 추측 때문에 시선 흐름을 깨면 안 된다 — 느슨한 tolerance 를 주는 이유.
    from .head import FALLBACK_BOX

    loose = _pick_zone(set(), OUT_W, 0, OUT_H, FALLBACK_BOX, tolerance=0.40)
    assert loose in UPPER, f"가정값인데도 위쪽 자리를 못 썼습니다: {loose}"

    # 얼굴이 화면 전체를 덮는 극단적 경우에도 말풍선은 그려져야 한다 (자리를 못 피해도 포기하지 않는다)
    with tempfile.TemporaryDirectory() as tmp2:
        tmp2 = Path(tmp2)
        # 마커는 순수 빨강 — 회색으로 하면 텍스트 안티앨리어싱과 구별되지 않는다
        Image.new("RGB", (896, 1152), (255, 0, 0)).save(tmp2 / "raw.png")
        out2, _, _ = compose_cut(
            tmp2 / "raw.png",
            [{"type": "dialogue", "content": "여기는 놓을 자리가 없다"}],
            tmp2 / "out2.png",
            head=(0.0, 0.0, 1.0, 1.0),  # 얼굴이 화면 전체
        )
        res2 = Image.open(out2).convert("RGB")
        assert res2.size == (OUT_W, OUT_H)
        whites2 = sum(c for c, px in res2.getcolors(1_000_000) if px == (255, 255, 255))
        assert whites2 > 10_000, "자리를 못 찾았다고 말풍선을 빼먹었습니다"

    # 생각 말풍선(타원)은 텍스트가 밖으로 새기 쉽다 — 타원 안에 들어가는지 확인한다
    limit = (OUT_W * 0.42 - PAD * 2) * 0.68
    font, lines, size = _fit_text(draw, "괜찮을 거야 정말로 괜찮을 거야", limit, OUT_H * 0.30, bold=True)
    text_w = max(draw.textlength(ln, font=font) for ln in lines)
    text_h = len(lines) * size * LINE_GAP
    # 타원 내접 사각형(폭·높이의 약 0.707배)보다 텍스트가 작아야 넘치지 않는다
    assert text_w <= text_w * 1.45 * 0.707 + 1, "타원 폭이 텍스트를 못 담습니다"
    assert text_h <= text_h * 1.55 * 0.707 + 1, "타원 높이가 텍스트를 못 담습니다"

    print("compose.py 자체 검사 통과")


if __name__ == "__main__":
    _demo()
