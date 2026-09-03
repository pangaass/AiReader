"""Versioned stage runner backed by the shared multi-agent protocol."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from dataclasses import replace
from typing import Any, Callable, Mapping, Protocol

from paper_visualizer.agents.base import ModuleAgent
from paper_visualizer.artifacts import atomic_write_json, sha256_file
from paper_visualizer.protocol import ArtifactRef, TaskEnvelope, TaskResult


class RetryableStageError(RuntimeError):
    """Explicitly marks a transient stage failure."""


class FatalStageError(RuntimeError):
    """Marks a deterministic stage/configuration failure."""


class FunctionWorker:
    def __init__(self, name: str, producer: Callable[[TaskEnvelope], object]) -> None:
        self.name = name
        self.producer = producer

    def run(self, task: TaskEnvelope) -> object:
        return self.producer(task)


class FunctionVerifier:
    def __init__(self, name: str, kind: str, validator: Callable[[object], list[str]] | None = None) -> None:
        self.name = name
        self.kind = kind
        self.validator = validator

    def review(self, task: TaskEnvelope, value: object) -> dict[str, dict[str, Any]]:
        valid = isinstance(value, Mapping) if self.kind == "json" else isinstance(value, str)
        if not valid:
            raise FatalStageError(f"{task.module} worker returned an invalid {self.kind} artifact")
        if self.kind == "json" and isinstance(value, Mapping) and value.get("schema_version") != "1.0.0":
            raise FatalStageError(f"{task.module} worker returned an unsupported schema_version")
        errors = self.validator(value) if self.validator else []
        if errors:
            raise FatalStageError(f"{task.module} verification failed ({len(errors)} checks)")
        return {
            "worker_output_type": {"status": "passed", "kind": self.kind, "verified_by": self.name},
            "module_contract": {"status": "passed", "error_count": 0, "verified_by": self.name},
        }


class ExecutionBackend(Protocol):
    """Pluggable boundary for local, process, or remote worker execution."""

    def run_worker(self, worker: FunctionWorker, task: TaskEnvelope) -> object: ...
    def run_verifier(self, verifier: FunctionVerifier, task: TaskEnvelope, value: object) -> dict[str, dict[str, Any]]: ...


class InProcessExecutionBackend:
    """Default deterministic backend; external agent backends can implement the same protocol."""

    def run_worker(self, worker: FunctionWorker, task: TaskEnvelope) -> object:
        return worker.run(task)

    def run_verifier(self, verifier: FunctionVerifier, task: TaskEnvelope, value: object) -> dict[str, dict[str, Any]]:
        return verifier.review(task, value)


class SubprocessExecutionBackend:
    """JSON-over-stdio backend for process-isolated or external agent adapters.

    Worker commands receive ``{"task": ...}`` and return ``{"value": ...}``.
    Verifier commands receive ``{"task": ..., "value": ...}`` and return an
    approved response with a non-empty ``checks`` object and ``agent_identity``.
    """

    def __init__(self, *, worker_command: list[str], verifier_command: list[str], timeout_seconds: int = 600) -> None:
        if not worker_command or not verifier_command:
            raise ValueError("worker_command and verifier_command are required")
        self.worker_command = list(worker_command)
        self.verifier_command = list(verifier_command)
        self.timeout_seconds = timeout_seconds

    def _call(self, command: list[str], payload: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            completed = subprocess.run(
                command,
                input=json.dumps(payload, ensure_ascii=False),
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RetryableStageError(f"external agent unavailable ({type(exc).__name__})") from exc
        if completed.returncode != 0:
            raise RetryableStageError(f"external agent exited with status {completed.returncode}")
        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise FatalStageError("external agent returned invalid JSON") from exc
        if not isinstance(response, Mapping) or not str(response.get("agent_identity") or "").strip():
            raise FatalStageError("external agent response lacks agent_identity")
        return response

    def run_worker(self, worker: FunctionWorker, task: TaskEnvelope) -> object:
        response = self._call(self.worker_command, {"role": "worker", "agent": worker.name, "task": task.to_dict()})
        if "value" not in response:
            raise FatalStageError("external worker response lacks value")
        return response["value"]

    def run_verifier(self, verifier: FunctionVerifier, task: TaskEnvelope, value: object) -> dict[str, dict[str, Any]]:
        response = self._call(self.verifier_command, {"role": "verifier", "agent": verifier.name, "task": task.to_dict(), "value": value})
        if response.get("approved") is not True or not isinstance(response.get("checks"), Mapping) or not response["checks"]:
            raise FatalStageError("external verifier did not approve the artifact with evidence")
        local_checks = verifier.review(task, value)
        return {**local_checks, "external_verifier": {"status": "passed", "agent_identity": response["agent_identity"], "checks": dict(response["checks"])}}


def stable_digest(value: object) -> str:
    """Hash semantic inputs while ignoring volatile execution metadata."""

    volatile = {"created_at", "attempt", "cache_hit"}

    def clean(item: object) -> object:
        if isinstance(item, Mapping):
            return {str(key): clean(value) for key, value in item.items() if key not in volatile}
        if isinstance(item, list):
            return [clean(value) for value in item]
        return item

    raw = json.dumps(clean(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


class FunctionModuleAgent(ModuleAgent):
    """Adapter that makes a deterministic stage callable obey ModuleAgent."""

    def __init__(
        self,
        *,
        name: str,
        role: str,
        worker: FunctionWorker,
        verifier: FunctionVerifier,
        backend: ExecutionBackend,
        writer: Callable[[object], None],
        output_path: Path,
        schema_version: str,
        status_resolver: Callable[[object], str] | None = None,
    ) -> None:
        self.name = name
        self.role = role
        self.worker = worker
        self.verifier = verifier
        self.backend = backend
        self.writer = writer
        self.output_path = output_path
        self.schema_version = schema_version
        self.status_resolver = status_resolver

    def run(self, task: TaskEnvelope) -> TaskResult:
        worker_task = replace(task, module=f"{task.module}.worker", task_id=f"{task.task_id}:worker")
        value = self.backend.run_worker(self.worker, worker_task)
        verifier_task = replace(task, module=f"{task.module}.verifier", task_id=f"{task.task_id}:verifier")
        verification_checks = self.backend.run_verifier(self.verifier, verifier_task, value)
        self.writer(value)
        status = self.status_resolver(value) if self.status_resolver else "passed"
        return TaskResult(
            status=status,  # type: ignore[arg-type]
            output_artifacts=[ArtifactRef(str(self.output_path), sha256_file(self.output_path), self.schema_version)],
            checks={
                **verification_checks,
                "worker_invoked": {"status": "passed", "worker": self.worker.name},
                "worker_task": {"status": "passed", "task_id": worker_task.task_id},
                "verifier_task": {"status": "passed", "task_id": verifier_task.task_id},
                "output_persisted": {"status": "passed", "path": str(self.output_path)},
                "input_hashes_recorded": {"status": "passed", "count": len(task.input_artifacts)},
            },
            agent_role=self.role,
            reviewed_by=self.verifier.name,
        )


class StageRunner:
    def __init__(self, *, paper_id: str, paper_dir: Path, cache_dir: Path, max_attempts: int, force: bool, backend: ExecutionBackend | None = None) -> None:
        self.paper_id = paper_id
        self.paper_dir = paper_dir
        self.cache_dir = cache_dir
        self.max_attempts = max(1, max_attempts)
        self.force = force
        self.backend = backend or InProcessExecutionBackend()
        self.run_id = uuid.uuid4().hex

    def _record(self, stage: str, envelope: TaskEnvelope, result: TaskResult, *, cache_hit: bool, stage_version: str) -> None:
        atomic_write_json(self.paper_dir / "orchestration" / f"{stage}.json", {
            "stage_version": stage_version,
            "cache_hit": cache_hit,
            "task": envelope.to_dict(),
            "result": result.to_dict(),
        })

    def run(
        self,
        *,
        stage: str,
        stage_version: str,
        role: str,
        reviewed_by: str,
        inputs: Mapping[str, object],
        output_path: Path,
        producer: Callable[[TaskEnvelope], object],
        kind: str = "json",
        schema_version: str = "1.0.0",
        acceptance_checks: tuple[str, ...] = ("output_persisted", "input_hashes_recorded"),
        use_stage_cache: bool = True,
        attempts: int | None = None,
        status_resolver: Callable[[object], str] | None = None,
        input_paths: Mapping[str, Path] | None = None,
        retryable_exceptions: tuple[type[Exception], ...] = (RetryableStageError, OSError, TimeoutError, ConnectionError),
        validator: Callable[[object], list[str]] | None = None,
    ) -> tuple[object, bool]:
        input_refs = [
            ArtifactRef(str(path), sha256_file(path), "1.0.0")
            for _, path in sorted((input_paths or {}).items())
            if path.is_file()
        ]
        semantic_input_hashes = {key: stable_digest(value) for key, value in sorted(inputs.items())}
        cache_key = stable_digest({"stage": stage, "version": stage_version, "inputs": semantic_input_hashes})
        extension = "json" if kind == "json" else "html"
        cache_path = self.cache_dir / stage / stage_version / cache_key / f"artifact.{extension}"
        cache_manifest = cache_path.parent / "manifest.json"
        envelope = TaskEnvelope(
            run_id=self.run_id,
            paper_id=self.paper_id,
            module=stage,
            task_id=f"{self.run_id}:{stage}",
            input_artifacts=input_refs,
            acceptance_checks=list(acceptance_checks),
            constraints={"cache_key": cache_key, "output_path": str(output_path)},
        )

        if use_stage_cache and cache_path.is_file() and cache_manifest.is_file() and not self.force:
            try:
                metadata = json.loads(cache_manifest.read_text(encoding="utf-8"))
                valid_cache = (
                    metadata.get("stage_version") == stage_version
                    and metadata.get("input_hashes") == semantic_input_hashes
                    and metadata.get("artifact_sha256") == sha256_file(cache_path)
                    and metadata.get("schema_version") == schema_version
                )
                if valid_cache:
                    value = json.loads(cache_path.read_text(encoding="utf-8")) if kind == "json" else cache_path.read_text(encoding="utf-8")
                    valid_cache = isinstance(value, Mapping) if kind == "json" else isinstance(value, str)
                    if kind == "json" and isinstance(value, Mapping):
                        valid_cache = value.get("schema_version") == schema_version
                    if not valid_cache:
                        value = None
                else:
                    value = None
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                value = None
        else:
            value = None
        if value is not None:
            if kind == "json":
                atomic_write_json(output_path, value)
            else:
                _atomic_write_text(output_path, str(value))
            status = status_resolver(value) if status_resolver else "passed"
            result = TaskResult(
                status=status,  # type: ignore[arg-type]
                output_artifacts=[ArtifactRef(str(output_path), sha256_file(output_path), schema_version)],
                checks={"cache": {"status": "passed", "cache_hit": True}, "output_persisted": {"status": "passed"}},
                agent_role=role,
                reviewed_by=reviewed_by,
            )
            self._record(stage, envelope, result, cache_hit=True, stage_version=stage_version)
            return value, True

        last_error: Exception | None = None
        limit = max(1, attempts if attempts is not None else self.max_attempts)
        for attempt in range(1, limit + 1):
            envelope.attempt = attempt
            agent = FunctionModuleAgent(
                name=stage,
                role=role,
                worker=FunctionWorker(f"{stage}-worker", producer),
                verifier=FunctionVerifier(reviewed_by, kind, validator),
                backend=self.backend,
                writer=(lambda value: atomic_write_json(output_path, value)) if kind == "json" else (lambda value: _atomic_write_text(output_path, str(value))),
                output_path=output_path,
                schema_version=schema_version,
                status_resolver=status_resolver,
            )
            try:
                result = agent.run(envelope)
                if use_stage_cache:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    if kind == "json":
                        atomic_write_json(cache_path, json.loads(output_path.read_text(encoding="utf-8")))
                    else:
                        _atomic_write_text(cache_path, output_path.read_text(encoding="utf-8"))
                    atomic_write_json(cache_manifest, {
                        "stage_version": stage_version,
                        "schema_version": schema_version,
                        "input_hashes": semantic_input_hashes,
                        "artifact_sha256": sha256_file(cache_path),
                    })
                value = json.loads(output_path.read_text(encoding="utf-8")) if kind == "json" else output_path.read_text(encoding="utf-8")
                observed_cache_hit = bool(value.get("cache_hit")) if isinstance(value, Mapping) else False
                self._record(stage, envelope, result, cache_hit=observed_cache_hit, stage_version=stage_version)
                return value, observed_cache_hit
            except Exception as exc:
                last_error = exc
                retryable = isinstance(exc, retryable_exceptions)
                failure = TaskResult(
                    status="retryable_error" if retryable and attempt < limit else "fatal_error",
                    checks={"execution": {"status": "failed", "error_type": type(exc).__name__}},
                    retry_hint="retry with exponential backoff" if retryable and attempt < limit else None,
                    agent_role=role,
                    reviewed_by=reviewed_by,
                )
                atomic_write_json(self.paper_dir / "attempts" / stage / f"attempt-{attempt:03d}.json", {
                    "stage_version": stage_version,
                    "task": envelope.to_dict(),
                    "result": failure.to_dict(),
                })
                if retryable and attempt < limit:
                    time.sleep(min(0.1 * 2 ** (attempt - 1), 1.0))
                else:
                    break
        raise RuntimeError(f"{stage} failed after {limit} attempts ({type(last_error).__name__})") from last_error
