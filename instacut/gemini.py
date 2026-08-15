"""Google Gemini(Nano Banana) 이미지 생성 백엔드.

SDXL + IP-Adapter 로는 캐릭터 형태가 컷마다 무너졌다 — 레퍼런스의 형태와 배경 구성이
한 다이얼에 묶여 있어서, 강하게 밀면 배경이 사라지고 약하게 하면 형태가 깨진다.
Gemini 는 참조 이미지를 "이 인물"로 이해하므로 그 트레이드오프가 없다.

ComfyUI 의 Gemini 노드에는 인증 입력이 없어(Comfy 계정 경유 전용) 개인 키를 못 물린다.
그래서 REST 로 직접 부른다. 의존성을 늘리지 않으려고 urllib 만 쓴다.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"
DEFAULT_MODEL = "gemini-3.1-flash-image"

# 이미지를 "읽는" 용도 — 생성보다 훨씬 싸다
VISION_MODEL = "gemini-3.7-flash"

LOCATE_PROMPT = (
    "이 만화 컷에서 **주인공 캐릭터 한 명**이 차지하는 영역을 알려줘.\n"
    "머리 끝부터 발끝까지, 몸 전체를 감싸는 사각형이다.\n"
    "배경의 나무나 다른 사람은 포함하지 마라.\n\n"
    "출력은 JSON 한 줄만. 값은 0~1 비율 (왼쪽 위가 0,0):\n"
    '{"box": [x0, y0, x1, y1]}\n'
    '캐릭터가 없으면 {"box": null}'
)

# 목표는 4:5(0.8)지만 지원 값 중에는 3:4(0.75)가 가장 가깝다.
# 남는 차이는 compose.fit_to_canvas 가 좌우를 잘라 흡수한다.
DEFAULT_ASPECT = "3:4"

ENV_KEY = "GOOGLE_API_KEY"


def api_key() -> str:
    """키는 환경변수에서만 읽는다. 코드나 project.json 에 두지 않는다."""
    key = os.environ.get(ENV_KEY, "").strip()
    if not key:
        raise RuntimeError(
            f"{ENV_KEY} 환경변수가 없습니다.\n"
            f'  PowerShell:  $env:{ENV_KEY} = "<키>"\n'
            f"  또는 .env 에 넣고 셸에서 읽어주세요 (.env 는 커밋되지 않습니다)"
        )
    return key


def build_request(prompt: str, ref_png: bytes | None, aspect: str = DEFAULT_ASPECT, model: str = DEFAULT_MODEL) -> dict:
    """요청 본문을 만든다. 레퍼런스가 있으면 base64 로 함께 보낸다."""
    payload: list[dict] = [{"type": "text", "text": prompt}]
    if ref_png:
        payload.append(
            {
                "type": "image",
                "mime_type": "image/png",
                "data": base64.b64encode(ref_png).decode("ascii"),
            }
        )

    return {
        "model": model,
        "input": payload,
        # 응답은 jpeg 만 지원한다 (png 를 넣으면 400). 입력 레퍼런스는 png 그대로 받는다.
        "response_format": {
            "type": "image",
            "mime_type": "image/jpeg",
            "aspect_ratio": aspect,
            "image_size": "1K",
        },
    }


def extract_image(response: dict) -> bytes:
    """응답에서 이미지 바이트를 꺼낸다.

    **`mime_type` 이 image/* 인 블록만 고른다.** 응답에는 추론 단계(`type: thought`)가
    함께 오는데 그 안의 `signature` 가 989KB 짜리 base64 라서, 단순히 "긴 문자열"을
    찾으면 그걸 이미지로 착각한다 (실제로 그렇게 만들었다가 깨진 파일을 받았다).
    """
    found: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            mime = node.get("mime_type", "")
            data = node.get("data")
            if isinstance(mime, str) and mime.startswith("image/") and isinstance(data, str):
                found.append(data)
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(response)

    # 문서에 적힌 형식도 받아준다 (mime 없이 output_image 로 오는 경우)
    if not found and isinstance(response.get("output_image"), str):
        found.append(response["output_image"])

    if not found:
        raise RuntimeError(f"응답에서 이미지를 찾지 못했습니다: {json.dumps(response)[:600]}")
    return base64.b64decode(found[0])


def generate(
    prompt: str,
    ref: Path | None = None,
    aspect: str = DEFAULT_ASPECT,
    model: str = DEFAULT_MODEL,
    timeout: int = 180,
) -> bytes:
    """컷 하나를 생성해 PNG 바이트를 돌려준다."""
    body = build_request(prompt, ref.read_bytes() if ref else None, aspect, model)
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key()},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return extract_image(json.loads(resp.read().decode("utf-8")))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:600]
        raise RuntimeError(f"Gemini 호출 실패 (HTTP {e.code}): {detail}") from None


def extract_text(response: dict) -> str:
    """응답에서 모델이 쓴 텍스트만 모은다. 추론 단계(type: thought)는 답이 아니다."""
    out: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "thought":
                return
            if node.get("type") == "text" and isinstance(node.get("text"), str):
                out.append(node["text"])
                return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(response)
    return "\n".join(out)


def parse_box(text: str) -> tuple[float, float, float, float] | None:
    """응답 텍스트에서 {"box": [...]} 를 꺼낸다. 코드펜스나 설명이 붙어도 견딘다."""
    m = re.search(r"\{[^{}]*\"box\"[^{}]*\}", text, re.S)
    if not m:
        return None
    try:
        box = json.loads(m.group(0)).get("box")
    except json.JSONDecodeError:
        return None
    if not box or len(box) != 4:
        return None
    x0, y0, x1, y1 = (max(0.0, min(1.0, float(v))) for v in box)
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1)


def locate_subject(img, model: str = VISION_MODEL, timeout: int = 120) -> tuple | None:
    """그림을 보여주고 주인공이 차지하는 영역을 좌표로 받는다.

    로컬 검출(엣지·Haar·밝기·YOLO·원·윤곽)이 전부 실패해서 만들었다 —
    양식화된 캐릭터에는 픽셀 단서가 없지만, 그림을 이해하는 모델은 그냥 본다.
    배경 인물과 주인공도 구분한다.
    """
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    body = {
        "model": model,
        "input": [
            {"type": "text", "text": LOCATE_PROMPT},
            {"type": "image", "mime_type": "image/png", "data": base64.b64encode(buf.getvalue()).decode("ascii")},
        ],
    }
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key()},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return parse_box(extract_text(json.loads(resp.read().decode("utf-8"))))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"좌표 질의 실패 (HTTP {e.code}): {e.read().decode('utf-8', 'replace')[:300]}") from None


def _demo() -> None:
    """API 를 부르지 않고 요청 조립과 응답 파싱만 확인한다."""
    # 레퍼런스 없이 — 텍스트만
    req = build_request("숲을 걷는 사람", None)
    assert req["model"] == DEFAULT_MODEL
    assert len(req["input"]) == 1 and req["input"][0]["type"] == "text"
    assert req["response_format"]["aspect_ratio"] == "3:4"
    # 응답 mime 은 jpeg 여야 한다 — png 로 요청하면 API 가 400 을 낸다
    assert req["response_format"]["mime_type"] == "image/jpeg"

    # 레퍼런스가 있으면 base64 로 실려야 한다
    fake_png = b"\x89PNG\r\n\x1a\n" + b"x" * 400
    req2 = build_request("같은 캐릭터로", fake_png)
    assert len(req2["input"]) == 2
    img_part = req2["input"][1]
    assert img_part["type"] == "image" and img_part["mime_type"] == "image/png"
    assert base64.b64decode(img_part["data"]) == fake_png

    # 응답 파싱 — 문서 형식
    payload = base64.b64encode(b"\x89PNG" + b"y" * 400).decode()
    assert extract_image({"output_image": payload}).startswith(b"\x89PNG")

    # 실제 응답 형식: steps[].content[] 안에 mime_type 이 붙어서 온다
    real = {
        "steps": [
            {"type": "thought", "signature": "E" * 900_000},  # 추론 흔적 — 이미지가 아니다
            {"type": "model_output", "content": [{"type": "image", "mime_type": "image/jpeg", "data": payload}]},
        ]
    }
    assert extract_image(real).startswith(b"\x89PNG"), "추론 signature 를 이미지로 착각했습니다"

    # 못 찾으면 조용히 빈 값을 주지 말고 실패해야 한다
    try:
        extract_image({"status": "ok", "steps": [{"type": "thought", "signature": "E" * 5000}]})
        raise AssertionError("이미지가 없는데 실패하지 않았습니다")
    except RuntimeError:
        pass

    # 좌표 응답 파싱 — 코드펜스나 설명이 붙어도 꺼내야 한다
    assert parse_box('{"box": [0.4, 0.46, 0.61, 0.67]}') == (0.4, 0.46, 0.61, 0.67)
    assert parse_box('```json\n{"box":[0.1,0.2,0.3,0.4]}\n```') == (0.1, 0.2, 0.3, 0.4)
    assert parse_box('여기 있습니다: {"box": [0, 0, 1, 1]} 끝') == (0.0, 0.0, 1.0, 1.0)
    assert parse_box('{"box": null}') is None  # 캐릭터가 없다고 답한 경우
    assert parse_box("좌표를 못 찾겠습니다") is None
    assert parse_box('{"box": [0.5, 0.5, 0.2, 0.9]}') is None  # 뒤집힌 상자는 버린다
    # 범위를 벗어난 값은 잘라낸다
    assert parse_box('{"box": [-0.3, 0.1, 1.4, 0.9]}') == (0.0, 0.1, 1.0, 0.9)

    # 텍스트 추출 — 추론 단계는 답이 아니다
    resp = {
        "steps": [
            {"type": "thought", "signature": "E" * 5000, "text": "생각 중..."},
            {"type": "model_output", "content": [{"type": "text", "text": '{"box": [0.2, 0.3, 0.5, 0.8]}'}]},
        ]
    }
    assert parse_box(extract_text(resp)) == (0.2, 0.3, 0.5, 0.8), "추론 단계를 답으로 착각했습니다"

    # 키가 없으면 명확히 알려야 한다
    saved = os.environ.pop(ENV_KEY, None)
    try:
        api_key()
        raise AssertionError("키가 없는데 통과했습니다")
    except RuntimeError as e:
        assert ENV_KEY in str(e)
    finally:
        if saved:
            os.environ[ENV_KEY] = saved

    print("gemini.py 자체 검사 통과")


if __name__ == "__main__":
    _demo()
