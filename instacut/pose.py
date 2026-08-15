"""말풍선 자리를 피해 인물을 세울 위치를 정하고, OpenPose 스틱 피규어를 그린다.

프롬프트로 "왼쪽 위를 비워라"를 아무리 넣어도 모델이 듣지 않았다 (여러 번 확인).
그래서 순서를 뒤집는다 — **말풍선 자리를 먼저 계산하고, 인물을 남는 자리에 세운다** (PRD P-6).
그 위치를 ControlNet 이 강제한다.

OpenPose 관절 순서는 COCO 18 포인트 규약을 따른다.
"""

from __future__ import annotations

from PIL import Image, ImageDraw

# COCO 18 관절. (이름, 부모) — 부모가 None 이면 뿌리
JOINTS = [
    "nose", "neck",
    "r_shoulder", "r_elbow", "r_wrist",
    "l_shoulder", "l_elbow", "l_wrist",
    "r_hip", "r_knee", "r_ankle",
    "l_hip", "l_knee", "l_ankle",
    "r_eye", "l_eye", "r_ear", "l_ear",
]

# OpenPose 표준 색상 (ControlNet 이 이 색을 보고 부위를 구분한다)
LIMBS = [
    ("neck", "r_shoulder", (255, 85, 0)),
    ("r_shoulder", "r_elbow", (255, 170, 0)),
    ("r_elbow", "r_wrist", (255, 255, 0)),
    ("neck", "l_shoulder", (170, 255, 0)),
    ("l_shoulder", "l_elbow", (85, 255, 0)),
    ("l_elbow", "l_wrist", (0, 255, 0)),
    ("neck", "r_hip", (0, 255, 85)),
    ("r_hip", "r_knee", (0, 255, 170)),
    ("r_knee", "r_ankle", (0, 255, 255)),
    ("neck", "l_hip", (0, 170, 255)),
    ("l_hip", "l_knee", (0, 85, 255)),
    ("l_knee", "l_ankle", (0, 0, 255)),
    ("neck", "nose", (255, 0, 0)),
    ("nose", "r_eye", (255, 0, 85)),
    ("r_eye", "r_ear", (255, 0, 170)),
    ("nose", "l_eye", (170, 0, 255)),
    ("l_eye", "l_ear", (85, 0, 255)),
]

JOINT_COLORS = {
    "nose": (255, 0, 0), "neck": (255, 85, 0),
    "r_shoulder": (255, 170, 0), "r_elbow": (255, 255, 0), "r_wrist": (255, 255, 0),
    "l_shoulder": (170, 255, 0), "l_elbow": (85, 255, 0), "l_wrist": (0, 255, 0),
    "r_hip": (0, 255, 85), "r_knee": (0, 255, 170), "r_ankle": (0, 255, 255),
    "l_hip": (0, 170, 255), "l_knee": (0, 85, 255), "l_ankle": (0, 0, 255),
    "r_eye": (255, 0, 85), "l_eye": (170, 0, 255), "r_ear": (255, 0, 170), "l_ear": (85, 0, 255),
}


def standing_pose(cx: float, head_y: float, height: float) -> dict[str, tuple[float, float]]:
    """서 있는 인체 비율의 관절 좌표. 모두 0~1 비율.

    cx: 몸 중심의 x, head_y: 머리 꼭대기 y, height: 머리끝~발끝 높이
    """
    h = height
    w = h * 0.22  # 어깨 폭 절반

    def p(dx: float, dy: float) -> tuple[float, float]:
        return (cx + dx * w, head_y + dy * h)

    return {
        "nose": p(0, 0.06),
        "r_eye": p(-0.25, 0.05), "l_eye": p(0.25, 0.05),
        "r_ear": p(-0.5, 0.06), "l_ear": p(0.5, 0.06),
        "neck": p(0, 0.14),
        "r_shoulder": p(-1.0, 0.16), "l_shoulder": p(1.0, 0.16),
        "r_elbow": p(-1.2, 0.34), "l_elbow": p(1.2, 0.34),
        "r_wrist": p(-1.25, 0.52), "l_wrist": p(1.25, 0.52),
        "r_hip": p(-0.55, 0.54), "l_hip": p(0.55, 0.54),
        "r_knee": p(-0.6, 0.76), "l_knee": p(0.6, 0.76),
        "r_ankle": p(-0.6, 0.98), "l_ankle": p(0.6, 0.98),
    }


