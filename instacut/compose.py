"""[4] 말풍선 합성 — raw 그림 + 한국어 텍스트 → out 최종 컷

생성 직후 자동으로 돌지 않는다. 사용자가 raw/ 를 보고 나서 부른다 (PRD F-4).
raw/ 는 절대 덮어쓰지 않는다. 텍스트를 몇 번 고쳐도 그림은 그대로 남아야 한다.
"""

from __future__ import annotations

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
    """
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
        if head and overlaps(box, head, tolerance=tolerance):
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
    # 타원은 안쪽 가용 폭이 사각형보다 좁다. 텍스트를 먼저 좁게 잡아야 밖으로 새지 않는다
    text_limit = (box_w - PAD * 2) * (0.68 if thought else 1.0)
    font, _, size = _fit_text(draw, text, text_limit, art_h * 0.44, bold=True)

    # 폰트는 전체 기준으로 한 번 정하고, 줄바꿈은 문장별로 따로 한다.
    # 문장 하나가 여러 줄이어도 그 문장은 한 말풍선에 통째로 들어간다.
    groups = [_wrap(draw, s, font, text_limit) for s in split_sentences(text)]

    # 조각마다 크기를 따로 잰다 — 줄 수가 다르면 크기도 달라야 자연스럽다
    boxes = []
    for g in groups:
        tw = max(draw.textlength(ln, font=font) for ln in g)
        th = len(g) * size * LINE_GAP
        bw, bh = tw + PAD * 2, th + PAD * 1.4
        if thought:  # 텍스트 박스를 감싸는 타원은 더 커야 한다
            bw, bh = bw * 1.45, bh * 1.55
        boxes.append((bw, bh, tw, th))

    def _overlap(bh: float) -> float:
        return bh * OVERLAP_ELLIPSE if thought else OVERLAP_RECT

    total_h = sum(b[1] for b in boxes) - sum(_overlap(b[1]) for b in boxes[:-1])
    max_bw = max(b[0] for b in boxes)

    x_left = max(w * 0.02, min(w * zx, w - max_bw - w * 0.03))
    y = max(art_top + art_h * 0.02, min(art_top + art_h * zy, art_top + art_h - total_h - art_h * 0.03))

    # 꼬리는 말하는 인물을 향한다. 인물 위치를 모르면 자리 반대편으로 가정한다
    if head:
        head_cx = (head[0] + head[2]) / 2 * w
    else:
        head_cx = w * (0.25 if zone.startswith("right") else 0.75)
    to_left = head_cx < x_left + max_bw / 2  # 인물이 말풍선보다 왼쪽에 있나

    # 말풍선이 화면 중간보다 아래면 인물이 위에 있다 → 꼬리를 위로 붙인다
    tail_up = (y + total_h / 2) > art_top + art_h * 0.5

    # 꼬리가 위로 가면 첫 조각에, 옆/아래로 가면 마지막 조각에 붙인다
    tail_idx = 0 if tail_up else len(boxes) - 1

    for i, (g, (bw, bh, tw, th)) in enumerate(zip(groups, boxes)):
        last = i == tail_idx
        # 이어붙일 때 조각들을 가운데로 맞춰야 한 덩어리로 읽힌다
        x0 = x_left + (max_bw - bw) / 2
        x1, y1 = x0 + bw, y + bh

        if thought:
            draw.ellipse([x0, y, x1, y1], fill="white", outline="black", width=3)
            if last:  # 꼬리 점은 한 조각에만
                for k, r in enumerate((13, 8, 5)):
                    if tail_up:  # 인물이 위에 있다 — 점이 위로 올라간다
                        cx = x0 + bw * (0.3 if to_left else 0.7) - k * (14 if to_left else -14)
                        cy = y - 12 - k * 17
                    else:
                        cx = (x0 - 14 - k * 22) if to_left else (x1 + 14 + k * 22)
                        cy = y1 + 10 + k * 16
                    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill="white", outline="black", width=3)
            tx, ty, cw = x0 + (bw - tw) / 2, y + (bh - th) / 2, tw
        else:
            draw.rounded_rectangle([x0, y, x1, y1], radius=22, fill="white", outline="black", width=3)
            if last:
                if tail_up:  # 꼬리를 말풍선 위쪽 모서리에서 인물 쪽으로 뻗는다
                    base_x = x0 + bw * (0.28 if to_left else 0.72)
                    tip_x = base_x + (-30 if to_left else 30)
                    pts = [(base_x - 20, y + 6), (base_x + 20, y + 6), (tip_x, y - 38)]
                else:
                    tail_y = y1 - bh * 0.28
                    tip, base = (x0 - 34, x0 + 6) if to_left else (x1 + 34, x1 - 6)
                    pts = [(base, tail_y), (base, tail_y + 34), (tip, tail_y + 14)]
                draw.polygon(pts, fill="white", outline="black")
                draw.line([pts[0], pts[2], pts[1]], fill="black", width=3)
            tx, ty, cw = x0 + PAD, y + PAD * 0.7, bw - PAD * 2

        _draw_lines(draw, g, font, size, tx, ty, center_w=cw)  # 말풍선 안 텍스트는 가운데 정렬
        y += bh - _overlap(bh)


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

    # 그림이 캔버스 전체를 채운다 — 흰 띠 없음
    img = fit_to_canvas(src, ART_W, ART_H)

    # 인물 위치를 먼저 알아야 자리를 고를 수 있다 (P-7)
    source = "saved" if head is not None else ""
    if head is None:
        head, source = subject_box(img)

    # 말풍선은 왼쪽 위가 1순위다(P-6). ControlNet 이 인물을 오른쪽에 세우지만,
    # 그래도 왼쪽에 서면 그림을 뒤집어 자리를 비운다 (그림에 글자가 없으니 어색하지 않다).
    # 저장·재사용되는 head 는 원본 기준 좌표다. 그래야 재합성할 때 같은 판정이 나온다.
    head_used = head
    if (head[0] + head[2]) / 2 < 0.5:
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
        head_used = (1 - head[2], head[1], 1 - head[0], head[3])

    draw = ImageDraw.Draw(img, "RGBA")

    # 나레이션이 있는 컷만 위쪽 말풍선을 내린다. 없으면 첫 말풍선이 화면 위에 붙어
    # 두 번째 말풍선과 사이가 벌어지고, 그 사이를 인물이 채운다.
    shift = NARR_SHIFT if narrations else 0.0

    if narrations:
        _narration(draw, narrations, ART_W, ART_H)

    # 좌표를 얼마나 믿을 수 있느냐에 따라 회피 강도를 정한다.
    # 저장된 값(saved)은 사용자가 고쳤을 수 있으므로 가장 정확한 것으로 취급한다.
    tolerance = TOLERANCE.get(source, TOLERANCE["gemini"])

    used: set[str] = set()
    prev_zone: str | None = None
    for t in balloons:
        zone = (
            t.get("pos")
            if t.get("pos") in ZONES
            else _pick_zone(used, ART_W, 0, ART_H, head_used, prev_zone, shift, tolerance)
        )
        if zone is None:  # 얼굴을 피할 자리가 없다 — 가장 덜 겹치는 자리에 놓는다
            zone = next((z for z in ZONE_PRIORITY if z not in used), ZONE_PRIORITY[-1])
        used.add(zone)
        _balloon(
            draw, t["content"].strip(), zone, ART_W, 0, ART_H, t.get("type") == "thought", shift, head_used
        )
        prev_zone = zone

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")
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

    # 인물이 왼쪽에 있으면 그림을 뒤집어 오른쪽으로 보낸다 — 왼쪽 위를 비우기 위해
    with tempfile.TemporaryDirectory() as tmp4:
        tmp4 = Path(tmp4)
        # 왼쪽 절반만 진하게 칠한 그림 = 인물이 왼쪽에 있는 상황
        src4 = Image.new("RGB", (1024, 1024), "white")
        ImageDraw.Draw(src4).rectangle([0, 0, 400, 1023], fill=(30, 30, 30))
        src4.save(tmp4 / "raw.png")
        out4, _, _ = compose_cut(
            tmp4 / "raw.png",
            [{"type": "dialogue", "content": "왼쪽 위에 와야 한다."}],
            tmp4 / "out4.png",
            head=(0.10, 0.05, 0.35, 0.40),  # 인물이 왼쪽
        )
        res4 = Image.open(out4).convert("RGB")
        # 반전됐다면 진한 영역이 오른쪽으로 갔어야 한다
        mid = OUT_H // 2
        left_dark = sum(1 for x in range(20, 400, 10) if sum(res4.getpixel((x, mid))) < 200)
        right_dark = sum(1 for x in range(OUT_W - 400, OUT_W - 20, 10) if sum(res4.getpixel((x, mid))) < 200)
        assert right_dark > left_dark, f"인물이 오른쪽으로 가지 않았습니다 (좌 {left_dark} / 우 {right_dark})"
    assert _pick_zone(used_seq, OUT_W, 0, OUT_H) is None  # 자리를 다 쓰면 없다고 한다

    # P-7 이 P-6 을 이긴다: 머리가 왼쪽 위에 있으면 1순위를 포기한다
    head_left = (0.0, 0.0, 0.5, 0.4)
    picked = _pick_zone(set(), OUT_W, 0, OUT_H, head_left)
    assert picked != "left-upper", f"머리 위에 말풍선을 놓으려 했습니다: {picked}"
    assert not overlaps(_to_art_ratio(_zone_box(picked, OUT_W, 0, OUT_H), OUT_W, 0, OUT_H), head_left, 0.04)

    # P-7: 머리가 화면을 다 덮으면(클로즈업) 놓을 자리가 없다고 답해야 한다 — 밖으로 내보내려고
    assert _pick_zone(set(), OUT_W, 0, OUT_H, (0.0, 0.0, 1.0, 1.0)) is None

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
