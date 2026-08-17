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


def _print_lines(cut: dict) -> None:
    """컷 하나의 대사를 번호와 함께 보여준다 — edit 의 --line 이 이 번호다."""
    from .compose import ZONES

    print(f"\n  컷 {cut['index']}  {cut['beat']}")
    if not cut["texts"]:
        print("    (텍스트 없음)")
    for i, t in enumerate(cut["texts"], start=1):
        who = t.get("speaker") or "—"
        marks = []
        if t.get("pos"):
            marks.append(f"자리={t['pos']}")
        if t.get("tail"):
            marks.append(f"꼬리→{t['tail']}")
        tail_note = f"  [{' '.join(marks)}]" if marks else ""
        print(f"    {i}. ({t.get('type', 'dialogue')}/{who}) {t.get('content', '')}{tail_note}")
    print(f"\n    자리 후보: {', '.join(ZONES)}")


def cmd_edit(args) -> None:
    """말풍선·꼬리·대사를 고친다. 그림은 건드리지 않으므로 GPU·API 비용이 없다."""
    from .compose import ZONES, compose_project

    d, project = _load(args.project)
    cut = next((c for c in project["cuts"] if c["index"] == args.cut), None)
    if cut is None:
        sys.exit(f"{args.cut}번 컷이 없습니다")

    # 인자 없이 부르면 지금 상태만 보여준다 — 무엇을 고칠지 정하는 화면
    if args.line is None:
        _print_lines(cut)
        return

    if not 1 <= args.line <= len(cut["texts"]):
        sys.exit(f"{args.line}번 대사가 없습니다 (1~{len(cut['texts'])})")
    t = cut["texts"][args.line - 1]

    changed = []
    if args.text is not None:
        t["content"] = args.text
        changed.append("대사")
    if args.type is not None:
        t["type"] = args.type
        changed.append("종류")
    if args.speaker is not None:
        t["speaker"] = args.speaker
        changed.append("화자")
    if args.pos is not None:
        if args.pos == "auto":
            t.pop("pos", None)
        elif args.pos not in ZONES:
            sys.exit(f"자리는 {', '.join(ZONES)} 또는 auto 중 하나여야 합니다: {args.pos}")
        else:
            t["pos"] = args.pos
        changed.append("자리")
    if args.tail is not None:
        if args.tail == "auto":
            t.pop("tail", None)
        else:
            t["tail"] = args.tail
        changed.append("꼬리")

    if not changed:
        sys.exit("바꿀 내용을 주세요 (--text / --type / --speaker / --pos / --tail)")

    _save(d, project)
    print(f"  컷 {args.cut} / {args.line}번 대사 — {', '.join(changed)} 수정")
    _print_lines(cut)
    compose_project(d, project, only=args.cut)  # 바로 결과를 볼 수 있어야 고치기 쉽다
    _save(d, project)


def cmd_export(args) -> None:
    """완성 컷을 업로드용으로 모은다. 순서대로 번호를 새로 매긴다."""
    import shutil

    from PIL import Image

    from .compose import OUT_H, OUT_W

    d, project = _load(args.project)
    dest = Path(args.out) if args.out else d / "export"
    dest.mkdir(parents=True, exist_ok=True)

    made, missing, wrong = [], [], []
    for cut in project["cuts"]:
        src = d / cut["out_image"]
        if not src.exists():
            missing.append(cut["index"])
            continue
        with Image.open(src) as im:
            if im.size != (OUT_W, OUT_H):
                wrong.append((cut["index"], im.size))
        target = dest / f"{len(made) + 1:02d}.png"
        shutil.copyfile(src, target)
        made.append(target)

    for i in missing:
        print(f"  {i:2d}번 컷: out 그림이 없습니다 — compose 를 먼저 실행하세요")
    for i, size in wrong:
        print(f"  {i:2d}번 컷: 규격이 다릅니다 {size} (기대 {OUT_W}x{OUT_H})")

    if not made:
        sys.exit("내보낼 컷이 없습니다")

    print(f"\n{len(made)}컷 내보냄 → {dest}")
    print(f"  파일명은 업로드 순서다 (01.png ~ {len(made):02d}.png)")
    if missing:
        print(f"  빠진 컷이 있어 번호가 원본과 어긋난다: {missing}")


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

    e = sub.add_parser("edit", help="말풍선·꼬리·대사 수정 (그림은 그대로, 비용 없음)")
    e.add_argument("cut", type=int, help="컷 번호")
    e.add_argument("--line", type=int, help="몇 번째 대사인지 (생략하면 목록만 보여준다)")
    e.add_argument("--text", help="대사 내용")
    e.add_argument("--type", choices=("dialogue", "thought", "narration"), help="말풍선 종류")
    e.add_argument("--speaker", help="화자 이름")
    e.add_argument("--pos", help="말풍선 자리. left-upper 등, auto 면 자동 선택으로 되돌린다")
    e.add_argument("--tail", help="꼬리가 향할 인물. auto 면 화자를 따른다")
    e.add_argument("--project")
    e.set_defaults(func=cmd_edit)

    x = sub.add_parser("export", help="완성 컷을 업로드용으로 모은다")
    x.add_argument("--out", help="내보낼 폴더 (기본: 프로젝트 안 export/)")
    x.add_argument("--project")
    x.set_defaults(func=cmd_export)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
