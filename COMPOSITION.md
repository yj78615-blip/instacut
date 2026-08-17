# 화면 구성 — 카메라 그리드와 인물 배치

컷을 만들 때 "인물을 어디에 둘 것인가"를 지시하는 언어를 정리한다.

결론부터: **어휘는 모델에 따라 갈린다.** 샷 사이즈·앵글 같은 촬영 용어는 어디서나 통하고,
영역을 지정하는 공간 지시는 언어를 이해하는 모델(Nano Banana)에서만 통한다.
로컬 SDXL 에서는 안 통하며, 그쪽은 ControlNet 으로 위치를 강제해야 한다.

이 문서가 필요해진 이유는 "그림 가운데에 캐릭터를 둬달라"는 지시를 도구가 알아듣지 못했기 때문이다.
사람에게는 한 마디면 되는 말이 이미지 모델에게는 왜 안 통하는지, 대신 무엇을 써야 하는지를 적는다.

---

## 1. 그리드 — 화면을 나누는 두 가지 방식

### 삼분할(rule of thirds)

가로세로를 3등분해 9칸을 만들고, **선과 교차점**에 중요한 것을 놓는다.
사진·영화·만화가 공유하는 가장 오래된 구도 규칙이고, **이미지 모델이 이름을 안다**.

```
┌───────┬───────┬───────┐
│       │       │       │   ← 상단 1/3 : 하늘·배경·말풍선 자리
├───────┼───────┼───────┤
│       │  ●    │       │   ← 중앙 밴드 : 인물의 얼굴이 오는 높이
├───────┼───────┼───────┤
│       │       │       │   ← 하단 1/3 : 바닥·발밑·말풍선 자리
└───────┴───────┴───────┘
      좌       중       우
```

교차점 네 곳(●이 오는 자리)이 "강한 위치"다. 정확히 한가운데는 오히려 정적이라
사진에서는 피하지만, **컷툰에서는 다르다** — 아래 4절 참고.

### 사분면(quadrant)

화면을 넷으로 나눠 좌상·우상·좌하·우하로 부른다. 우리 말풍선 자리(`ZONES`)가 이 체계다.

**주의: 이미지 모델에게는 이 말이 안 통한다.** 우리가 직접 확인했고(ISSUES.md),
외부 실험에서도 픽셀 좌표나 사분면 번호로 배치를 지정한 사례는 나오지 않는다.
사분면은 **합성 단계(Pillow)에서 쓰는 우리 내부 언어**로 남겨야 한다.

---

## 2. 샷 사이즈 — 인물이 프레임을 얼마나 차지하나

거리 어휘는 확립돼 있고 모델이 잘 따른다. 우리 `SUBJECT_SCALE` 이 여기 해당한다.

| 이름 | 인물이 차지하는 높이 | 컷툰에서 |
|---|---|---|
| Extreme close-up | 눈·입만 | 감정 강조 컷. 말풍선 자리 없음 |
| Close-up | 얼굴 | 말풍선 하나가 겨우 |
| Medium shot | 허리 위 | 대화 컷의 기본 |
| Cowboy shot | 허벅지 중간 | 서부극에서 권총집이 보이는 높이 |
| **Medium-full** | 무릎 위 | **인물 둘이 마주 보는 컷에 적합** |
| Full shot | 전신 (약 1/2~2/3) | 동작이 보이는 컷 |
| **Wide / Establishing** | 전신 (약 1/3 이하) | **말풍선 자리가 가장 넉넉하다. 현재 기본값** |
| Extreme wide | 점처럼 작음 | 표정이 안 보여 컷툰으로 성립 안 함 |

`full upper body` 라고 썼더니 상반신 구도로 갔다. 전신을 원하면 `full body visible from head to feet`
라고 명시해야 한다 — 이미 코드 주석에 있는 내용이다.

---

## 3. 앵글과 여백 — 나머지 어휘

**앵글** (모델이 잘 따름)

| 표현 | 효과 |
|---|---|
| `eye-level shot` | 중립. 일상툰의 기본 |
| `low angle shot` | 인물이 커 보임. 위쪽 여백이 좁아진다 |
| `high angle shot` | 인물이 작아 보임. **위쪽 여백이 늘어 말풍선에 유리** |
| `top-down` | 바닥이 배경이 됨. 인물 실루엣만 보인다 |

