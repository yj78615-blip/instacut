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

# 좌표는 배열이 아니라 **이름 붙은 필드**로 받는다.
# `[x0, y0, x1, y1]` 로 받던 시절, 모델이 자기 관례인 [y, x, y, x] 순서로 답해
# 말풍선 꼬리가 엉뚱한 곳을 가리켰다. 순서는 지킬 의무가 없는 약속이지만
# 필드 이름은 뒤바뀔 수가 없다.
_COORD_RULE = (
    "출력은 JSON 한 줄만. 좌표는 0~1 비율이고 필드 이름을 반드시 지켜라:\n"
    "  x = 왼쪽 끝 0.0 → 오른쪽 끝 1.0\n"
    "  y = 위쪽 끝 0.0 → 아래쪽 끝 1.0\n"
)

LOCATE_PROMPT = (
    "이 만화 컷에서 **주인공 캐릭터 한 명**이 차지하는 영역을 알려줘.\n"
    "머리 끝부터 발끝까지, 몸 전체를 감싸는 사각형이다.\n"
    "배경의 나무나 다른 사람은 포함하지 마라.\n\n"
    + _COORD_RULE
    + '{"box": {"x0": 0.30, "y0": 0.12, "x1": 0.60, "y1": 0.90}}\n'
    '캐릭터가 없으면 {"box": null}'
)

PEOPLE_PROMPT = (
    "이 만화 컷에 있는 **사람(캐릭터)마다** 위치를 알려줘.\n"
    "각각 머리 끝부터 발끝까지 몸 전체를 감싸는 사각형이다.\n"
    "배경의 흐릿한 군중은 빼고, 장면에 의미 있게 등장하는 인물만.\n\n"
    "역할 후보: {roles}\n"
    "각 인물이 이 중 누구인지 label 에 적어라. 확실하지 않으면 가장 그럴듯한 것을 고른다.\n\n"
    + _COORD_RULE  # 중괄호가 없어 .format 에 안전하다
    + '{{"people": [{{"label": "주인공", "x0": 0.30, "y0": 0.12, "x1": 0.60, "y1": 0.90}}]}}\n'
    '사람이 없으면 {{"people": []}}'
)

# 캔버스가 1080x1080 정사각이므로 생성도 1:1 — 잘리는 부분이 없다.
DEFAULT_ASPECT = "1:1"

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


def _box_of(node) -> tuple[float, float, float, float] | None:
    """x0/y0/x1/y1 필드에서 상자를 꺼낸다. 예전 [x0,y0,x1,y1] 배열도 받아준다."""
    if isinstance(node, dict) and "box" in node and not {"x0", "y0"} <= node.keys():
        node = node["box"]
    if isinstance(node, dict):
        try:
            vals = [float(node[k]) for k in ("x0", "y0", "x1", "y1")]
        except (KeyError, TypeError, ValueError):
            return None
    elif isinstance(node, (list, tuple)) and len(node) == 4:
        try:
            vals = [float(v) for v in node]
        except (TypeError, ValueError):
            return None
    else:
        return None

    x0, y0, x1, y1 = (max(0.0, min(1.0, v)) for v in vals)
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1)


def parse_box(text: str) -> tuple[float, float, float, float] | None:
    """응답 텍스트에서 주인공 상자를 꺼낸다. 코드펜스나 설명이 붙어도 견딘다."""
    m = re.search(r"\{.*\"box\".*\}", text, re.S)
    if not m:
        return None
    try:
        return _box_of(json.loads(m.group(0)))
    except json.JSONDecodeError:
        return None


def parse_people(text: str, roles: list[str] | None = None) -> dict[str, tuple]:
    """응답에서 {"people": [{"label", "x0"...}]} 를 꺼내 라벨→상자 로 만든다.

    `roles` 를 주면 그 이름으로 정규화한다. 후보를 프롬프트로 줘도 모델이 벗어난
    이름을 답하기 때문이다 — `주인공` 대신 `손님` 이 왔고, 그러면 매칭이 빗나가
    꼬리가 엉뚱한 사람을 겨눴다. 지시를 강화하는 것만으로는 못 믿는다.
    """
    m = re.search(r"\{.*\"people\".*\}", text, re.S)
    if not m:
        return {}
    try:
        people = json.loads(m.group(0)).get("people") or []
    except json.JSONDecodeError:
        return {}

    found: list[tuple[str, tuple]] = []
    for p in people:
        label = (p.get("label") or "").strip()
        box = _box_of(p)
        if label and box:
            found.append((label, box))

    if roles is None:  # 정규화 없이 그대로 (같은 라벨이 겹치면 첫 것을 쓴다)
        return {label: box for label, box in reversed(found)}

    return _match_roles(found, roles)


