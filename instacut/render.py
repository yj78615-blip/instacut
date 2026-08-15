"""[3] 컷 이미지 생성 — 그림만 만든다. 텍스트는 얹지 않는다 (PRD F-3).

comfy CLI를 통해 로컬 ComfyUI에 붙는다. 한 편(8컷)은 foreach 블루프린트 하나로
단일 그래프에 팬아웃되므로 제출도 한 번이다.
산출물은 raw/ 뿐이고, 말풍선은 compose.py가 [4] 단계에서 따로 얹는다.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import yaml

from .compose import NARRATION_ZONE, ZONE_H, ZONES, _pick_zone
from .pose import draw_pose, place_subject
from .split import assemble_prompt

# 캐릭터 레퍼런스(projects/<이름>/character_ref.png)가 있으면 IP-Adapter 를 얹는다.
# 텍스트만으로는 "이 캐릭터"를 재현할 수 없다 — no nose 라고 써도 모델이 코를 그린다.
FRAGMENT_POSE = "sdxl_pose_t2i"
# 캐릭터 레퍼런스가 있으면 ControlNet 을 끄고 IP-Adapter 만 쓴다.
# 8GB VRAM 에 SDXL·ControlNet·IP-Adapter·CLIP Vision 을 모두 올리면 서버가 죽는다.
# 위치 제어를 잃는 대신 캐릭터 스타일을 지킨다.
FRAGMENT_CHAR = "sdxl_charonly_t2i"
CHAR_REF = "character_ref.png"
# 그림이 캔버스(1080x1350, 4:5) 전체를 채우므로 생성도 세로로 뽑는다.
# SDXL 버킷 중 4:5 에 가장 가까운 것 — 크롭 손실이 거의 없다.
GEN_W, GEN_H = 896, 1152


def _comfy_bin() -> str:
    found = shutil.which("comfy") or shutil.which("comfy.exe")
    if not found:
        raise RuntimeError("comfy CLI를 찾을 수 없습니다. pip install comfy-cli")
    return found


def _comfy(args: list[str], root: Path, timeout: int = 1800) -> dict:
    """comfy를 JSON 모드로 부르고 envelope을 돌려준다. 실패하면 hint까지 실어 올린다."""
    proc = subprocess.run(
        [_comfy_bin(), "--json", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    try:
        env = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        raise RuntimeError(
            f"comfy {' '.join(args)} 응답을 읽지 못했습니다.\n"
            f"stdout: {proc.stdout[-800:]}\nstderr: {proc.stderr[-800:]}"
        )

    if not env.get("ok"):
        err = env.get("error") or {}
        raise RuntimeError(
            f"comfy {args[0]} 실패 [{err.get('code')}]: {err.get('message')}\n"
            f"hint: {err.get('hint')}"
        )
    return env


def ensure_server(root: Path) -> None:
    env = _comfy(["env"], root, timeout=60)
    if not env["data"]["server"]["running"]:
        raise RuntimeError(
            "ComfyUI 서버가 꺼져 있습니다. 먼저 실행하세요:\n  comfy launch --background"
        )


def _discover(root: Path, folder: str, prefer: str, what: str) -> str:
    """모델 이름은 하드코딩하지 않는다. 서버가 실제로 가진 것을 쓴다."""
    files = _comfy(["models", "list-folder", folder, "--where", "local"], root, 120)["data"]["files"]
    if not files:
        raise RuntimeError(f"{folder} 폴더가 비어 있습니다. {what}을 넣어주세요.")
    names = [f["name"] for f in files]
    for name in names:
        if prefer in name.lower():
            return name
    return names[0]


def discover_checkpoint(root: Path) -> str:
    return _discover(root, "checkpoints", "xl", "SDXL 체크포인트")


def discover_controlnet(root: Path) -> str:
    return _discover(root, "controlnet", "openpose", "OpenPose ControlNet 모델")


def planned_zones(cut: dict) -> list[str]:
    """**생성 전에** 이 컷의 말풍선이 어디에 놓일지 정한다.

    아직 그림이 없으니 머리 위치를 모른다 — P-7(머리 회피) 없이 P-6 순서만 쓴다.
    그리고 이 자리를 피해 인물을 세우므로, 결과적으로 머리가 말풍선 자리에 오지 않는다.
    순서가 거꾸로였던 것이 그동안 구도가 안 잡힌 원인이었다.
    """
    n = sum(
        1
        for t in cut.get("texts", [])
        if t.get("type") != "narration" and (t.get("content") or "").strip()
    )
    zones: list[str] = []
    prev = None
    for _ in range(n):
        z = _pick_zone(set(zones), 1, 0, 1, None, prev)  # head=None → 순수 우선순위
        if z is None:
            break
        zones.append(z)
        prev = z
    return zones


def write_pose(root: Path, cut: dict, slug: str) -> str:
    """말풍선 자리를 피해 인물을 세운 스틱 피규어를 ComfyUI input 폴더에 쓴다."""
    zones = planned_zones(cut)
    boxes = [
        (ZONES[z][0], ZONES[z][1], ZONES[z][0] + ZONES[z][2], ZONES[z][1] + ZONE_H) for z in zones
    ]
    # 나레이션도 그림 위에 얹히므로 그 자리도 비워야 한다
    if any(
        t.get("type") == "narration" and (t.get("content") or "").strip() for t in cut.get("texts", [])
    ):
        boxes.append(NARRATION_ZONE)
    cx, head_y, height = place_subject(boxes)

    workspace = Path(_comfy(["env"], root, 60)["data"]["workspace"]["path"])
    input_dir = workspace / "input"
    input_dir.mkdir(parents=True, exist_ok=True)

    name = f"instacut_pose_{slug}_{cut['index']:02d}.png"
    draw_pose(GEN_W, cx, head_y, height).save(input_dir / name)
    return name


def build_blueprint(
    project: dict,
    cuts: list[dict],
    ckpt: str,
    cnet: str,
    poses: dict[int, str],
    slug: str,
    char_image: str | None = None,
) -> dict:
    """컷 하나당 foreach 아이템 하나. 단일 그래프로 팬아웃된다."""
    items = []
    for cut in cuts:
        cut["final_prompt"] = assemble_prompt(project, cut)
        items.append(
            {
                "id": f"cut_{cut['index']:02d}",
                "prompt": cut["final_prompt"],
                "negative": cut["negative_prompt"],
                "seed": cut["seed"],
                "pose": poses[cut["index"]],
            }
        )

    params = {
        "ckpt_name": ckpt,
        "positive": "$item.prompt",
        "negative": "$item.negative",
        "seed": "$item.seed",
        "width": GEN_W,
        "height": GEN_H,
    }
    if char_image:
        params["char_image"] = char_image
    else:
        params["controlnet_name"] = cnet
        params["pose_image"] = "$item.pose"
        # 위치만 잡고 인체 비율은 강제하지 않는다 — 캐릭터가 2등신일 수도 있다
        params["strength"] = 0.5
        params["end_percent"] = 0.5

    return {
        "output_prefix": f"outputs/{slug}",
        "foreach": items,
        "pipeline": [
            {
                "fragment": FRAGMENT_CHAR if char_image else FRAGMENT_POSE,
                "alias": "shot",
                "params": params,
            }
        ],
    }


def _collect(files: list[dict], cuts: list[dict], project_dir: Path) -> list[Path]:
    """다운로드 결과를 raw/cut_NN.png 로 정리한다.

    받은 파일명은 item 기반이 아니라 prompt 기반(`<prompt8>_<nnn>.png`)이다.
    컷 번호는 envelope의 `url`(서버가 저장한 원본 경로)에 남은 SaveImage 접두사에서
    되찾는다. 배열 순서는 믿지 않는다 — 팬아웃 결과의 순서는 보장되지 않는다.
    """
    by_index: dict[int, Path] = {}
    for f in files:
        m = re.search(r"cut_(\d{2})", Path(str(f.get("url", ""))).name)
        if m:
            by_index[int(m.group(1))] = Path(f["path"])

    made = []
    for cut in cuts:
        src = by_index.get(cut["index"])
        if src is None or not src.exists():
            print(f"  {cut['index']:2d}번 컷: 결과 파일을 찾지 못했습니다")
            continue

        target = project_dir / cut["raw_image"]
        target.parent.mkdir(parents=True, exist_ok=True)
        src.replace(target)
        made.append(target)
        print(f"  {cut['index']:2d}번 컷 → {cut['raw_image']}")
    return made


def render(root: Path, project_dir: Path, project: dict, only: int | None = None) -> list[Path]:
    ensure_server(root)

    cuts = [
        c
        for c in project["cuts"]
        if (only is None or c["index"] == only) and not (c["locked"] and only is None)
    ]
    if not cuts:
        print("생성할 컷이 없습니다 (모두 잠겨 있거나 지정한 컷이 없습니다)")
        return []

    slug = project_dir.name
    ckpt = discover_checkpoint(root)
    cnet = discover_controlnet(root)
    print(f"체크포인트: {ckpt}")
    print(f"ControlNet: {cnet}  |  {len(cuts)}컷 생성")

    # 캐릭터 레퍼런스가 있으면 ComfyUI input 으로 복사해 IP-Adapter 에 물린다
    char_image = None
    ref = project_dir / CHAR_REF
    if ref.exists():
        workspace = Path(_comfy(["env"], root, 60)["data"]["workspace"]["path"])
        char_image = f"instacut_char_{slug}.png"
        shutil.copy(ref, workspace / "input" / char_image)
        print(f"캐릭터 레퍼런스: {ref.name} → IP-Adapter")

    poses = {}
    for cut in cuts:
        poses[cut["index"]] = write_pose(root, cut, slug)
        zones = planned_zones(cut)
        print(f"  {cut['index']:2d}번 컷 말풍선 자리: {', '.join(zones) or '(없음)'}")

    bp_path = root / "blueprints" / f"{slug}.yaml"
    bp_path.parent.mkdir(parents=True, exist_ok=True)
    bp_path.write_text(
        yaml.safe_dump(
            build_blueprint(project, cuts, ckpt, cnet, poses, slug, char_image),
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    _comfy(["workflow", "compose", str(bp_path.relative_to(root))], root, 300)
    compiled = bp_path.with_suffix(".compiled.json")

    # 제출 전 검증 — 모델 이름이나 노드가 틀리면 20분 뒤가 아니라 지금 알아야 한다.
    # (IPAdapter preset 과 받아둔 모델이 어긋나 두 번이나 조용히 실패했다)
    try:
        _comfy(["validate", "--workflow", str(compiled.relative_to(root))], root, 120)
    except RuntimeError as e:
        print(f"  ⚠ 사전 검증 경고: {e}")

    staging = root / "outputs"
    staging.mkdir(exist_ok=True)

    print("생성 중... (컷당 15~30초)")
    run_env = _comfy(["run", "--workflow", str(compiled.relative_to(root)), "--wait"], root)
    prompt_id = run_env["data"].get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"prompt_id를 받지 못했습니다: {run_env['data']}")

    dl = _comfy(["download", prompt_id, "--out-dir", str(staging)], root, 600)
    return _collect(dl["data"]["files"], cuts, project_dir)
