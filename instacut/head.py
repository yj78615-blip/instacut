"""인물의 머리 위치를 찾는다 — 말풍선이 얼굴을 덮지 않게 하려고 (PRD P-7).

배경 복잡도로는 이 규칙을 지킬 수 없다. 숲처럼 배경이 조밀하고 인물이 매끈한 컷에서는
얼굴 쪽이 오히려 "한산한 곳"으로 뽑힌다. 얼굴이 어디 있는지 실제로 알아야 한다.

검출이 실패해도 안전한 쪽으로 실패해야 한다 — 못 찾았다고 얼굴 위에 얹으면 안 되므로,
마지막 폴백은 "인물은 대개 가운데 위쪽에 있다"는 가정이다.
"""

from __future__ import annotations

import os

import cv2
import numpy as np
from PIL import Image

# Haar 는 얼굴만 잡는다. 머리카락·이마·턱까지 감싸도록 넓힌다.
#
# 넉넉하게(위 0.75 / 옆 0.28) 잡았더니 박스가 화면 위쪽을 크게 먹어서,
# 비어 있는 왼쪽 위 자리까지 못 쓰고 말풍선이 전부 아래로 밀렸다.
# 머리카락을 덮을 만큼만 남기고 조인다 — 여기서 더 줄이면 정수리를 가리기 시작한다.
GROW_UP = 0.45  # 위로 (머리카락)
GROW_SIDE = 0.16
GROW_DOWN = 0.12  # 아래로 (턱·목)

# 검출 실패 시 가정하는 머리 위치.
#
# 처음엔 (0.22, 0.0, 0.78, 0.46) 으로 화면 상단 절반을 잡았는데, 그러면 위쪽 말풍선
# 자리의 58% 를 덮어 자리가 통째로 막힌다. 양식화된 캐릭터에서 Haar 가 자주 실패하므로
# **추측 하나 때문에 말풍선이 전부 아래로 몰리는 일**이 실제로 생겼다.
# 인물이 있을 법한 가운데만 보호하고 가장자리는 열어둔다.
FALLBACK_BOX = (0.34, 0.12, 0.66, 0.48)

_CASCADES = (
    ("haarcascade_frontalface_default.xml", False),
    ("haarcascade_frontalface_alt2.xml", False),
    ("haarcascade_profileface.xml", False),
    ("haarcascade_profileface.xml", True),  # 좌우 반전 = 반대쪽 측면
)


def _detect_faces(gray: np.ndarray) -> list[tuple[int, int, int, int]]:
    h, w = gray.shape
    # 오검출은 거르되 wide shot 의 작은 얼굴은 살려야 한다.
    # 0.10 으로 잡았더니 인물이 멀리 있는 컷에서 얼굴을 통째로 놓쳤다.
    min_side = max(40, int(min(h, w) * 0.06))
    equalized = cv2.equalizeHist(gray)

    found: list[tuple[int, int, int, int]] = []
    for name, flip in _CASCADES:
        cascade = cv2.CascadeClassifier(cv2.data.haarcascades + name)
        if cascade.empty():
            continue
        target = cv2.flip(equalized, 1) if flip else equalized
        for x, y, fw, fh in cascade.detectMultiScale(
            target, scaleFactor=1.08, minNeighbors=6, minSize=(min_side, min_side)
        ):
            if flip:
                x = w - x - fw
            found.append((int(x), int(y), int(fw), int(fh)))
    return found


def detect_head(img: Image.Image) -> tuple[float, float, float, float] | None:
    """그림에서 주인공의 머리를 찾는다. 반환은 이미지 대비 비율 (x0, y0, x1, y1).

    못 찾으면 None — 호출자가 폴백을 결정한다.
    """
    gray = np.array(img.convert("L"))
    faces = _detect_faces(gray)
    if not faces:
        return None

    x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])  # 가장 큰 얼굴 = 주인공
    h, w = gray.shape

    x0 = (x - fw * GROW_SIDE) / w
    y0 = (y - fh * GROW_UP) / h
    x1 = (x + fw * (1 + GROW_SIDE)) / w
    y1 = (y + fh * (1 + GROW_DOWN)) / h
    return (max(0.0, x0), max(0.0, y0), min(1.0, x1), min(1.0, y1))


def head_box(img: Image.Image) -> tuple[tuple[float, float, float, float], bool]:
    """머리 영역과 '실제로 검출했는지' 여부. 실패하면 안전한 가정값을 돌려준다."""
    found = detect_head(img)
    return (found, True) if found else (FALLBACK_BOX, False)