def _match_roles(found: list[tuple[str, tuple]], roles: list[str]) -> dict[str, tuple]:
    """응답 라벨을 후보 이름으로 맞춘다.

    1. 정확히 일치하는 것부터 가져간다
    2. 남은 응답을 남은 후보에 순서대로 배정한다 (`손님` → `주인공`)
    3. 그래도 남으면 버린다 — 모르는 사람을 화자로 만들지 않는다
    """
    out: dict[str, tuple] = {}
    left = list(found)

    for label, box in list(left):
        if label in roles and label not in out:
            out[label] = box
            left.remove((label, box))

    # 남은 개수가 정확히 맞을 때만 배정한다. 응답이 후보보다 적으면 누가 빠졌는지
    # 알 수 없어서, 남은 후보 중 아무에게나 붙이면 꼬리가 엉뚱한 사람을 겨눈다.
    # 그럴 바에는 비워두는 편이 낫다 — 호출자가 주인공 상자로 폴백한다.
    remaining = [r for r in roles if r not in out]
    if len(left) == len(remaining):
        for (label, box), role in zip(left, remaining):
            out[role] = box

    return out


def locate_people(img, roles: list[str], model: str = VISION_MODEL, timeout: int = 120) -> dict[str, tuple]:
    """컷에 있는 인물들의 위치를 역할 이름과 함께 받는다.

    말풍선 꼬리를 **말하는 사람**에게 걸기 위한 것이다. 시나리오가 화자를 알려주니
    (`texts[].speaker`) 그 이름을 후보로 주고 매칭시킨다.
    """
    prompt = PEOPLE_PROMPT.format(roles=", ".join(roles) if roles else "주인공")
    # 후보를 넘겨 응답 라벨을 시나리오 이름으로 정규화한다 — 모델이 후보를 벗어나기 때문이다
    return parse_people(extract_text(_ask(img, prompt, model, timeout)), roles)


