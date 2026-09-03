from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from paper_visualizer.artifacts import sha256_file
from paper_visualizer.orchestration.runner import RetryableStageError, StageRunner, SubprocessExecutionBackend


def test_stage_runner_caches_and_records_protocol(tmp_path: Path):
    output = tmp_path / "artifacts/paper/plans/value.json"
    runner = StageRunner(paper_id="paper", paper_dir=tmp_path / "artifacts/paper", cache_dir=tmp_path / "cache", max_attempts=3, force=False)
    calls = 0

    def produce(task):
        nonlocal calls
        calls += 1
        return {"schema_version": "1.0.0", "value": 7}

    value, cache_hit = runner.run(
        stage="content", stage_version="2.0.0", role="content-lead", reviewed_by="content-verifier",
        inputs={"ir": {"value": 1}}, output_path=output, producer=produce,
    )
    assert value["value"] == 7 and cache_hit is False
    first_record = json.loads((tmp_path / "artifacts/paper/orchestration/content.json").read_text(encoding="utf-8"))
    assert first_record["result"]["checks"]["worker_invoked"]["worker"] == "content-worker"
    assert first_record["result"]["checks"]["worker_output_type"]["verified_by"] == "content-verifier"
    value, cache_hit = runner.run(
        stage="content", stage_version="2.0.0", role="content-lead", reviewed_by="content-verifier",
        inputs={"ir": {"value": 1}}, output_path=output, producer=produce,
    )
    assert value["value"] == 7 and cache_hit is True and calls == 1
    record = json.loads((tmp_path / "artifacts/paper/orchestration/content.json").read_text(encoding="utf-8"))
    assert record["task"]["module"] == "content"
    assert record["result"]["agent_role"] == "content-lead"
    assert record["result"]["reviewed_by"] == "content-verifier"
    assert record["result"]["checks"]["cache"]["cache_hit"] is True


def test_stage_runner_retries_and_persists_each_failed_attempt(tmp_path: Path):
    runner = StageRunner(paper_id="paper", paper_dir=tmp_path / "artifacts/paper", cache_dir=tmp_path / "cache", max_attempts=3, force=False)
    attempts = 0

    def flaky(task):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RetryableStageError("transient")
        return {"schema_version": "1.0.0", "attempt": task.attempt}

    value, _ = runner.run(
        stage="model", stage_version="1.0.0", role="model-lead", reviewed_by="model-verifier",
        inputs={"parsed": {"value": 1}}, output_path=tmp_path / "artifacts/paper/ir/paper_ir.json", producer=flaky,
    )
    assert value["attempt"] == 3
    failures = sorted((tmp_path / "artifacts/paper/attempts/model").glob("attempt-*.json"))
    assert len(failures) == 2
    assert json.loads(failures[0].read_text(encoding="utf-8"))["result"]["status"] == "retryable_error"


def test_stage_runner_preserves_previous_output_after_terminal_failure(tmp_path: Path):
    output = tmp_path / "artifacts/paper/plans/value.json"
    output.parent.mkdir(parents=True)
    output.write_text('{"previous": true}\n', encoding="utf-8")
    runner = StageRunner(paper_id="paper", paper_dir=tmp_path / "artifacts/paper", cache_dir=tmp_path / "cache", max_attempts=2, force=False)

    with pytest.raises(RuntimeError, match="failed after 2 attempts"):
        runner.run(
            stage="visual", stage_version="1.0.0", role="visual-lead", reviewed_by="visual-verifier",
            inputs={"content": {"value": 1}}, output_path=output,
            producer=lambda task: (_ for _ in ()).throw(ValueError("permanent")),
        )

    assert json.loads(output.read_text(encoding="utf-8")) == {"previous": True}
    final = json.loads((tmp_path / "artifacts/paper/attempts/visual/attempt-001.json").read_text(encoding="utf-8"))
    assert final["result"]["status"] == "fatal_error"
    assert not (tmp_path / "artifacts/paper/attempts/visual/attempt-002.json").exists()


def test_corrupt_cache_is_rebuilt(tmp_path: Path):
    runner = StageRunner(paper_id="paper", paper_dir=tmp_path / "artifacts/paper", cache_dir=tmp_path / "cache", max_attempts=2, force=False)
    output = tmp_path / "artifacts/paper/plans/value.json"
    calls = 0

    def produce(task):
        nonlocal calls
        calls += 1
        return {"schema_version": "1.0.0", "value": calls}

    runner.run(stage="page", stage_version="1.0.0", role="page-lead", reviewed_by="page-verifier", inputs={"x": 1}, output_path=output, producer=produce)
    cache_artifact = next((tmp_path / "cache/page/1.0.0").rglob("artifact.json"))
    cache_artifact.write_text('{"corrupt": true}\n', encoding="utf-8")
    value, cache_hit = runner.run(stage="page", stage_version="1.0.0", role="page-lead", reviewed_by="page-verifier", inputs={"x": 1}, output_path=output, producer=produce)
    assert cache_hit is False and value["value"] == 2 and calls == 2
    cache_artifact.write_text('{"schema_version": "9.0.0", "value": 99}\n', encoding="utf-8")
    cache_manifest = cache_artifact.parent / "manifest.json"
    metadata = json.loads(cache_manifest.read_text(encoding="utf-8"))
    metadata["artifact_sha256"] = sha256_file(cache_artifact)
    cache_manifest.write_text(json.dumps(metadata), encoding="utf-8")
    value, cache_hit = runner.run(stage="page", stage_version="1.0.0", role="page-lead", reviewed_by="page-verifier", inputs={"x": 1}, output_path=output, producer=produce)
    assert cache_hit is False and value["value"] == 3 and calls == 3


def test_subprocess_backend_runs_distinct_worker_and_verifier(tmp_path: Path):
    worker = [sys.executable, "-c", "import json,sys; json.load(sys.stdin); print(json.dumps({'agent_identity':'worker-process','value':{'schema_version':'1.0.0','value':11}}))"]
    verifier = [sys.executable, "-c", "import json,sys; json.load(sys.stdin); print(json.dumps({'agent_identity':'verifier-process','approved':True,'checks':{'contract':'passed'}}))"]
    backend = SubprocessExecutionBackend(worker_command=worker, verifier_command=verifier)
    runner = StageRunner(paper_id="paper", paper_dir=tmp_path / "artifacts/paper", cache_dir=tmp_path / "cache", max_attempts=1, force=False, backend=backend)
    output = tmp_path / "artifacts/paper/plans/value.json"
    value, cache_hit = runner.run(
        stage="model", stage_version="1.0.0", role="model-lead", reviewed_by="model-verifier",
        inputs={"parsed": {"value": 1}}, output_path=output,
        producer=lambda task: (_ for _ in ()).throw(AssertionError("local producer must not run")),
    )
    assert value["value"] == 11 and cache_hit is False
    record = json.loads((tmp_path / "artifacts/paper/orchestration/model.json").read_text(encoding="utf-8"))
    evidence = record["result"]["checks"]["external_verifier"]
    assert evidence["agent_identity"] == "verifier-process"
    assert evidence["checks"] == {"contract": "passed"}