def subject_box(img: Image.Image, use_api: bool = True) -> tuple[tuple[float, float, float, float], str]:
    """말풍선이 피해야 할 영역과 그 출처.

    출처에 따라 신뢰도가 다르므로 호출자가 회피 강도를 조절한다 (compose.TOLERANCE).

      gemini   — 인물 **몸 전체**. 배경 인물과 주인공도 구분한다. 가장 정확
      haar     — 얼굴만. 실사풍에는 잘 맞지만 양식화된 캐릭터는 놓친다
      fallback — 가정값. 추측이므로 느슨하게 적용해야 한다

    로컬 검출을 여섯 가지(엣지·Haar·밝기·YOLO·원·윤곽) 시도했는데 양식화된
    캐릭터에서는 전부 실패했다. 픽셀에 "사람다움"이 없기 때문이다.
    """
    if use_api and os.environ.get("GOOGLE_API_KEY", "").strip():
        try:
            from . import gemini

            box = gemini.locate_subject(img)
            if box:
                return box, "gemini"
        except Exception as e:  # 네트워크·쿼터·형식 무엇이든 로컬 검출로 내려간다
            print(f"  (좌표 질의 실패 → 로컬 검출로 대체: {str(e)[:80]})")

    face = detect_head(img)
    return (face, "haar") if face else (FALLBACK_BOX, "fallback")


def overlaps(box_a, box_b, tolerance: float = 0.0) -> bool:
    """두 사각형이 겹치는지. tolerance 만큼은 스쳐도 괜찮은 것으로 본다."""
    ax0, ay0, ax1, ay1 = box_a
    bx0, by0, bx1, by1 = box_b
    inter_w = min(ax1, bx1) - max(ax0, bx0)
    inter_h = min(ay1, by1) - max(ay0, by0)
    if inter_w <= 0 or inter_h <= 0:
        return False
    area_a = (ax1 - ax0) * (ay1 - ay0)
    return area_a > 0 and (inter_w * inter_h) / area_a > tolerance


def _demo() -> None:
    from PIL import ImageDraw

    # 겹침 판정
    assert overlaps((0, 0, 1, 1), (0.5, 0.5, 1.5, 1.5))
    assert not overlaps((0, 0, 0.4, 0.4), (0.5, 0.5, 1, 1))
    assert not overlaps((0, 0, 1, 0.1), (0, 0.099, 1, 1), tolerance=0.05)  # 살짝 스치는 건 허용

    # 얼굴이 없는 그림에서는 검출 실패 → 안전한 가정값
    blank = Image.new("RGB", (600, 800), "white")
    box, detected = head_box(blank)
    assert detected is False
    assert box == FALLBACK_BOX

    # API 를 끄면 로컬 경로로만 간다 (출처가 함께 온다)
    box2, source = subject_box(blank, use_api=False)
    assert source == "fallback" and box2 == FALLBACK_BOX
    canvas2 = Image.new("RGB", (600, 800), "white")
    assert subject_box(canvas2, use_api=False)[1] in ("haar", "fallback")

    # 폴백은 가운데(인물이 대개 있는 곳)만 보호하고 가장자리는 열어둬야 한다.
    # 너무 넓으면 말풍선 자리를 다 막아 시선 흐름이 깨진다.
    fx0, fy0, fx1, fy1 = FALLBACK_BOX
    assert fx0 < 0.5 < fx1, "가운데를 덮지 않습니다"
    assert fx0 > 0.25 and fx1 < 0.75, "좌우로 너무 넓습니다 (말풍선 자리를 막는다)"
    assert (fx1 - fx0) * (fy1 - fy0) < 0.20, "폴백 영역이 화면의 20% 를 넘습니다"

    # 확장 규칙: 검출된 얼굴보다 머리 영역이 항상 크고 위로 더 뻗어야 한다
    canvas = Image.new("RGB", (400, 500), "white")
    ImageDraw.Draw(canvas).rectangle([100, 200, 200, 320], fill="black")
    fake_face = (100, 200, 100, 120)
    x, y, fw, fh = fake_face
    assert (y - fh * GROW_UP) / 500 < y / 500  # 위로 확장된다
    assert GROW_UP > GROW_DOWN  # 머리카락은 턱보다 위로 많이 뻗는다

    print("head.py 자체 검사 통과")


if __name__ == "__main__":
    _demo()