def _ask(img, prompt: str, model: str, timeout: int) -> dict:
    """이미지 한 장과 프롬프트를 보내고 응답 JSON 을 받는다."""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    body = {
        "model": model,
        "input": [
            {"type": "text", "text": prompt},
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
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"질의 실패 (HTTP {e.code}): {e.read().decode('utf-8', 'replace')[:300]}") from None


def locate_subject(img, model: str = VISION_MODEL, timeout: int = 120) -> tuple | None:
    """그림을 보여주고 주인공이 차지하는 영역을 좌표로 받는다.

    로컬 검출(엣지·Haar·밝기·YOLO·원·윤곽)이 전부 실패해서 만들었다 —
    양식화된 캐릭터에는 픽셀 단서가 없지만, 그림을 이해하는 모델은 그냥 본다.
    배경 인물과 주인공도 구분한다.
    """
    return parse_box(extract_text(_ask(img, LOCATE_PROMPT, model, timeout)))


def _demo() -> None:
    """API 를 부르지 않고 요청 조립과 응답 파싱만 확인한다."""
    # 레퍼런스 없이 — 텍스트만
    req = build_request("숲을 걷는 사람", None)
    assert req["model"] == DEFAULT_MODEL
    assert len(req["input"]) == 1 and req["input"][0]["type"] == "text"
    assert req["response_format"]["aspect_ratio"] == "1:1"  # 캔버스가 정사각이라 크롭이 없다
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

    # 좌표 — 이름 붙은 필드가 정답. 배열 순서를 믿었다가 [y,x,y,x] 로 와서 꼬리가 틀어졌었다
    named = '설명 어쩌고\n{"box": {"x0": 0.3, "y0": 0.1, "x1": 0.6, "y1": 0.9}}'
    assert parse_box(named) == (0.3, 0.1, 0.6, 0.9)
    assert parse_box('{"box": [0.3, 0.1, 0.6, 0.9]}') == (0.3, 0.1, 0.6, 0.9)  # 예전 형식
    assert parse_box('{"box": null}') is None
    assert parse_box('{"box": {"x0": 0.6, "y0": 0.1, "x1": 0.3, "y1": 0.9}}') is None  # 뒤집힌 상자

    two = (
        '{"people": ['
        '{"label": "점원", "x0": 0.13, "y0": 0.37, "x1": 0.28, "y1": 0.68},'
        '{"label": "주인공", "x0": 0.42, "y0": 0.36, "x1": 0.63, "y1": 0.67}]}'
    )
    got = parse_people(two)
    assert set(got) == {"점원", "주인공"}
    # 점원은 왼쪽, 주인공은 오른쪽 — 축이 뒤바뀌면 이 비교가 깨진다
    assert got["점원"][0] < got["주인공"][0], "x/y 축이 뒤바뀌었습니다"
    assert parse_people('{"people": []}') == {}

    # 후보 밖 라벨을 후보 이름으로 맞춘다.
    # 실제로 시나리오는 `주인공` 인데 모델이 `손님` 이라 답했고, 꼬리가 빗나갔다.
    roles = ["점원", "주인공"]
    off = (
        '{"people": ['
        '{"label": "점원", "x0": 0.13, "y0": 0.37, "x1": 0.28, "y1": 0.68},'
        '{"label": "손님", "x0": 0.42, "y0": 0.36, "x1": 0.63, "y1": 0.67}]}'
    )
    fixed = parse_people(off, roles)
    assert set(fixed) == {"점원", "주인공"}, fixed
    assert fixed["점원"][0] == 0.13 and fixed["주인공"][0] == 0.42, fixed  # 상자가 뒤바뀌면 안 된다

    # 일치하는 것을 먼저 가져간다 — 순서가 어긋나 있어도 이름이 맞으면 그대로
    swapped = parse_people(
        '{"people": ['
        '{"label": "행인", "x0": 0.13, "y0": 0.37, "x1": 0.28, "y1": 0.68},'
        '{"label": "점원", "x0": 0.42, "y0": 0.36, "x1": 0.63, "y1": 0.67}]}',
        roles,
    )
    assert swapped["점원"][0] == 0.42, swapped  # 이름이 맞는 쪽이 먼저
    assert swapped["주인공"][0] == 0.13, swapped  # 남은 하나가 남은 후보로

    # 후보보다 많이 오면 남는 것을 배정하지 않는다 — 둘 중 누가 주인공인지 모른다.
    # 이름이 맞은 것만 남는다.
    crowd = parse_people(
        '{"people": ['
        '{"label": "점원", "x0": 0.1, "y0": 0.1, "x1": 0.2, "y1": 0.9},'
        '{"label": "손님", "x0": 0.3, "y0": 0.1, "x1": 0.4, "y1": 0.9},'
        '{"label": "행인", "x0": 0.5, "y0": 0.1, "x1": 0.6, "y1": 0.9}]}',
        roles,
    )
    assert set(crowd) == {"점원"}, crowd

    # 응답이 후보보다 적으면 배정하지 않는다 — 누가 빠졌는지 모르는데 붙이면
    # 꼬리가 엉뚱한 사람을 겨눈다. 비워두면 호출자가 주인공 상자로 폴백한다.
    lone = parse_people('{"people": [{"label": "손님", "x0": 0.4, "y0": 0.3, "x1": 0.6, "y1": 0.7}]}', roles)
    assert lone == {}, lone

    # 이름이 맞으면 하나만 와도 그대로 쓴다
    named_one = parse_people('{"people": [{"label": "점원", "x0": 0.1, "y0": 0.3, "x1": 0.2, "y1": 0.7}]}', roles)
    assert set(named_one) == {"점원"}, named_one

    # 후보를 안 주면 손대지 않는다 (기존 동작)
    assert set(parse_people(off)) == {"점원", "손님"}

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

    # 인물별 위치 — 화자와 매칭하려면 라벨이 함께 와야 한다
    multi = parse_people('{"people": [{"label":"주인공","box":[0.1,0.2,0.3,0.9]},'
                         '{"label":"점원","box":[0.6,0.2,0.8,0.9]}]}')
    assert set(multi) == {"주인공", "점원"}, multi
    assert multi["점원"][0] == 0.6
    assert parse_people('{"people": []}') == {}
    assert parse_people("사람이 없습니다") == {}
    # 상자가 망가진 항목은 버리고 나머지는 살린다
    partial = parse_people('{"people":[{"label":"A","box":[0.5,0.5,0.2,0.9]},{"label":"B","box":[0,0,1,1]}]}')
    assert set(partial) == {"B"}, partial

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