**여백** — 세 가지 이름이 있고, 셋 다 말풍선 자리와 직결된다.

- **헤드룸(headroom)** — 정수리 위 공간. 좁으면 위쪽 말풍선이 머리를 덮는다
- **노즈룸(nose room / looking room)** — 인물이 **바라보는** 방향의 공간
- **리드룸(lead room)** — 인물이 **움직이는** 방향의 공간. 정지한 인물에는 노즈룸을 쓴다
- **세이프 영역(safe area)** — 잘려도 되는 가장자리. 생성 비율과 캔버스가 다르면 그만큼 잘린다

우리 캔버스는 **1080×1080 정사각**이고 생성도 1:1(Gemini) / 1024×1024(SDXL)라
**잘리는 부분이 없다**(`fit_to_canvas`). 예전에 3:4로 생성해 4:5 캔버스에 맞추던 시절에는
위아래가 2.7% 잘렸는데, 비율을 맞추면서 사라진 문제다.

비율이 어긋나는 그림을 넣으면 `fit_to_canvas` 가 중앙 기준으로 자른다 — 그때는
인물의 정수리와 발끝을 프레임 끝에 붙이지 말아야 한다.

---

## 4. 컷툰은 사진과 반대다

사진 구도는 "정중앙을 피하라"고 가르친다. 컷툰에서는 그 조언을 **뒤집어야** 한다.

| | 사진·영화 | 인스타 컷툰 |
|---|---|---|
| 인물 위치 | 삼분할 교차점 | **중앙 밴드** (세로 1/3~2/3) |
| 이유 | 정중앙은 정적 | 위·아래를 말풍선이 먹는다 |
| 여백 | 시선 방향에 | 위·아래에 |
| 화면 비 | 가로가 흔함 | 1:1 정사각 고정 |

**핵심: 말풍선이 상단과 하단을 쓰므로 인물은 세로 중앙 밴드에 있어야 한다.**
좌우는 삼분할을 따라도 되지만, 대화 컷은 두 인물이 가운데로 모여야 말풍선과 인물의 거리가 짧아진다.

시선 흐름(P-6, 역S자)까지 겹치면 이렇게 된다.

```
┌───────────────────────┐
│ ①말풍선               │  ← 상단: 첫 말풍선
│                       │
│      ②인물 ← ← ← ←    │  ← 중앙 밴드: 인물. 시선이 여기를 지난다
│                       │
│               ③말풍선 │  ← 하단: 둘째 말풍선 (반대편)
└───────────────────────┘
```

---

## 5. 무엇이 통하고 무엇이 안 통하나

우리 실험(ISSUES.md)과 외부 실험을 합친 결과다.

### 통하는 것

| 유형 | 예시 | 근거 |
|---|---|---|
| **이름 있는 구도 규칙** | `rule of thirds composition` | 외부 실험에서 세 피사체가 정확히 삼분할을 따랐다 |
| **강제 어법** | `subject MUST be positioned according to the rule of thirds, both horizontally and vertically` | 대문자 MUST 를 붙이면 준수율이 오른다 |
| **샷 사이즈** | `wide establishing shot`, `medium-full shot` | 확립된 촬영 용어 |
| **크기 비율** | `subject occupies about one third of the frame height` | 우리 실험에서 성공 |
| **중앙 지정** | `center-framed`, `centered horizontally` | Google 공식 예시가 `Medium-full shot, center-framed` 형태를 쓴다 |
| **앵글** | `eye-level shot`, `high angle` | 확립된 용어 |

### 모델에 따라 갈리는 것 — 영역 지정

**여기서 백엔드를 구분해야 한다.** 같은 지시가 SDXL 에서는 실패하고 Nano Banana 에서는 성공한다.

| 유형 | 예시 | 로컬 SDXL | Nano Banana |
|---|---|---|---|
| 사분면 비우기 | `upper left quadrant completely empty` | 실패 | **성공** |
| 좌우 배치 | `subject positioned on the right side of the frame` | 실패 | **성공** |
| 영역 비우기 | `leave the left third empty` | 실패 | 미검증 |
| 중앙 배치 | `centered horizontally and vertically` | 부분 | **성공** |

근거는 편의점 4컷이다. 컷3·컷4에 `subject positioned on the right side of the frame,
upper left quadrant completely empty` 가 들어갔고, 결과는 인물이 오른쪽에 서고 좌상단이
비었다. 컷2의 `centered` 도 두 인물을 가운데로 모았다.

