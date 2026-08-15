# InstaCut

원고 텍스트를 인스타 컷툰으로 만든다. 설계 문서는 [instacut_prd.md](instacut_prd.md), 알려진 문제는 [ISSUES.md](ISSUES.md).

```
원고 → 컷 분할·번역 → 자리 계산 → 그림 생성 → 말풍선 합성 → 게시
```

## 왜 이렇게 만들었나

**한글은 이미지 모델에 맡기지 않는다.** 그림과 텍스트를 따로 만들어 합친다. 모델이 한글을 못 쓰기도 하지만, 텍스트를 고칠 때마다 GPU 를 돌릴 이유가 없다.

**말풍선 자리를 먼저 정하고 그림을 만든다.** 그림을 먼저 만들면 인물이 어디 서든 말풍선이 남는 자리로 밀려난다. 순서를 뒤집어야 한다.

**컷은 독립적으로 다시 만들 수 있다.** 5번 컷을 고칠 때 1~4번을 건드리지 않는다.

## 준비

```bash
comfy launch --background   # ComfyUI (SDXL 체크포인트 필요)
copy .env.example .env      # Gemini 백엔드를 쓸 때만 — 키를 채운다
```

LLM 은 `claude` CLI 를 헤드리스로 부른다. `ANTHROPIC_API_KEY` 는 필요 없다.
`GOOGLE_API_KEY` 는 `--backend gemini` 를 쓸 때만 필요하고, 없으면 로컬 경로로만 동작한다.

선택적으로 쓰는 모델:

| 용도 | 모델 | 없으면 |
|---|---|---|
| 인물 위치 강제 | ControlNet OpenPose SDXL | 프롬프트로만 지시 (잘 안 먹는다) |
| 캐릭터 스타일 고정 | IP-Adapter PLUS SDXL + CLIP Vision | 캐릭터 묘사 문자열로만 |

## 쓰는 법

```bash
# [1] 원고 → 컷 스크립트 + 화풍 번역 (LLM 1회)
uv run python -m instacut.cli new 원고.txt --cuts 4 --title "제목" --style "차분한 수채 느낌"

# [2] 번역 결과 확인 — 마음에 안 들면 project.json 의 _en 값을 직접 수정
uv run python -m instacut.cli show

# [3] 그림 생성 (텍스트 없음)
uv run python -m instacut.cli render

# [4] 말풍선 얹기
uv run python -m instacut.cli compose

# 특정 컷만 다시
uv run python -m instacut.cli regen 2      # 다른 그림 (GPU)
uv run python -m instacut.cli compose 3    # 텍스트만 (1초, GPU 안 씀)
```

### 캐릭터 모델 쓰기

`projects/<제목>/character_ref.png` 에 캐릭터 그림을 두면 그 캐릭터로 전 컷을 그린다.
텍스트 묘사만으로는 "이 캐릭터"를 재현할 수 없다 — `no nose` 라고 써도 모델이 코를 그린다.

```bash
# Nano Banana (권장) — 레퍼런스를 "이 인물"로 이해한다. 컷당 과금
uv run python -m instacut.cli render --backend gemini

# 로컬 (IP-Adapter) — 비용은 없지만 형태가 무너진다
uv run python -m instacut.cli render
```

**백엔드를 고르는 기준.** 로컬 IP-Adapter 는 레퍼런스를 "이미지"로 참조해서 형태와 배경이 한
다이얼에 묶인다 — 강하게 밀면 배경이 사라지고, 약하게 하면 캐릭터가 깨진다. Nano Banana 는
레퍼런스를 캐릭터로 이해하므로 그 트레이드오프가 없다. 캐릭터가 중요하면 `--backend gemini`.

```bash
$env:GOOGLE_API_KEY = "<키>"    # Google AI Studio 에서 발급, 결제 활성화 필요
uv run python -m instacut.cli render 2 --backend gemini   # 1컷만 먼저 시험
```

## 구조

```
instacut/
├── split.py     [1] 원고+화풍(한국어) → project.json   (claude CLI 1회)
├── pose.py          말풍선 자리 → 인물 위치 → 스틱 피규어
├── render.py    [3] project.json → raw/cut_NN.png      (ComfyUI foreach 1회)
├── head.py          얼굴 검출 (말풍선이 머리를 피하도록)
├── compose.py   [4] raw + 텍스트 → out/cut_NN.png       (Pillow)
└── cli.py       진입점

fragments/
├── sdxl_base_t2i.json       기본 t2i
├── sdxl_pose_t2i.json       + ControlNet (위치 강제)
├── sdxl_char_t2i.json       + ControlNet + IP-Adapter (8GB VRAM 에선 못 씀)
└── sdxl_charonly_t2i.json   + IP-Adapter (ControlNet 없이)

projects/<제목>/
├── project.json       컷 배열. 이걸 고치면 결과가 바뀐다
├── character_ref.png  (선택) 캐릭터 레퍼런스
├── raw/               텍스트 없는 그림. 절대 덮어쓰지 않는다
└── out/               말풍선 얹은 최종 컷 (1080x1350)
```

## 말풍선 규칙

| | 규칙 |
|---|---|
| **P-6** | 시선 흐름 — 첫 말풍선은 위, 다음은 아래+반대편. 그 사이를 그림이 채운다 |
| **P-7** | 인물의 머리와 겹치는 자리는 쓰지 않는다 (P-6 보다 우선) |
| 분할 | 문장 경계에서만. 문장 중간은 끊지 않는다 |
| 이어붙이기 | 조각을 겹쳐 그리고 꼬리는 한 조각에만 |
| 꼬리 | 말하는 인물을 향한다. 말풍선이 화면 중간 아래면 위로 붙는다 |
| 나레이션 | 그림 위 상단 박스. 인물이 그 자리를 비켜선다 |

## 다음에 할 일

각 이슈에는 **왜 지금 하지 않는지**도 적혀 있다. 빠뜨린 것이 아니라 미룬 것이다.

| | 이슈 | 왜 미뤘나 |
|---|---|---|
| [#1](https://github.com/yj78615-blip/instacut/issues/1) | 다중 화자 — `speaker` 를 인물 위치와 매칭해 꼬리 걸기 | 원고 세 편이 전부 주인공 독백이라 검증할 데이터가 없다 |
| [#2](https://github.com/yj78615-blip/instacut/issues/2) | 인물 위치 검출을 로컬 VLM(Florence-2)으로 | 지금은 Gemini 질의라 컷당 비용이 든다. 로컬로 옮기면 무료 |
| [#3](https://github.com/yj78615-blip/instacut/issues/3) | 캐릭터 LoRA 학습 | 8GB 에서 SDXL LoRA 학습이 되는지 미확인. 되면 캐릭터 비용이 초기 한 번으로 끝난다 |

**#2 와 #3 이 둘 다 되면 크레딧 없이 전체 파이프라인이 돈다.**

이미 부딪혀서 기록해둔 문제들은 [ISSUES.md](ISSUES.md) 에 있다 — VRAM 한계, 프롬프트로 구도가 안 잡히는 것, 인물 검출 6종이 왜 실패했는지 등.

## 자체 검사

```bash
uv run python -m instacut.split      # 파싱·검증·프롬프트 조립·자리 예약
uv run python -m instacut.compose    # 줄바꿈·폰트·말풍선 배치·꼬리 방향
uv run python -m instacut.head       # 얼굴 검출 폴백·겹침 판정
uv run python -m instacut.pose       # 인체 비율·인물 배치
```
