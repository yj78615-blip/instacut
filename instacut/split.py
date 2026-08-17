"""[1] 해석·번역 — 원고 + 화풍(한국어) → project.json

claude CLI를 헤드리스로 부른다. ANTHROPIC_API_KEY 없이 동작한다.
LLM 호출은 편당 1회. 컷 분할·화풍 변환·캐릭터 추출을 한 번에 처리한다 (PRD [1]).
"""

from __future__ import annotations

import json
import random
import re
import shutil
import subprocess
from pathlib import Path

# 전 컷 공통. 이미지 모델이 글자를 그리지 못하게 막는다 (PRD P-1)
NEGATIVE = (
    "text, letters, korean text, speech bubble, caption, "
    "watermark, signature, logo, ugly, deformed"
)

# 말풍선이 들어갈 자리를 **생성 전에** 비워두게 하는 지시 (PRD P-6).
#
# 그림을 먼저 만들고 빈 곳을 찾는 방식은 순서가 거꾸로다. 인물이 어디에 서느냐를
# 모델이 마음대로 정해버리면 말풍선은 남는 자리로 밀려날 수밖에 없다.
# 컷의 대사 개수를 세어, 실제로 쓸 자리를 비우도록 프롬프트에 미리 박는다.
RESERVE = {
    0: "",  # 나레이션만 있는 컷 — 그림 밖 띠를 쓰므로 구도가 자유롭다
    1: (
        "subject positioned on the right side of the frame, "
        "upper left quadrant completely empty, plain simple background there"
    ),
    2: (
        "subject positioned in the center of the frame, "
        "upper left quadrant and lower right quadrant completely empty, "
        "plain simple background in those two corners"
    ),
}
RESERVE_MANY = (
    "subject small and centered low in the frame, "
    "upper half and both side margins empty, plain simple background"
)

# 화자가 둘 이상인 컷 — 대화하는 인물들을 화면 가운데로 모은다.
# 한쪽에 몰리면 반대쪽이 텅 비고, 말풍선(위/아래)과 인물 사이도 멀어진다.
RESERVE_DIALOGUE = (
    "the two characters face each other in the middle of the frame, "
    "both centered horizontally and vertically, close together, "
    "upper area and lower area empty with plain simple background, "
    "no large empty space on either side"
)

# 인물이 프레임을 꽉 채우면 말풍선 놓을 자리가 없다. 전 컷에 공통으로 주입해 구도를 넓힌다.
# 합성 단계에서 그림을 축소하는 방법도 있지만, 그러면 주변에 채울 배경이 없어 흰 여백이 생긴다.
SUBJECT_SCALE = (
    "wide establishing shot, small distant figure, "
    "subject occupies about one third of the frame height, "
    "full body visible from head to feet, "
    "vast surrounding environment dominates the frame, "
    "camera far from the subject"
)
# `full upper body` 라고 썼더니 모델이 상반신 구도로 갔다. 전신을 보려면 head to feet 이라고 해야 한다.
# 여기서 더 줄이면 표정이 안 보여 컷툰으로 성립하지 않는다.

MAX_TEXT_LEN = 40  # 컷당 대사 총 길이 상한. 넘으면 말풍선이 그림을 덮는다

