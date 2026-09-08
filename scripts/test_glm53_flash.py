#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from alignment_harness.llm import ClaudeRunner


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe GLM-5.3-Flash text and local-image reading")
    parser.add_argument("--output", type=Path, default=Path("artifacts/alignment_harness/probe"))
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    image_path = output / "vision_probe.png"
    image = Image.new("RGB", (800, 450), "#164e9b")
    draw = ImageDraw.Draw(image)
    draw.polygon([(400, 55), (180, 365), (620, 365)], fill="#ffd84d")
    draw.rectangle((250, 175, 550, 265), fill="white")
    draw.text((285, 205), "VISION_OK_735", fill="black", stroke_width=1)
    image.save(image_path)

    runner = ClaudeRunner("glm-5.3-flash", timeout_seconds=300)
    text_result = runner.run(
        'Return exactly {"probe":"text","ok":true} as JSON.', Path.cwd(), allow_read=False
    )
    image_result = runner.run(
        f"""Use Read to inspect the local image {image_path}. Return JSON only with keys:
probe='image', background_color, shape_color, shape, visible_text, and ok. Set ok true only
if the visible text is VISION_OK_735 and the large shape is a yellow triangle.""",
        Path.cwd(),
    )
    report = {
        "model": runner.model,
        "text": text_result.data,
        "image": image_result.data,
        "passed": text_result.data.get("ok") is True and image_result.data.get("ok") is True,
        "image_file": str(image_path),
        "cost_usd": text_result.cost_usd + image_result.cost_usd,
    }
    (output / "probe_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