벤치마크도 같은 방향을 가리킨다. GenSpace(2025)에서 확산 전문 모델과 통합 모델의 격차가
크다 — SDXL 은 ELO 841, FLUX.1-dev 는 1046, 반면 통합 모델인 GPT-4o(53.22)와
Gemini-2.0-Flash(56.44)가 상위다. 저자들은 그 이유를 **"이미지·텍스트 입력을 통합해 얻은
일반적 인지 능력"** 으로 설명한다. GenEval 에서 SDXL 의 position 항목 점수는 0.15 다.

SDXL 에서 실패한 기록([ISSUES.md](ISSUES.md) 「프롬프트로는 구도가 제어되지 않는다」)은
그대로 유효하다 — **그 표는 SDXL 에 대한 것이지 모든 모델에 대한 것이 아니다.**
언어를 이해하는 모델과 CLIP 임베딩으로 조건을 거는 모델은 공간 지시에서 갈린다.

### 여전히 안 통하는 것 — 어느 모델에서도

| 유형 | 예시 | 근거 |
|---|---|---|
| **픽셀 좌표** | `place the subject at (300, 800)` | 시도된 사례 자체가 없다 |
| **대상 기준 좌/우** | `the character's left hand`, `left eye socket` | GPT-4o 21.21% (아래) |
| **수치로 준 거리·크기** | `the subject is 2 meters from the camera` | GPT-4o 30~41% |

**자기중심(egocentric) 대 타자중심(allocentric)** — 이 구분이 결정적이다. GenSpace 측정에서
같은 모델이 자기중심 지시는 **94.55%**, 타자중심 지시는 **21.21%** 로 갈렸다.

- **자기중심** = 보는 사람 기준. `left side of the frame`, `center of the frame` → 잘 통한다
- **타자중심** = 대상 기준. `the car's right door`, `the character's left hand` → 좌우가 뒤집힌다

저자들의 분석은 **"모델이 객체를 시청자 쪽으로 향하게 그리는 경향이 있어 시청자 기준 좌우가
반전된다"** 는 것이다. 우리 프롬프트는 전부 프레임 기준(`left side of the frame`)이므로 안전하다.
인물의 손·눈 같은 신체 좌우를 지시할 일이 생기면 뒤집힐 것을 각오해야 한다.

수치 지시도 약하다. `one third of the frame height` 가 우리 실험에서 통한 것은 사실이지만,
`wide establishing shot` 이라는 샷 사이즈 어휘와 함께 들어갔다. **수치 단독으로는 신뢰하지 말 것** —
GenSpace 에서 객체 크기 30.47%, 객체 거리 41.33%, 카메라 거리 35.19% 로 절반을 밑돈다.

### 프롬프트 조립 순서

Google 문서가 제시하는 기본 형태다. 우리 `assemble_prompt()` 도 같은 순서다.

```
[Subject] + [Action] + [Location/context] + [Composition] + [Style]
```

구도 어휘를 너무 많이 쌓으면 서로 충돌한다는 조언이 있으나(2~3개 권장), 이는 블로그
수준의 경험담이고 우리가 검증하지 않았다. 현재 `RESERVE_DIALOGUE` 는 네 가지를 쌓고도
작동한다.

---

## 6. 왜 모델에 따라 갈리나

확산 모델이 공간 지시를 못 듣는 이유는 연구로 정리돼 있고, 셋 다 우리 파이프라인의
설계 결정과 직결된다.

### (1) 텍스트 인코더가 공간 관계를 보존하지 않는다

CLIP 텍스트 인코더는 이미지-텍스트 대조 학습만 받고 **공간에 대한 명시적 감독이 없어서**,
인코딩 단계에서 공간 관계가 거의 완전히 소실된다. CLIP 이 사실상 bag-of-words 라는
비판과 같은 맥락이다. 더 큰 인코더도 마찬가지여서, **T5-XXL(11B)조차 논리적으로 동등한
공간 표현을 95% 이상 구분하지 못한다.**

SDXL 은 CLIP 두 개를 텍스트 인코더로 쓴다. "왼쪽 위를 비워라" 가 인코딩되는 순간
"왼쪽·위·비어있음" 이라는 단어 뭉치로 뭉개진다.

