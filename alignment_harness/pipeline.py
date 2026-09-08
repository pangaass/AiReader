from __future__ import annotations

import concurrent.futures
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .llm import AgentResult, ClaudeRunner
from .preprocess import prepare_paper_context, prepare_presentation_units, split_paper_context
from .prompts import (
    ALIGNMENT_SCHEMA,
    BASELINE_PAPER_INSTRUCTION,
    OPTIMIZER_SCHEMA,
    PAPER_SCHEMA,
    PRESENTATION_SCHEMA,
    alignment_prompt,
    optimizer_prompt,
    paper_prompt,
    presentation_prompt,
)


def load_manifest(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    pairs = payload.get("pairs", payload) if isinstance(payload, dict) else payload
    if not isinstance(pairs, list):
        raise ValueError("Manifest must contain a list or a `pairs` list")
    required = {"id", "paper_file", "presentation_file", "presentation_type"}
    for pair in pairs:
        missing = required - pair.keys()
        if missing:
            raise ValueError(f"Pair is missing {sorted(missing)}: {pair}")
    return pairs


def deterministic_score(presentation: dict[str, Any], paper: dict[str, Any]) -> dict[str, float]:
    """Cheap diagnostics; semantic quality remains the independent judge's job."""
    target_overview = len(presentation.get("overview") or [])
    paper_overview = len(paper.get("overview") or [])
    target_sections = len(presentation.get("sections") or [])
    paper_sections = len(paper.get("sections") or [])
    evidence = [
        evidence
        for item in paper.get("overview") or []
        for evidence in item.get("evidence") or []
        if evidence.get("quote")
    ]
    return {
        "overview_count_recall": min(paper_overview / max(target_overview, 1), 1.0),
        "section_count_recall": min(paper_sections / max(target_sections, 1), 1.0),
        "overview_evidence_rate": min(len(evidence) / max(paper_overview, 1), 1.0),
    }


@dataclass
class PairRun:
    pair_id: str
    presentation: dict[str, Any]
    paper: dict[str, Any]
    audit: dict[str, Any]
    deterministic: dict[str, float]
    cost_usd: float


class AlignmentPipeline:
    def __init__(self, root: Path, output: Path, runner: ClaudeRunner, *, resume: bool = False):
        self.root = root.resolve()
        self.output = output.resolve()
        self.runner = runner
        self.resume = resume
        self.output.mkdir(parents=True, exist_ok=True)

    def _record(self, directory: Path, name: str, result: AgentResult) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}.json").write_text(
            json.dumps(result.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        metadata = {
            "model": self.runner.model,
            "cost_usd": result.cost_usd,
            "duration_ms": result.envelope.get("duration_ms"),
            "session_id": result.envelope.get("session_id"),
        }
        (directory / f"{name}.meta.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @staticmethod
    def _aggregate(parts: list[dict[str, Any]], presentation_type: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "paper_id": next((part.get("paper_id") for part in parts if part.get("paper_id")), "unknown"),
            "title": next((part.get("title") for part in parts if part.get("title")), None),
            "overview": [],
            "sections": [],
            "visual_references": [],
            "warnings": [],
        }
        if presentation_type:
            result["presentation_type"] = presentation_type
            result["narrative_order"] = []
        seen: dict[str, set[str]] = {"overview": set(), "sections": set(), "visual_references": set()}
        for part in parts:
            for key, identity in (
                ("overview", "summary"), ("sections", "heading"), ("visual_references", "label")
            ):
                for item in part.get(key) or []:
                    marker = str(item.get(identity) or item.get("visible_label") or item)
                    if marker not in seen[key]:
                        seen[key].add(marker)
                        result[key].append(item)
            result["warnings"].extend(part.get("warnings") or [])
            if presentation_type:
                result["narrative_order"].extend(part.get("narrative_order") or [])
        return result

    @staticmethod
    def _compact_for_judge(data: dict[str, Any]) -> dict[str, Any]:
        sections = []
        for section in (data.get("sections") or [])[:12]:
            sections.append(
                {
                    "heading": section.get("heading"),
                    "role": section.get("role"),
                    "summary": section.get("summary"),
                    "detail_claims": [
                        detail.get("claim") for detail in (section.get("details") or [])[:2]
                    ],
                }
            )
        return {
            "title": data.get("title"),
            "overview": (data.get("overview") or [])[:12],
            "sections": sections,
            "visual_references": (data.get("visual_references") or [])[:12],
            "warnings": (data.get("warnings") or [])[:8],
        }

    def _run_presentation(self, pair: dict[str, Any], source: Path, pair_dir: Path) -> AgentResult:
        units = prepare_presentation_units(source, pair_dir.parent / "prepared")
        results: list[tuple[str, AgentResult]] = []
        errors: list[str] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(units), 2)) as pool:
            futures = {
                pool.submit(
                    self.runner.run,
                    presentation_prompt(pair, path, embedded, label),
                    self.root,
                    allow_read=path is not None,
                    schema=PRESENTATION_SCHEMA,
                ): label
                for label, path, embedded in units
            }
            for future in concurrent.futures.as_completed(futures):
                label = futures[future]
                try:
                    result = future.result()
                    results.append((label, result))
                    self._record(pair_dir / "presentation_units", label, result)
                except Exception as exc:
                    errors.append(f"{label}: {type(exc).__name__}: {exc}")
                    error_path = pair_dir / "presentation_units" / f"{label}.error.json"
                    error_path.parent.mkdir(parents=True, exist_ok=True)
                    error_path.write_text(json.dumps({"error": errors[-1]}, indent=2), encoding="utf-8")
        if not results:
            raise RuntimeError("All presentation units failed: " + "; ".join(errors))
        data = self._aggregate([result.data for _, result in results], pair["presentation_type"])
        data["warnings"].extend(errors)
        envelope = {
            "total_cost_usd": sum(result.cost_usd for _, result in results),
            "duration_ms": max((result.envelope.get("duration_ms") or 0) for _, result in results),
            "session_id": [result.envelope.get("session_id") for _, result in results],
        }
        return AgentResult(data, envelope)

    def _run_paper(
        self, pair: dict[str, Any], context_path: Path, paper_file: Path, instruction: str, pair_dir: Path
    ) -> AgentResult:
        units = split_paper_context(context_path.read_text(encoding="utf-8"))
        results: list[tuple[str, AgentResult]] = []
        errors: list[str] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(units), 2)) as pool:
            futures = {
                pool.submit(
                    self.runner.run,
                    paper_prompt(
                        pair, context_path, instruction, original_pdf=paper_file,
                        embedded_content=content, unit_label=label,
                    ),
                    self.root,
                    allow_read=False,
                    schema=PAPER_SCHEMA,
                ): label
                for label, content in units
            }
            for future in concurrent.futures.as_completed(futures):
                label = futures[future]
                try:
                    result = future.result()
                    results.append((label, result))
                    self._record(pair_dir / "paper_units", label, result)
                except Exception as exc:
                    errors.append(f"{label}: {type(exc).__name__}: {exc}")
                    error_path = pair_dir / "paper_units" / f"{label}.error.json"
                    error_path.parent.mkdir(parents=True, exist_ok=True)
                    error_path.write_text(json.dumps({"error": errors[-1]}, indent=2), encoding="utf-8")
        if not results:
            raise RuntimeError("All paper units failed: " + "; ".join(errors))
        data = self._aggregate([result.data for _, result in results])
        data["warnings"].extend(errors)
        envelope = {
            "total_cost_usd": sum(result.cost_usd for _, result in results),
            "duration_ms": max((result.envelope.get("duration_ms") or 0) for _, result in results),
            "session_id": [result.envelope.get("session_id") for _, result in results],
        }
        return AgentResult(data, envelope)

    def run_pair(
        self,
        pair: dict[str, Any],
        instruction: str,
        iteration: int,
        presentation_cache: dict[str, Any] | None = None,
    ) -> PairRun:
        pair_dir = self.output / pair["id"] / f"iteration-{iteration:02d}"
        presentation_file = (self.root / pair["presentation_file"]).resolve()
        paper_file = (self.root / pair["paper_file"]).resolve()
        for source in (presentation_file, paper_file):
            if not source.is_file():
                raise FileNotFoundError(source)
        paper_context = prepare_paper_context(
            paper_file, self.output / pair["id"] / "prepared" / "paper_context.md"
        )

        saved_presentation = pair_dir / "presentation_extraction.json"
        saved_paper = pair_dir / "paper_extraction.json"
        resumed_extractions = False
        if self.resume and saved_presentation.is_file() and saved_paper.is_file():
            presentation_data = json.loads(saved_presentation.read_text(encoding="utf-8"))
            paper_result = AgentResult(
                json.loads(saved_paper.read_text(encoding="utf-8")),
                {"total_cost_usd": 0.0, "duration_ms": 0, "session_id": "resumed"},
            )
            presentation_cost = 0.0
            resumed_extractions = True
        elif presentation_cache is None:
            # On the baseline, independent agents execute concurrently and neither sees
            # the other's source. Later iterations freeze this presentation-side anchor.
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                future_presentation = pool.submit(
                    self._run_presentation, pair, presentation_file, pair_dir
                )
                future_paper = pool.submit(
                    self._run_paper, pair, paper_context, paper_file, instruction, pair_dir
                )
                presentation_result = future_presentation.result()
                self._record(pair_dir, "presentation_extraction", presentation_result)
                paper_result = future_paper.result()
            presentation_data = presentation_result.data
            presentation_cost = presentation_result.cost_usd
        else:
            presentation_data = presentation_cache
            presentation_cost = 0.0
            (pair_dir / "presentation_extraction.json").parent.mkdir(parents=True, exist_ok=True)
            (pair_dir / "presentation_extraction.json").write_text(
                json.dumps(presentation_data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            paper_result = self._run_paper(pair, paper_context, paper_file, instruction, pair_dir)
        if not resumed_extractions:
            self._record(pair_dir, "paper_extraction", paper_result)
        diagnostics = deterministic_score(presentation_data, paper_result.data)
        saved_audit = pair_dir / "alignment_audit.json"
        resumed_audit = self.resume and resumed_extractions and saved_audit.is_file()
        if resumed_audit:
            judge_result = AgentResult(
                json.loads(saved_audit.read_text(encoding="utf-8")),
                {"total_cost_usd": 0.0, "duration_ms": 0, "session_id": "resumed"},
            )
        else:
            try:
                judge_result = self.runner.run(
                    alignment_prompt(
                        pair,
                        self._compact_for_judge(presentation_data),
                        self._compact_for_judge(paper_result.data),
                    ),
                    self.root,
                    allow_read=False,
                    schema=ALIGNMENT_SCHEMA,
                )
            except Exception as exc:
                fallback_score = sum(diagnostics.values()) / max(len(diagnostics), 1)
                judge_result = AgentResult(
                    {
                        "overall_score": round(fallback_score, 4),
                        "scores": diagnostics,
                        "matched": [],
                        "missing_from_paper_harness": [],
                        "unsupported_or_overstated": [],
                        "presentation_only_external": [],
                        "recommendations": [f"Judge failed and needs retry: {type(exc).__name__}"],
                    },
                    {"total_cost_usd": 0.0, "duration_ms": None, "session_id": None},
                )
        if not resumed_audit:
            self._record(pair_dir, "alignment_audit", judge_result)
        (pair_dir / "deterministic_scores.json").write_text(
            json.dumps(diagnostics, indent=2), encoding="utf-8"
        )
        return PairRun(
            pair_id=pair["id"],
            presentation=presentation_data,
            paper=paper_result.data,
            audit=judge_result.data,
            deterministic=diagnostics,
            cost_usd=presentation_cost + paper_result.cost_usd + judge_result.cost_usd,
        )

    def run(
        self,
        pairs: list[dict[str, Any]],
        iterations: int = 2,
        initial_instruction: str = BASELINE_PAPER_INSTRUCTION,
    ) -> dict[str, Any]:
        if iterations < 1:
            raise ValueError("iterations must be at least 1")
        if not pairs:
            raise ValueError("at least one pair is required")
        instruction = initial_instruction.strip()
        history: list[dict[str, Any]] = []
        total_cost = 0.0
        started = time.time()
        presentation_cache: dict[str, dict[str, Any]] = {}
        best_score = -1.0
        selected_instruction = instruction
        for iteration in range(iterations):
            # Pair-level processing is deliberately bounded; the two side agents inside
            # each pair remain parallel, while avoiding provider-side queue saturation.
            runs = [
                self.run_pair(pair, instruction, iteration, presentation_cache.get(pair["id"]))
                for pair in pairs
            ]
            presentation_cache.update({run.pair_id: run.presentation for run in runs})
            total_cost += sum(run.cost_usd for run in runs)
            scores = [float(run.audit.get("overall_score") or 0.0) for run in runs]
            record: dict[str, Any] = {
                "iteration": iteration,
                "instruction": instruction,
                "pair_scores": {run.pair_id: run.audit.get("overall_score") for run in runs},
                "mean_score": sum(scores) / max(len(scores), 1),
                "deterministic_scores": {run.pair_id: run.deterministic for run in runs},
            }
            if record["mean_score"] > best_score:
                best_score = record["mean_score"]
                selected_instruction = instruction
            if iteration + 1 < iterations:
                optimized = self.runner.run(
                    optimizer_prompt(instruction, [run.audit for run in runs]),
                    self.root,
                    allow_read=False,
                    schema=OPTIMIZER_SCHEMA,
                )
                total_cost += optimized.cost_usd
                self._record(self.output / "meta", f"optimizer-{iteration:02d}", optimized)
                revised = optimized.data.get("revised_instruction")
                if isinstance(revised, str) and revised.strip():
                    instruction = revised.strip()
                record["optimizer"] = optimized.data
            history.append(record)

        summary = {
            "model": self.runner.model,
            "pair_count": len(pairs),
            "iterations": iterations,
            "history": history,
            "selected_instruction": selected_instruction,
            "selected_score": best_score,
            "last_proposed_instruction": instruction,
            "total_cost_usd": round(total_cost, 6),
            "elapsed_seconds": round(time.time() - started, 2),
            "run_fingerprint": hashlib.sha256(
                json.dumps(history, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest(),
        }
        (self.output / "round_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (self.output / "selected_instruction.md").write_text(
            selected_instruction.rstrip() + "\n", encoding="utf-8"
        )
        return summary
