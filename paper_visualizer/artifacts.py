"""Content-addressed artifacts and stage manifests."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


@dataclass
class ArtifactStore:
    root: Path
    cache_root: Path

    def paper_dir(self, paper_id: str) -> Path:
        path = self.root / paper_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def stage_path(self, paper_id: str, stage: str, filename: str) -> Path:
        path = self.paper_dir(paper_id) / stage / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def write_stage_json(
        self,
        paper_id: str,
        stage: str,
        filename: str,
        payload: Any,
        *,
        stage_version: str,
        input_hashes: list[str],
        attempt: int = 1,
        status: str = "passed",
    ) -> Path:
        wrapped = {
            "schema_version": "1.0.0",
            "stage_version": stage_version,
            "input_hashes": input_hashes,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "attempt": attempt,
            "status": status,
            "data": payload,
        }
        path = self.stage_path(paper_id, stage, filename)
        atomic_write_json(path, wrapped)
        return path