### (2) 레이아웃은 텍스트가 아니라 노이즈가 정한다

더 근본적인 문제다. 확산 과정에서 **초기 노이즈가 공간 배치를 강하게 결정하고, 텍스트
프롬프트는 그 자리에 무엇이 올지를 정할 뿐**이다. 학습이 진행될수록 이 의존이 강해지는데,
배치를 노이즈에 담는 편이 텍스트에서 배우는 것보다 쉽기 때문이다.

**이것이 ControlNet 이 즉시 작동한 이유다.** 스틱 피규어는 배치를 텍스트가 아니라 조건으로
직접 준다 — 노이즈가 정하던 자리를 빼앗는 것이다. 프롬프트로 백 번 말하는 것보다
포즈 이미지 한 장이 확실한 이유가 여기 있다.

### (3) 학습 데이터에 공간 관계가 적고 편향돼 있다

대규모 이미지-텍스트 데이터셋에는 공간 관계를 서술한 캡션이 드물고, 있는 것도 편향돼 있다.
"사람이 문 앞에 서 있다" 는 캡션은 흔해도 "화면 왼쪽 위가 비어 있다" 는 캡션은 없다.

### 통합 모델은 왜 다른가

Gemini 같은 통합(unified) 모델은 이미지와 텍스트 입력을 한 모델에서 다루고, GenSpace
저자들은 그 결과로 얻은 **일반적 인지 능력**이 공간 성능 차이를 만든다고 본다. 프롬프트를
"조건 벡터" 로 압축하는 게 아니라 문장으로 읽는 쪽에 가깝다.

다만 통합 모델도 만능은 아니다 — 위에서 봤듯 타자중심 좌우와 수치 지시에서는 똑같이 무너진다.

---

## 7. 컷툰 실무 관행

한국 웹툰 제작 가이드가 말하는 것 중 우리 규칙과 겹치는 부분이다.

- **읽기 방향** — 한국은 왼쪽에서 오른쪽, 위에서 아래. 말풍선을 이 순서로 놓아야 대사 순서가
  읽힌다. 우리 P-6(왼쪽 위 우선)이 여기서 나온다
- **말풍선 여백** — 말풍선 주변에 여백을 둬야 답답하지 않고 가독성이 올라간다
- **시선 흐름** — 인물 배치 순서와 말풍선 배치가 함께 연출을 만든다

**단, 세로 스크롤 웹툰의 관행을 그대로 가져오면 안 된다.** 웹툰은 컷 사이 거터(빈 공간)로
템포를 만들고 독자가 한 번에 한두 컷만 본다. 인스타 캐러셀은 **정사각 카드 한 장이 완결**이고
스와이프로 넘어간다 — 거터가 없고, 카드 안에서 시선 흐름이 끝나야 한다.

---

## 8. 현재 코드에 대한 진단

`split.py` 의 구도 지시를 위 기준으로 보면 이렇다.

| 상수 | 지금 내용 | Nano Banana | 로컬 SDXL |
|---|---|---|---|
| `SUBJECT_SCALE` | `wide establishing shot`, `one third of the frame height`, `head to feet` | 유효 | 유효 |
| `RESERVE[1]` | `subject on the right side`, `upper left quadrant empty` | **유효** (컷3·컷4 확인) | 무효 |
| `RESERVE[2]` | 두 사분면 비우기 | 미검증 | 무효 |
| `RESERVE_MANY` | `upper half and both side margins empty` | 미검증 | 무효 |
| `RESERVE_DIALOGUE` | `centered horizontally and vertically`, `close together` | **유효** (컷2 확인) | 부분 |

**Nano Banana 백엔드에서는 자리 예약이 작동한다.** 편의점 4컷이 근거다.
로컬 SDXL 경로에서는 여전히 안 듣고, 그쪽은 ControlNet 스틱 피규어로 위치를 강제하는
편이 확실하다(`pose.py`).

그리고 어느 쪽이든 합성 단계가 인물 위치를 **보고 나서** 말풍선을 놓으므로(P-7, `subject_box`)
생성이 빗나가도 수습된다. 다만 인물이 프레임 끝에 붙으면 수습할 여지가 좁아진다.

### 남은 개선 여지

