#!/usr/bin/env python3
"""
batch.py — 把 exam.json 裡每一題都跑過 v0 pipeline 產生 MP4

使用: python3 batch.py <exam.json> [output_dir]
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

# Windows 終端 cp950 不支援 emoji，強制 UTF-8
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# v0 pipeline 的位置（同目錄）
PIPELINE = Path(__file__).parent / "pipeline.py"


def problem_to_v0_json(exam_title: str, prob: dict) -> dict:
    """把 v1 的 problem 轉成 v0 pipeline 吃的格式"""
    v0_dict = {
        "title": f"{exam_title} — {prob['number']}",
        "subtitle": prob["problem"][:30] + ("..." if len(prob["problem"]) > 30 else ""),
        "problem": prob["problem"],
        "steps": prob["steps"],
    }
    if "image" in prob:
        v0_dict["image"] = prob["image"]
    return v0_dict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("exam_json", help="v1 exam.json 路徑")
    ap.add_argument("output_dir", nargs="?", default="./videos",
                    help="輸出根目錄 (預設 ./videos),實際寫到 <output_dir>/<exam_stem>/")
    ap.add_argument("--only", nargs="+", help="只跑特定題目 id,例如 --only q1 q3")
    args = ap.parse_args()

    exam_path = Path(args.exam_json)
    # 各考卷獨立 subfolder,避免多份 exam 都有 q1.mp4 會互相覆蓋
    out_dir = Path(args.output_dir) / exam_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    data = json.loads(exam_path.read_text(encoding="utf-8"))
    problems = data["problems"]
    if args.only:
        problems = [p for p in problems if p["id"] in args.only]
        if not problems:
            sys.exit(f"❌ 找不到題目 id: {args.only}")

    print(f"📦 準備生成 {len(problems)} 支影片,輸出到 {out_dir}")
    if not PIPELINE.exists():
        sys.exit(f"❌ 找不到 v0 pipeline: {PIPELINE}")

    results = []
    for i, prob in enumerate(problems):
        pid = prob["id"]
        v0_json_path = out_dir / f"{pid}.json"
        v0_data = problem_to_v0_json(data["exam_title"], prob)
        v0_json_path.write_text(
            json.dumps(v0_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        out_name = str(out_dir / pid)
        print(f"\n[{i+1}/{len(problems)}] 處理 {pid}: {prob['number']}")
        try:
            subprocess.run(
                [sys.executable, str(PIPELINE), str(v0_json_path.resolve()), pid],
                check=True,
            )
            # pipeline.py 輸出到 output/ 目錄，搬到 out_dir
            pipeline_out = Path(__file__).parent / "output"
            src_mp4 = pipeline_out / f"{pid}.mp4"
            src_srt = pipeline_out / f"{pid}.srt"
            dst_mp4 = out_dir / f"{pid}.mp4"
            dst_srt = out_dir / f"{pid}.srt"
            if src_mp4.exists():
                src_mp4.replace(dst_mp4)
            if src_srt.exists():
                src_srt.replace(dst_srt)
            results.append((pid, True, dst_mp4))
        except subprocess.CalledProcessError as e:
            print(f"   ❌ 失敗: {e}")
            results.append((pid, False, None))

    print(f"\n{'=' * 50}")
    print(f"完成: {sum(1 for _, ok, _ in results if ok)}/{len(results)} 成功")
    for pid, ok, path in results:
        status = "✅" if ok else "❌"
        print(f"  {status} {pid}: {path if ok else '失敗'}")


if __name__ == "__main__":
    main()
