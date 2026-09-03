"""LLM-backed narrative and metadata enrichment for a validated content plan."""

from __future__ import annotations

import copy
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping


class NarrativePlanningError(RuntimeError):
    """Raised when the narrative agent cannot return a complete grounded plan."""


def _endpoint(base_url: str, suffix: str) -> str:
    base = str(base_url or "https://api.openai.com/v1").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (parsed.scheme != "https" and not local_http) or not parsed.netloc or parsed.username or parsed.password:
        raise NarrativePlanningError("LLM Endpoint 必须是有效的 HTTPS URL")
    for known in ("/responses", "/chat/completions"):
        if parsed.path.rstrip("/").endswith(known):
            base = base[: -len(known)]
            break
    return f"{base}/{suffix.lstrip('/')}"


def _response_text(payload: Mapping[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    for output in payload.get("output", []):
        if not isinstance(output, Mapping):
            continue
        for content in output.get("content", []):
            if isinstance(content, Mapping) and content.get("type") == "output_text":
                return str(content.get("text") or "")
    choices = payload.get("choices", [])
    if choices and isinstance(choices[0], Mapping):
        message = choices[0].get("message", {})
        if isinstance(message, Mapping):
            content = message.get("content")
            if isinstance(content, str):
                return content
    return ""


def _post(url: str, api_key: str, body: Mapping[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=240) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise NarrativePlanningError("LLM 返回格式无效")
    return value


def _call_structured(
    *, api_key: str, model: str, base_url: str, prompt: str,
    schema: Mapping[str, Any], max_attempts: int,
) -> dict[str, Any]:
    system = (
        "你是学术论文内容设计智能体。你的任务是基于给定证据写中文解释，而不是展示、翻译或拼接原文摘录。"
        "不得补充证据中没有的事实、数字或因果关系；不确定的信息留空。"
    )
    last_error: Exception | None = None
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            responses_body = {
                "model": model,
                "input": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
                ],
                "text": {"format": {"type": "json_schema", "name": "paper_narrative", "strict": True, "schema": schema}},
            }
            try:
                payload = _post(_endpoint(base_url, "responses"), api_key, responses_body)
            except urllib.error.HTTPError as error:
                if error.code not in {400, 404, 405, 422}:
                    raise
                chat_body = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                    "response_format": {"type": "json_schema", "json_schema": {"name": "paper_narrative", "strict": True, "schema": schema}},
                }
                payload = _post(_endpoint(base_url, "chat/completions"), api_key, chat_body)
            text = _response_text(payload)
            if not text:
                raise NarrativePlanningError("LLM 未返回结构化内容")
            result = json.loads(text)
            if not isinstance(result, dict):
                raise NarrativePlanningError("LLM 返回内容不是对象")
            return result
        except urllib.error.HTTPError as error:
            last_error = NarrativePlanningError(f"LLM API 返回 HTTP {error.code}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, NarrativePlanningError) as error:
            last_error = error
        if attempt < max_attempts:
            time.sleep(min(2 ** (attempt - 1), 4))
    raise NarrativePlanningError(f"内容设计智能体失败：{last_error}")


def _input_payload(ir: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[str, Any]:
    evidence = {str(item["id"]): str(item.get("verbatim_text") or "") for item in ir.get("evidence", [])}
    first_page_blocks = []
    for page in ir.get("pages", [])[:2]:
        for block in page.get("blocks", [])[:60]:
            text = " ".join(str(block.get("text") or "").split())
            if text:
                first_page_blocks.append({"page": page.get("number"), "role": block.get("role"), "text": text[:500]})
    narrative_items = []
    for item in plan.get("items", []):
        if item.get("content_type") != "narrative":
            continue
        narrative_items.append({
            "id": item["id"], "section": item["primary_section"], "current_title": item["title"],
            "evidence": [evidence[evidence_id] for evidence_id in item.get("evidence_ids", []) if evidence_id in evidence],
        })
    return {
        "paper": ir.get("paper", {}),
        "first_pages": first_page_blocks,
        "narrative_items": narrative_items,
    }


def _schema(item_ids: list[str]) -> dict[str, Any]:
    nullable_string = {"type": ["string", "null"]}
    return {
        "type": "object",
        "properties": {
            "metadata": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "authors": {"type": "array", "items": {"type": "string"}},
                    "affiliations": {"type": "array", "items": {"type": "string"}},
                    "venue": nullable_string,
                    "published_at": nullable_string,
                    "doi": nullable_string,
                    "arxiv_id": nullable_string,
                },
                "required": ["title", "authors", "affiliations", "venue", "published_at", "doi", "arxiv_id"],
                "additionalProperties": False,
            },
            "items": {
                "type": "array",
                "minItems": len(item_ids), "maxItems": len(item_ids),
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "enum": item_ids},
                        "title": {"type": "string", "minLength": 2, "maxLength": 48},
                        "body": {"type": "string", "minLength": 8, "maxLength": 600},
                    },
                    "required": ["id", "title", "body"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["metadata", "items"],
        "additionalProperties": False,
    }


def enrich_content_plan(
    ir: Mapping[str, Any], plan: Mapping[str, Any], *, api_key: str, model: str,
    base_url: str = "https://api.openai.com/v1", max_attempts: int = 3,
) -> dict[str, Any]:
    """Turn source-bound claim slots into a concise Chinese paper story."""

    payload = _input_payload(ir, plan)
    item_ids = [str(item["id"]) for item in payload["narrative_items"]]
    if not item_ids:
        return copy.deepcopy(dict(plan))
    prompt = (
        "请完成论文 Visualizer 的栏目内容。栏目已经固定，不要改变栏目。"
        "对每个 narrative item 写一个清晰标题和一段中文解释：一分钟速读项目写 1—2 句，其他项目写 2—4 句。"
        "解释其作用、逻辑或实验意义，不要逐句翻译，不要使用引号，不要出现“摘录”“生成式总结”等标签。"
        "每个 item 只能使用它自己的 evidence。metadata 只从 paper 与 first_pages 提取，没有的信息返回空数组或 null。"
        "必须原样返回所有 item id，不能增删。输入如下：\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )
    result = _call_structured(
        api_key=api_key, model=model, base_url=base_url, prompt=prompt,
        schema=_schema(item_ids), max_attempts=max_attempts,
    )
    patches = result.get("items", [])
    by_id = {str(item.get("id")): item for item in patches if isinstance(item, Mapping)}
    if set(by_id) != set(item_ids) or len(patches) != len(item_ids):
        raise NarrativePlanningError("内容设计智能体没有返回全部栏目内容")
    enriched = copy.deepcopy(dict(plan))
    for item in enriched.get("items", []):
        if item.get("content_type") != "narrative":
            continue
        patch = by_id[item["id"]]
        title = " ".join(str(patch.get("title") or "").split()).strip()
        body = " ".join(str(patch.get("body") or "").split()).strip()
        if not title or not body or "原文摘录" in title + body:
            raise NarrativePlanningError(f"内容设计智能体返回了无效栏目内容：{item['id']}")
        item.update({"title": title, "body": body, "generation": "llm"})
    metadata = result.get("metadata", {})
    if isinstance(metadata, Mapping):
        current = enriched["paper_metadata"]
        for key in ("title", "venue", "published_at", "doi", "arxiv_id"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                current[key] = " ".join(value.split()).strip()
        for key in ("authors", "affiliations"):
            values = metadata.get(key)
            if isinstance(values, list):
                clean = [" ".join(str(value).split()).strip() for value in values if str(value).strip()]
                if clean:
                    current[key] = list(dict.fromkeys(clean))
        if not current.get("published_at"):
            first_page_text = " ".join(item["text"] for item in payload.get("first_pages", []))
            year_match = re.search(r"\b(?:19|20)\d{2}\b", first_page_text)
            if year_match:
                current["published_at"] = year_match.group(0)
    return enriched