PROMPT = """너는 인스타 컷툰 제작 도구의 [1] 해석·번역 단계다.
원고를 컷으로 나누고, 화풍과 캐릭터를 이미지 생성 모델용 영어 프롬프트로 변환한다.

## 입력

원고:
```
{text}
```

컷 수: {n}컷
화풍(사용자가 한국어로 지정): {style}

## 변환 규칙

R-1. 인상어는 시각 요소로 분해한다. 화면에 그릴 수 없는 단어를 영어로 옮기지 않는다.
     "감성적인" → `emotional` (X, 아무 효과 없음)
     "감성적인" → `soft diffused lighting, warm color grading, muted palette` (O)
R-2. 인상어 하나를 선·색·빛·구도·질감 중 2~4개 축으로 푼다.
R-3. 원문에 없는 요소를 추가하지 않는다.
     "파스텔 톤" → `soft pastel palette, cherry blossom background` (X, 배경은 원문에 없다)
     품질 태그(8k, masterpiece, best quality)도 넣지 않는다.
R-4. 이미지 모델이 실제로 반응하는 관용 표현을 쓴다 (cel shading, flat colors, soft diffused lighting).
R-5. 결과는 쉼표로 구분된 명사구 나열. 문장으로 쓰지 않는다.

변환 예시:
  심플한 라인 드로잉 → clean minimal line art, thin uniform linework, flat colors, no shading
  파스텔 톤        → soft pastel palette, low saturation, muted colors, high key
  웹툰 느낌        → korean webtoon style, clean lineart, cel shading, digital coloring
  담백한          → restrained composition, limited color palette, generous white space
  낙서 같은        → loose sketchy linework, hand-drawn imperfect strokes, marker texture

## 컷 분할 규칙

- 정확히 {n}컷으로 나눈다.
- 마지막 컷은 마무리 또는 여운이 남는 훅 역할을 한다.
- 컷당 대사·나레이션 총 길이는 {maxlen}자를 넘지 않는다. 길면 쪼개거나 줄인다.
- **대사와 나레이션은 원고의 한국어를 그대로 쓴다. 절대 영어로 번역하지 않는다.**
- 장면 묘사(scene_en)만 영어로 쓴다. 인물의 자세·표정·행동·장소를 구체적으로.
- 화풍과 캐릭터 묘사는 scene_en에 넣지 않는다. 나중에 자동으로 앞에 붙는다.
- 말풍선 자리(왼쪽 여백) 지시도 넣지 않는다. 자동으로 붙는다.
- **각 scene_en에 샷 타입을 하나 넣는다** — medium shot / wide full-body shot /
  over-the-shoulder shot / establishing wide shot 중에서. 전 컷이 같은 샷이면 단조로워지니 섞는다.
  **얼굴이 프레임을 채우는 극단적 클로즈업은 쓰지 않는다** — 말풍선 놓을 자리가 없어진다.
  (모든 컷이 같은 시드를 쓰기 때문에, 구도 변화는 샷 타입이 만든다.)

## 캐릭터

원고를 읽고 주인공의 외모를 한국어로 정한 뒤 영어로 변환한다.
원고에 외모 단서가 없으면 원고 분위기에 맞게 자연스럽게 정한다.

**아래 항목을 모두 넣어야 한다.** 하나라도 빠지면 컷마다 다른 사람이 그려진다 —
특히 인종과 머리색이 빠지면 컷마다 인종이 바뀐다.

- 국적/인종 (한국 배경 원고면 Korean)
- 피부톤
- 머리: 색 + 길이 + 스타일 + 앞머리 유무
- 눈: 색 + 모양
- 얼굴형
- 복장: 상의 + 하의 (색까지)
- 액세서리 (없으면 no accessories)

예: `a Korean woman in her early 20s, fair skin, jet-black straight shoulder-length bob
with blunt bangs, dark brown almond eyes, small oval face, crisp white button-up shirt,
black slacks, no accessories`

## 출력

아래 JSON만 출력한다. 설명·코드펜스·인사말 금지.

{{
  "style": {{
    "art_style_ko": "사용자가 준 화풍 원문 그대로",
    "art_style_en": "변환 결과",
    "character_ko": "추출한 캐릭터 묘사(한국어)",
    "character_en": "변환 결과"
  }},
  "cuts": [
    {{
      "index": 1,
      "beat": "이 컷에서 벌어지는 일(한국어 한 줄)",
      "scene_en": "english scene description",
      "texts": [
        {{"type": "narration", "content": "한국어 나레이션"}},
        {{"type": "dialogue", "content": "한국어 대사", "speaker": "주인공"}}
      ]
    }}
  ]
}}

type은 narration(나레이션), dialogue(대사), thought(속마음) 중 하나다.
텍스트가 없는 컷은 "texts": [] 로 둔다.

**speaker — 누가 말하는가.** dialogue 와 thought 에는 반드시 넣는다.
- 주인공이 말하거나 생각하면 `"주인공"`
- 다른 인물이면 원고에 나온 대로 (`"선배"`, `"점원"`, `"엄마"` …)
- 나레이션은 화자가 없으므로 speaker 를 넣지 않는다

말풍선 꼬리가 이 사람을 향하게 되므로 틀리면 만화가 성립하지 않는다.
원고에서 누가 한 말인지 분명하지 않으면 `"주인공"` 으로 둔다.
"""


