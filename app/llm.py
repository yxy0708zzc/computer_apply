"""
llm.py —— OpenAI 兼容流式客户端（吸收 NVIDIA NIM 兼容性经验）

- 流式读超时 120s（慢推理模型非流式会超时，本系统全流式）
- 流式响应中 usage 块等空 choices chunk 跳过
- 思考链走 delta.reasoning_content（单独事件，不回传 messages）
- 错误体兼容 OpenAI {"error":{...}} 与 NIM {"detail":...} 两种形态
- 401/400 快速失败不重试；429/5xx/网络异常指数退避重试 2 次
"""

import json
import time
from typing import Callable, Dict, Generator, List, Optional

import requests

STREAM_TIMEOUT = (10, 120)      # (连接, 读)
RETRY_DELAYS = (2, 6)


class LLMError(Exception):
    """LLM 调用失败（已带用户可读信息）"""


def _extract_error_message(resp: requests.Response) -> str:
    try:
        data = resp.json()
    except Exception:
        return f"HTTP {resp.status_code}: {resp.text[:200]}"
    # OpenAI 形态 {"error": {"message": ...}} / NIM 形态 {"detail": ...}
    err = data.get("error")
    if isinstance(err, dict) and err.get("message"):
        return f"HTTP {resp.status_code}: {err['message']}"
    if isinstance(err, str):
        return f"HTTP {resp.status_code}: {err}"
    if data.get("detail"):
        return f"HTTP {resp.status_code}: {data['detail']}"
    return f"HTTP {resp.status_code}: {json.dumps(data, ensure_ascii=False)[:300]}"


def stream_chat(api_key: str, base_url: str, model: str, messages: List[Dict],
                tools: Optional[List[Dict]] = None,
                temperature: float = 0.7,
                on_raw: Optional[Callable[[Dict], None]] = None
                ) -> Generator[Dict, None, None]:
    """流式对话。yield 事件：
      {"type": "thinking", "delta": str}
      {"type": "content",  "delta": str}
      {"type": "tool_calls", "calls": [{index,id,name,arguments}]}   # 结束时一次性给出聚合结果
      {"type": "finish", "reason": str}
      {"type": "usage",  "prompt_tokens": int, "completion_tokens": int}
    失败抛 LLMError。
    """
    url = base_url.rstrip("/") + "/chat/completions"
    body: Dict = {
        "model": model, "messages": messages, "temperature": temperature, "stream": True,
        "stream_options": {"include_usage": True},
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    last_err = ""
    for attempt in range(1, 4):
        try:
            resp = requests.post(url, headers=headers, json=body,
                                 timeout=STREAM_TIMEOUT, stream=True)
        except requests.RequestException as e:
            last_err = f"连接失败: {e}"
            if attempt < 3:
                time.sleep(RETRY_DELAYS[attempt - 1])
            continue

        if resp.status_code != 200:
            msg = _extract_error_message(resp)
            # 认证/参数类错误快速失败，不重试
            if resp.status_code in (400, 401, 403, 404):
                raise LLMError(msg)
            last_err = msg
            if attempt < 3:
                time.sleep(RETRY_DELAYS[attempt - 1])
            continue

        # ---- 流式解析 ----
        calls_by_index: Dict[int, Dict] = {}
        finish_reason = None
        usage = None
        try:
            for line in resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if on_raw:
                    try:
                        on_raw(chunk)
                    except Exception:
                        pass
                if chunk.get("usage"):
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue    # usage 块等空 chunk（NIM 经验）
                ch = choices[0]
                delta = ch.get("delta") or {}
                reasoning = delta.get("reasoning_content")
                if reasoning:
                    yield {"type": "thinking", "delta": reasoning}
                content = delta.get("content")
                if content:
                    yield {"type": "content", "delta": content}
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    slot = calls_by_index.setdefault(
                        idx, {"index": idx, "id": "", "name": "", "arguments": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]     # 增量名覆盖为全名
                    if fn.get("arguments"):
                        slot["arguments"] += fn["arguments"]
                if ch.get("finish_reason"):
                    finish_reason = ch["finish_reason"]
        except requests.RequestException as e:
            last_err = f"流中断: {e}"
            if attempt < 3:
                time.sleep(RETRY_DELAYS[attempt - 1])
            continue
        finally:
            try:
                resp.close()
            except Exception:
                pass

        if calls_by_index:
            yield {"type": "tool_calls",
                   "calls": sorted(calls_by_index.values(), key=lambda c: c["index"])}
        if finish_reason:
            yield {"type": "finish", "reason": finish_reason}
        if usage:
            yield {"type": "usage",
                   "prompt_tokens": usage.get("prompt_tokens") or 0,
                   "completion_tokens": usage.get("completion_tokens") or 0}
        return

    raise LLMError(last_err or "LLM 请求失败（重试耗尽）")