지금 지시는 컷툰의 세로 배치를 다루지 않는다. `RESERVE[1]` 은 좌우만 말하고, 인물이
세로로 어디 서는지는 방치한다. 4절에서 정리한 "중앙 밴드" 를 명시하면 위·아래 말풍선
자리가 함께 확보된다.

```python
RESERVE_CENTER = (
    "subject center-framed, "
    "subject MUST be positioned on the middle horizontal third of the frame, "
    "generous headroom above and floor space below"
)
```

미검증안이다. 적용하려면 컷 하나로 시험해야 한다.

---

## 9. 용어 대응표

우리 코드의 내부 언어와 프롬프트 언어를 나란히 둔다. **모두 프레임 기준(자기중심)이다** —
대상 기준 좌우는 쓰지 않는다.

| 우리 코드 | 프롬프트 언어 | 비고 |
|---|---|---|
| `ZONES` 좌상/우상/좌하/우하 | — | 합성 전용. 프롬프트에 쓰지 말 것 |
| `subject_box` (0~1 비율) | — | 검출 결과. 프롬프트에 쓰지 말 것 |
| 인물이 세로 가운데 | `middle horizontal third`, `center-framed` | |
| 인물이 작게 | `wide shot` + `one third of the frame height` | 수치 단독은 약하다. 샷 사이즈와 함께 |
| 인물이 크게 | `medium shot`, `close-up` | |
| 머리 위 여백 | `generous headroom` | |
| 두 인물이 마주 봄 | `two characters facing each other, center-framed` | |
| 눈높이 | `eye-level shot` | |
| 인물의 왼손·오른쪽 얼굴 | — | **쓰지 말 것.** 타자중심이라 뒤집힌다 |

---

## 출처

**왜 갈리는가 (6절)**

- [GenSpace: Benchmarking Spatially-Aware Image Generation](https://arxiv.org/html/2505.24870) — 자기중심 94.55% 대 타자중심 21.21%, 모델별 ELO, 통합 모델 우위의 원인
- [CoMPaSS: Enhancing Spatial Understanding in Text-to-Image Diffusion Models (ICCV 2025)](https://arxiv.org/html/2412.13195v1) — CLIP·T5 텍스트 인코더가 공간 관계를 보존하지 못한다
- [Demystifying Numerosity in Diffusion Models](https://arxiv.org/html/2510.11117v1) — 레이아웃은 노이즈가 정하고 텍스트는 무엇이 올지만 정한다
- [GenEval 2: Addressing Benchmark Drift in Text-to-Image Evaluation](https://arxiv.org/html/2512.16853v1) — SDXL position 0.15

**프롬프트 어휘 (2·3·5절)**

- [Nano Banana 프롬프트 가이드 — Google DeepMind](https://deepmind.google/models/gemini-image/prompt-guide/)
- [Ultimate prompting guide for Nano Banana — Google Cloud Blog](https://cloud.google.com/blog/products/ai-machine-learning/ultimate-prompting-guide-for-nano-banana) — `[Subject] + [Action] + [Location] + [Composition] + [Style]`
- [Nano Banana can be prompt engineered for extremely nuanced AI image generation — Max Woolf](https://minimaxir.com/2025/11/nano-banana-prompts/) — `MUST ... rule of thirds` 강제 어법
- [Prompting tips for Nano Banana Pro — Google Blog](https://blog.google/products-and-platforms/products/gemini/prompting-tips-nano-banana-pro/)

**컷툰 실무 (7절)**

- [만화 웹툰 콘티 작법 — 시선의 흐름과 웹툰 스크롤 방식 편집 (한국콘텐츠진흥원)](https://edu.kocca.kr/edu/onlineEdu/openLecture/view.do?pSeq=470&menuNo=500085)
- [만화 구성 — 컷 분할 방법 (CLIP STUDIO)](https://www.clipstudio.net/drawing/archives/161626)
- [효과적인 웹툰 내 말풍선 디자인](https://www.jaenung.net/tree/27870)
- [웹툰 컷 구성 가이드: 몰입을 부르는 세로 스크롤](https://comistitch.com/ko/blog/webtoon-vertical-scroll-paneling-guide/)

**우리 실험**

- [ISSUES.md](ISSUES.md) 「프롬프트로는 구도가 제어되지 않는다」 — **SDXL 백엔드 기준**
- `projects/편의점/` 4컷 — Nano Banana 백엔드에서 자리 예약이 작동한 근거