def place_subject(reserved: list[tuple[float, float, float, float]]) -> tuple[float, float, float]:
    """말풍선이 차지한 자리를 피해 인물을 세울 (중심x, 머리y, 키)를 정한다.

    말풍선은 위/아래 구석에 놓이므로, 인물은 그 반대쪽 가로 방향으로 비켜선다.
    """
    if not reserved:
        return 0.5, 0.10, 0.86  # 아무도 안 쓰면 가운데

    # 예약된 자리들의 x 중심 평균 — 인물은 그 반대쪽으로
    xs = [(b[0] + b[2]) / 2 for b in reserved]
    avg_x = sum(xs) / len(xs)
    cx = 0.72 if avg_x < 0.5 else 0.28

    # 위쪽이 예약됐으면 인물을 아래로 내려 머리를 비켜준다
    top_used = any(b[1] < 0.35 for b in reserved)
    head_y = 0.30 if top_used else 0.10
    height = 0.66 if top_used else 0.86
    return cx, head_y, height


def draw_pose(size: int, cx: float, head_y: float, height: float) -> Image.Image:
    """OpenPose 스틱 피규어. 검은 배경에 관절과 뼈대만."""
    img = Image.new("RGB", (size, size), "black")
    draw = ImageDraw.Draw(img)
    pts = {k: (x * size, y * size) for k, (x, y) in standing_pose(cx, head_y, height).items()}

    for a, b, color in LIMBS:
        draw.line([pts[a], pts[b]], fill=color, width=max(4, size // 128))
    for name, (x, y) in pts.items():
        r = max(3, size // 180)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=JOINT_COLORS[name])
    return img


def _demo() -> None:
    # 관절이 빠짐없이 있어야 뼈대를 그릴 수 있다
    pose = standing_pose(0.5, 0.1, 0.8)
    assert set(pose) == set(JOINTS), set(JOINTS) ^ set(pose)
    for a, b, _ in LIMBS:
        assert a in pose and b in pose

    # 인체 비율: 머리가 맨 위, 발목이 맨 아래, 어깨는 엉덩이보다 위
    assert pose["nose"][1] < pose["neck"][1] < pose["r_hip"][1] < pose["r_ankle"][1]
    assert pose["r_shoulder"][0] < pose["l_shoulder"][0]  # 오른쪽이 화면 왼쪽

    # 키가 커지면 발목이 더 내려간다
    tall = standing_pose(0.5, 0.1, 0.9)
    assert tall["r_ankle"][1] > pose["r_ankle"][1]

    # 말풍선이 왼쪽에 있으면 인물은 오른쪽으로 비켜야 한다
    cx, _, _ = place_subject([(0.045, 0.03, 0.465, 0.27)])
    assert cx > 0.5, cx
    cx2, _, _ = place_subject([(0.535, 0.03, 0.955, 0.27)])
    assert cx2 < 0.5, cx2

    # 위쪽이 예약되면 인물이 내려가고 작아진다
    _, hy_top, h_top = place_subject([(0.045, 0.03, 0.465, 0.27)])
    _, hy_bot, h_bot = place_subject([(0.045, 0.62, 0.465, 0.86)])
    assert hy_top > hy_bot, (hy_top, hy_bot)
    assert h_top < h_bot

    # 그림이 캔버스 안에 들어가고, 검은 배경 위에 실제로 뭔가 그려진다
    img = draw_pose(512, 0.7, 0.3, 0.66)
    assert img.size == (512, 512)
    colors = [c for c, px in img.getcolors(100_000) if px != (0, 0, 0)]
    assert sum(colors) > 500, "스틱 피규어가 거의 안 그려졌습니다"

    print("pose.py 자체 검사 통과")


if __name__ == "__main__":
    _demo()
