from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any


def download_file(url: str, destination: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and destination.stat().st_size > 0:
        content = destination.read_bytes()
        return {
            "url": url,
            "path": str(destination),
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "cached": True,
        }
    temporary = destination.with_suffix(destination.suffix + ".part")
    completed = subprocess.run(
        [
            "curl", "-L", "--fail", "--retry", "3", "--connect-timeout", "20",
            "--max-time", "180", "--silent", "--show-error", "-o", str(temporary), url,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Download failed for {url}: {completed.stderr.strip()}")
    os.replace(temporary, destination)
    content = destination.read_bytes()
    return {
        "url": url,
        "path": str(destination),
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "cached": False,
    }


def download_manifest(manifest_path: Path, root: Path) -> list[dict[str, Any]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    receipts: list[dict[str, Any]] = []
    for pair in manifest["pairs"]:
        paper_target = root / pair["paper_file"]
        receipt = download_file(pair["paper_url"], paper_target)
        receipt.update({"pair_id": pair["id"], "side": "paper"})
        receipts.append(receipt)

        presentation_target = root / pair["presentation_file"]
        if "presentation_url" in pair:
            receipt = download_file(pair["presentation_url"], presentation_target)
        else:
            source = pair["presentation_extract"]
            bundle_target = presentation_target.with_name("source-bundle.json")
            receipt = download_file(source["url"], bundle_target)
            bundle = json.loads(bundle_target.read_text(encoding="utf-8"))
            record = bundle["data"][int(source["record_index"])]
            selected = {
                "dataset": "GEM/SciDuet",
                "paper_id": record["paper_id"],
                "paper_title": record["paper_title"],
                "slides": record["slides"],
            }
            presentation_target.parent.mkdir(parents=True, exist_ok=True)
            presentation_target.write_text(
                json.dumps(selected, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            receipt.update(
                {
                    "derived_path": str(presentation_target),
                    "derived_sha256": hashlib.sha256(presentation_target.read_bytes()).hexdigest(),
                    "record_index": source["record_index"],
                }
            )
        receipt.update({"pair_id": pair["id"], "side": "presentation"})
        receipts.append(receipt)
    receipt_path = root / "data/alignment_pairs/download_receipts.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipts, indent=2), encoding="utf-8")
    return receipts