def _claude_bin() -> str:
    for name in ("claude.cmd", "claude"):
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError(
        "claude CLI를 찾을 수 없습니다. npm install -g @anthropic-ai/claude-code"
    )


def _ask_claude(prompt: str, timeout: int = 300) -> str:
    """claude CLI를 헤드리스로 호출한다. 프롬프트는 stdin으로 넘긴다."""
    proc = subprocess.run(
        [_claude_bin(), "-p"],
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if proc.returncode != 0:
        # 인증 만료 같은 메시지는 stdout 으로 나온다 — stderr 만 보면 빈 에러가 찍힌다
        detail = (proc.stderr or "").strip() or (proc.stdout or "").strip() or "(출력 없음)"
        hint = ""
        if "authenticate" in detail.lower() or "oauth" in detail.lower():
            hint = "\n→ 터미널에서 `claude` 를 한 번 실행해 로그인하세요."
        raise RuntimeError(f"claude 호출 실패 (exit {proc.returncode}): {detail[:400]}{hint}")
    return proc.stdout


def _extract_json(raw: str) -> dict:
    """응답에서 JSON 객체를 뽑는다. 코드펜스나 앞뒤 설명이 붙어 있어도 견딘다."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    candidate = fenced.group(1) if fenced else None

    if candidate is None:
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end <= start:
            raise ValueError(f"응답에서 JSON을 찾지 못했습니다:\n{raw[:500]}")
        candidate = raw[start : end + 1]

    return json.loads(candidate)


def validate(data: dict, n_cuts: int) -> list[str]:
    """PRD F-1 검증. 치명적이지 않은 문제는 경고로 돌려준다."""
    warnings: list[str] = []

    style = data.get("style", {})
    for key in ("art_style_ko", "art_style_en", "character_ko", "character_en"):
        if not style.get(key):
            raise ValueError(f"style.{key} 가 비어 있습니다")

    cuts = data.get("cuts", [])
    if len(cuts) != n_cuts:
        warnings.append(f"컷 수가 {n_cuts}가 아니라 {len(cuts)}개입니다")

    for cut in cuts:
        idx = cut.get("index", "?")
        if not cut.get("scene_en"):
            raise ValueError(f"{idx}번 컷에 scene_en 이 없습니다")

        total = sum(len(t.get("content", "")) for t in cut.get("texts", []))
        if total > MAX_TEXT_LEN:
            warnings.append(f"{idx}번 컷 대사가 {total}자입니다 (상한 {MAX_TEXT_LEN}자)")

        for t in cut.get("texts", []):
            # 대사·속마음에는 화자가 있어야 한다. 없으면 주인공으로 둔다 —
            # 말풍선 꼬리가 이 값을 보고 누구를 향할지 정한다.
            if t.get("type") in ("dialogue", "thought") and not (t.get("speaker") or "").strip():
                t["speaker"] = "주인공"
                warnings.append(f"{idx}번 컷 대사에 화자가 없어 '주인공'으로 뒀습니다: {t.get('content', '')[:14]}")

            content = t.get("content", "")
            # 대사가 영어로 번역돼 돌아오는 것이 이 단계의 대표적 실패다
            if content and not re.search(r"[가-힣]", content):
                warnings.append(f"{idx}번 컷 텍스트에 한글이 없습니다: {content!r}")

    return warnings


def build_project(data: dict, title: str, source_text: str, seed_base: int | None = None) -> dict:
    """LLM 응답을 project.json 구조로 채운다. seed·프롬프트 조립은 여기서."""
    rng = random.Random(seed_base)
    style = data["style"]

    # 편 전체가 같은 시드를 쓴다. 캐릭터 문자열만 고정해서는 컷마다 얼굴이 바뀌는데,
    # 시드까지 묶으면 같은 인물로 읽힌다(M0 비교 실험 결과).
    # 컷 하나만 다른 그림을 원하면 `instacut regen N` 이 그 컷의 시드만 바꾼다.
    shared_seed = rng.randrange(1, 2**31)

    cuts = []
    for i, cut in enumerate(data["cuts"], start=1):
        cuts.append(
            {
                "index": i,
                "beat": cut.get("beat", ""),
                "scene_en": cut["scene_en"],
                "final_prompt": None,  # [3]에서 조립되어 기록된다
                "negative_prompt": NEGATIVE,
                "seed": shared_seed,
                "balloon_zone": "left",
                "texts": cut.get("texts", []),
                "raw_image": f"raw/cut_{i:02d}.png",
                "out_image": f"out/cut_{i:02d}.png",
                "locked": False,
            }
        )

    return {
        "title": title,
        "source_text": source_text,
        "style": {
            "art_style_ko": style["art_style_ko"],
            "art_style_en": style["art_style_en"],
            "character_ko": style["character_ko"],
            "character_en": style["character_en"],
            "aspect_ratio": "4:5",  # 최종 캔버스. 그림 자체는 1080x1080 정사각 고정
        },
        "caption": "",
        "hashtags": [],
        "cuts": cuts,
    }


def reserve_hint(cut: dict) -> str:
    """말풍선이 몇 개 들어가는지, 화자가 몇 명인지 보고 구도 지시를 만든다."""
    balloons = [
        t for t in cut.get("texts", []) if t.get("type") != "narration" and (t.get("content") or "").strip()
    ]
    speakers = {(t.get("speaker") or "").strip() for t in balloons}
    speakers.discard("")

    # 대화 컷은 인물 배치가 다르다 — 마주 보고 가운데 모여야 한다
    if len(speakers) > 1:
        return RESERVE_DIALOGUE
    return RESERVE.get(len(balloons), RESERVE_MANY)


def assemble_prompt(project: dict, cut: dict) -> str:
    """컷 하나의 최종 프롬프트를 조립한다. 화풍·캐릭터·장면·크기·말풍선 자리 순서."""
    style = project["style"]
    parts = [
        style["art_style_en"],
        style["character_en"],
        cut["scene_en"],
        SUBJECT_SCALE,
        reserve_hint(cut),
    ]
    return ", ".join(p.strip() for p in parts if p and p.strip())


def split(text: str, n_cuts: int, style: str, title: str, seed_base: int | None = None) -> tuple[dict, list[str]]:
    prompt = PROMPT.format(text=text, n=n_cuts, style=style, maxlen=MAX_TEXT_LEN)
    data = _extract_json(_ask_claude(prompt))
    warnings = validate(data, n_cuts)
    return build_project(data, title, text, seed_base), warnings


def _demo() -> None:
    """LLM 없이 도는 자체 검사 — 파싱·검증·조립만 확인한다."""
    raw = """설명이 앞에 붙는 경우도 있다.
```json
{"style": {"art_style_ko": "심플한 라인",
           "art_style_en": "clean minimal line art, flat colors",
           "character_ko": "20대 여성, 단발",
           "character_en": "young woman in her 20s, short bob haircut"},
 "cuts": [{"index": 1, "beat": "지하철에서 긴장", "scene_en": "standing in a subway car",
           "texts": [{"type": "narration", "content": "첫 출근 날 아침."}]},
          {"index": 2, "beat": "사무실 도착", "scene_en": "in front of an office door",
           "texts": [{"type": "dialogue", "content": "Here we go."}]}]}
```"""
    data = _extract_json(raw)
    assert data["style"]["art_style_ko"] == "심플한 라인"

    warns = validate(data, 3)
    assert any("컷 수가 3가 아니라 2개" in w for w in warns), warns
    # 대사가 영어로 번역돼 돌아오는 실패를 잡아야 한다
    assert any("한글이 없습니다" in w for w in warns), warns

    # 화자 — 대사·속마음에는 반드시 있어야 하고, 없으면 주인공으로 채운다
    speaker_data = {
        "style": data["style"],
        "cuts": [
            {
                "index": 1,
                "beat": "x",
                "scene_en": "x",
                "texts": [
                    {"type": "dialogue", "content": "화자 없는 대사"},
                    {"type": "thought", "content": "속마음", "speaker": "선배"},
                    {"type": "narration", "content": "나레이션"},
                ],
            }
        ],
    }
    sw = validate(speaker_data, 1)
    texts = speaker_data["cuts"][0]["texts"]
    assert texts[0]["speaker"] == "주인공", "화자가 없는 대사를 채우지 않았습니다"
    assert texts[1]["speaker"] == "선배", "주어진 화자를 덮어썼습니다"
    assert "speaker" not in texts[2], "나레이션에 화자를 넣었습니다"
    assert any("화자가 없어" in w for w in sw), sw

    project = build_project(data, "테스트", "원고 원문", seed_base=42)
    assert len(project["cuts"]) == 2
    assert project["cuts"][0]["raw_image"] == "raw/cut_01.png"
    # 캐릭터 일관성의 핵심 — 편 전체가 같은 시드를 공유해야 한다
    assert project["cuts"][0]["seed"] == project["cuts"][1]["seed"]
    assert project["cuts"][0]["balloon_zone"] == "left"

    prompt = assemble_prompt(project, project["cuts"][0])
    assert prompt.startswith("clean minimal line art")
    assert "young woman in her 20s" in prompt
    assert "standing in a subway car" in prompt
    # 인물이 프레임을 꽉 채우지 않도록 하는 지시가 전 컷에 들어가야 한다
    assert "subject occupies about one third of the frame height" in prompt

    # 말풍선 자리를 생성 전에 예약한다 — 대사 개수에 따라 비울 자리가 달라진다
    assert reserve_hint({"texts": [{"type": "narration", "content": "나레이션"}]}) == ""
    one = reserve_hint({"texts": [{"type": "dialogue", "content": "하나"}]})
    two = reserve_hint({"texts": [{"type": "dialogue", "content": "하나"}, {"type": "thought", "content": "둘"}]})
    assert "upper left quadrant completely empty" in one
    assert "upper left quadrant and lower right quadrant" in two, two
    assert one != two  # 개수가 다르면 비울 자리도 달라야 한다

    # 화자가 둘이면 대화 구도 — 인물들을 가운데로 모은다
    talk = reserve_hint(
        {
            "texts": [
                {"type": "dialogue", "content": "봉투 필요하세요?", "speaker": "점원"},
                {"type": "dialogue", "content": "아니요.", "speaker": "주인공"},
            ]
        }
    )
    assert "face each other in the middle" in talk, talk
    assert talk != two, "화자가 둘인데 단일 인물 구도를 씁니다"
    # 같은 사람이 두 번 말하면 대화가 아니다
    solo_twice = reserve_hint(
        {
            "texts": [
                {"type": "dialogue", "content": "가야지.", "speaker": "주인공"},
                {"type": "thought", "content": "늦었네.", "speaker": "주인공"},
            ]
        }
    )
    assert solo_twice == two, "화자가 한 명인데 대화 구도를 씁니다"

    # 대사 1개짜리 컷의 프롬프트에는 왼쪽 위를 비우라는 지시가 실제로 들어간다
    cut_one = dict(project["cuts"][0], texts=[{"type": "dialogue", "content": "대사"}])
    assert "upper left quadrant completely empty" in assemble_prompt(project, cut_one)

    # 원고에 없는 화풍이 조립 단계에서 새로 끼어들면 안 된다
    assert "masterpiece" not in prompt and "8k" not in prompt

    # 시드는 seed_base로 재현 가능해야 한다 (같은 입력 → 같은 그림)
    again = build_project(data, "테스트", "원고 원문", seed_base=42)
    assert [c["seed"] for c in again["cuts"]] == [c["seed"] for c in project["cuts"]]

    print("split.py 자체 검사 통과")


if __name__ == "__main__":
    _demo()
