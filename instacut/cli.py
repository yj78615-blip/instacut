"""InstaCut CLI — 원고 텍스트 → 인스타 컷툰

  instacut new 원고.txt --cuts 8 --title "제목" --style "심플한 라인 드로잉, 파스텔 톤"
  instacut render          # [3] 그림만 생성
  instacut compose         # [4] 말풍선 얹기
  instacut regen 5         # 5번 컷만 다시 (그림 + 말풍선)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECTS = ROOT / "projects"


def _slug(title: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]', "", title).strip().replace(" ", "_")
    return s or "untitled"


def _load(name: str | None) -> tuple[Path, dict]:
    """프로젝트를 연다. 이름을 안 주면 가장 최근에 손댄 것."""
    if name:
        d = PROJECTS / name
        if not d.exists():
            sys.exit(f"프로젝트가 없습니다: {d}")
    else:
        dirs = [p for p in PROJECTS.glob("*") if (p / "project.json").exists()]
        if not dirs:
            sys.exit("프로젝트가 없습니다. 먼저 `instacut new` 를 실행하세요.")
        d = max(dirs, key=lambda p: (p / "project.json").stat().st_mtime)

    # utf-8-sig: 사용자가 메모장 등으로 고치면 BOM 이 붙는데, 그걸로 깨지면 안 된다
    return d, json.loads((d / "project.json").read_text(encoding="utf-8-sig"))


def _save(d: Path, project: dict) -> None:
    (d / "project.json").write_text(
        json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def show_translation(project: dict) -> None:
    """[2] 검토 — 한국어/영어 대조 출력 (PRD F-2b)."""
    s = project["style"]
    print()
    print("  화풍")
    print(f"    한국어  {s['art_style_ko']}")
    print(f"    영어    {s['art_style_en']}")
    print()
    print("  캐릭터  (원고에서 추출)")
    print(f"    한국어  {s['character_ko']}")
    print(f"    영어    {s['character_en']}")
    print()
    print("  컷")
    for c in project["cuts"]:
        texts = " / ".join(t.get("content", "") for t in c["texts"]) or "(텍스트 없음)"
        print(f"    {c['index']:2d}. {c['beat']}")
        print(f"        {texts}")
    print()


def cmd_new(args) -> None:
    from .split import split

    text = Path(args.script).read_text(encoding="utf-8").strip()
    if not text:
        sys.exit("원고가 비어 있습니다")

    title = args.title or Path(args.script).stem
    print(f"[1] 해석·번역 중... (원고 {len(text)}자 → {args.cuts}컷)")

    project, warnings = split(text, args.cuts, args.style, title, seed_base=args.seed)

    d = PROJECTS / _slug(title)
    d.mkdir(parents=True, exist_ok=True)
    _save(d, project)

    show_translation(project)
    for w in warnings:
        print(f"  ⚠ {w}")

    print(f"프로젝트: {d}")
    print("project.json 을 확인·수정한 뒤 `instacut render` 를 실행하세요.")


def cmd_render(args) -> None:
    from .render import render

    d, project = _load(args.project)

    if args.backend == "gemini" and not args.yes:
        # 컷당 과금된다. 실수로 수십 컷을 돌리지 않게 한 번 확인한다
        n = 1 if args.cut else len([c for c in project["cuts"] if not c["locked"]])
        print(f"Gemini(Nano Banana)로 {n}컷을 생성합니다. 컷당 비용이 발생합니다.")
        if input("진행할까요? [y/N] ").strip().lower() not in ("y", "yes"):
            sys.exit("취소했습니다")

    made = render(ROOT, d, project, only=args.cut, backend=args.backend)
    _save(d, project)  # final_prompt 기록
    if made:
        print(f"\n{len(made)}컷 생성 완료 → {d / 'raw'}")
        print("그림을 확인한 뒤 `instacut compose` 로 말풍선을 얹으세요.")


def cmd_compose(args) -> None:
    from .compose import compose_project

    d, project = _load(args.project)
    made = compose_project(d, project, only=args.cut)
    if made:
        print(f"\n{len(made)}컷 합성 완료 → {d / 'out'}")


def cmd_regen(args) -> None:
    from .compose import compose_project
    from .render import render

    d, project = _load(args.project)
    cut = next((c for c in project["cuts"] if c["index"] == args.cut), None)
    if cut is None:
        sys.exit(f"{args.cut}번 컷이 없습니다")

    if args.seed is not None:
        cut["seed"] = args.seed
    else:
        cut["seed"] = (cut["seed"] * 1103515245 + 12345) % (2**31)  # 다른 그림을 뽑는다

    render(ROOT, d, project, only=args.cut)
    _save(d, project)
    compose_project(d, project, only=args.cut)


def cmd_show(args) -> None:
    d, project = _load(args.project)
    print(f"프로젝트: {d}")
    show_translation(project)


def main() -> None:
    p = argparse.ArgumentParser(prog="instacut", description="텍스트 → 인스타 컷툰")
    sub = p.add_subparsers(dest="cmd", required=True)

    n = sub.add_parser("new", help="[1] 원고 → 컷 스크립트")
    n.add_argument("script", help="원고 텍스트 파일")
    n.add_argument("--cuts", type=int, default=8, help="컷 수 (기본 8)")
    n.add_argument("--title", help="제목 (기본: 파일명)")
    n.add_argument("--style", required=True, help='화풍을 한국어로. 예: "심플한 라인 드로잉, 파스텔 톤"')
    n.add_argument("--seed", type=int, help="시드 기준값 (같은 값이면 같은 그림)")
    n.set_defaults(func=cmd_new)

    r = sub.add_parser("render", help="[3] 그림 생성 (텍스트 없음)")
    r.add_argument("cut", nargs="?", type=int, help="특정 컷만")
    r.add_argument("--project")
    r.add_argument(
        "--backend",
        choices=("comfy", "gemini"),
        default="comfy",
        help="comfy=로컬 ComfyUI (기본) / gemini=Nano Banana API (컷당 과금, 캐릭터 일관성 강함)",
    )
    r.add_argument("--yes", action="store_true", help="비용 확인을 건너뛴다")
    r.set_defaults(func=cmd_render)

    c = sub.add_parser("compose", help="[4] 말풍선 얹기")
    c.add_argument("cut", nargs="?", type=int, help="특정 컷만")
    c.add_argument("--project")
    c.set_defaults(func=cmd_compose)

    g = sub.add_parser("regen", help="컷 하나를 다른 그림으로 (그림 + 말풍선)")
    g.add_argument("cut", type=int)
    g.add_argument("--seed", type=int)
    g.add_argument("--project")
    g.set_defaults(func=cmd_regen)

    s = sub.add_parser("show", help="[2] 번역 결과·컷 스크립트 확인")
    s.add_argument("--project")
    s.set_defaults(func=cmd_show)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
