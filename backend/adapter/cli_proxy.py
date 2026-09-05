"""
CLIProxy - 协议转换代理层
统一处理 OpenAI/Claude/Gemini 协议到 StandardRequest 的转换
"""
import logging
from typing import Any

from backend.adapter.standard_request import StandardRequest, CLAUDE_CODE_OPENAI_PROFILE
from backend.core.config import resolve_model
from backend.services.prompt_builder import messages_to_prompt
from backend.toolcall.normalize import build_tool_name_registry

log = logging.getLogger("qwen2api.cli_proxy")


class CLIProxy:
    """
    协议转换代理 - 类似 ds2api 的 CLIProxy
    负责将不同协议（OpenAI/Claude/Gemini）转换为统一的 StandardRequest
    """

    @staticmethod
    def from_openai(req_data: dict, *, client_profile: str = CLAUDE_CODE_OPENAI_PROFILE) -> StandardRequest:
        """
        OpenAI 协议 -> StandardRequest

        Args:
            req_data: OpenAI 格式的请求体
            client_profile: 客户端配置文件

        Returns:
            StandardRequest: 统一的标准请求对象
        """
        model_name = req_data.get("model", "gpt-4o")
        prompt_result = messages_to_prompt(req_data, client_profile=client_profile)

        tools = prompt_result.tools
        tool_names = [
            tool_name
            for tool_name in (tool.get("name") for tool in tools)
            if isinstance(tool_name, str) and tool_name
        ]

        return StandardRequest(
            prompt=prompt_result.prompt,
            response_model=model_name,
            resolved_model=resolve_model(model_name),
            surface="openai",
            client_profile=client_profile,
            requested_model=model_name,
            stream=req_data.get("stream", False),
            tools=tools,
            tool_names=tool_names,
            tool_name_registry=build_tool_name_registry(tool_names),
            tool_enabled=prompt_result.tool_enabled,
        )

    @staticmethod
    def from_anthropic(req_data: dict, *, client_profile: str = CLAUDE_CODE_OPENAI_PROFILE) -> StandardRequest:
        """
        Anthropic Claude 协议 -> StandardRequest

        Args:
            req_data: Claude 格式的请求体
            client_profile: 客户端配置文件

        Returns:
            StandardRequest: 统一的标准请求对象
        """
        model_name = req_data.get("model", "claude-3-5-sonnet")
        prompt_result = messages_to_prompt(req_data, client_profile=client_profile)

        tools = prompt_result.tools
        tool_names = [
            tool_name
            for tool_name in (tool.get("name") for tool in tools)
            if isinstance(tool_name, str) and tool_name
        ]

        return StandardRequest(
            prompt=prompt_result.prompt,
            response_model=model_name,
            resolved_model=resolve_model(model_name),
            surface="anthropic",
            client_profile=client_profile,
            requested_model=model_name,
            stream=req_data.get("stream", False),
            tools=tools,
            tool_names=tool_names,
            tool_name_registry=build_tool_name_registry(tool_names),
            tool_enabled=prompt_result.tool_enabled,
        )

    @staticmethod
    def from_gemini(model: str, req_data: dict, *, stream: bool | None = None) -> StandardRequest:
        """
        Google Gemini 协议 -> StandardRequest

        Args:
            model: Gemini 模型名称
            req_data: Gemini 格式的请求体
            stream: 是否流式输出（None 则从请求体推断）

        Returns:
            StandardRequest: 统一的标准请求对象
        """
        prompt = CLIProxy._extract_gemini_prompt(req_data)
        stream_requested = CLIProxy._is_gemini_stream_request(req_data) if stream is None else stream

        # Gemini 暂不支持工具调用，后续可扩展
        tools = []
        tool_names = []

        return StandardRequest(
            prompt=prompt,
            response_model=model,
            resolved_model=resolve_model(model),
            surface="gemini",
            requested_model=model,
            content=prompt,
            stream=stream_requested,
            tools=tools,
            tool_names=tool_names,
            tool_name_registry={},
            tool_enabled=False,
        )

    @staticmethod
    def _extract_gemini_prompt(body: dict) -> str:
        """从 Gemini 请求体中提取 prompt"""
        lines: list[str] = []
        for message in body.get("contents", []) or []:
            if message.get("role") != "user":
                continue
            for part in message.get("parts", []) or []:
                text = part.get("text")
                if text:
                    lines.append(text)
        return "\n".join(lines)

    @staticmethod
    def _is_gemini_stream_request(body: dict[str, Any]) -> bool:
        """判断 Gemini 请求是否为流式"""
        if body.get("stream") is True:
            return True
        generation_config = body.get("generationConfig")
        if isinstance(generation_config, dict) and generation_config.get("stream") is True:
            return True
        return False

    @staticmethod
    def to_openai_response(execution, standard_request: StandardRequest) -> dict:
        """
        StandardRequest 执行结果 -> OpenAI 响应格式

        Args:
            execution: 执行结果对象
            standard_request: 原始标准请求

        Returns:
            dict: OpenAI 格式的响应
        """
        return {
            "id": f"chatcmpl-{execution.chat_id[:12]}",
            "object": "chat.completion",
            "created": int(execution.state.created_at or 0),
            "model": standard_request.response_model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": execution.state.answer_text,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": len(standard_request.prompt),
                "completion_tokens": len(execution.state.answer_text),
                "total_tokens": len(standard_request.prompt) + len(execution.state.answer_text),
            },
        }

    @staticmethod
    def to_anthropic_response(execution, standard_request: StandardRequest, msg_id: str, directive) -> dict:
        """
        StandardRequest 执行结果 -> Anthropic Claude 响应格式

        Args:
            execution: 执行结果对象
            standard_request: 原始标准请求
            msg_id: 消息 ID
            directive: 工具调用指令

        Returns:
            dict: Claude 格式的响应
        """
        content_blocks: list[dict] = []

        # 添加思考内容
        if execution.state.reasoning_text:
            content_blocks.append({"type": "thinking", "thinking": execution.state.reasoning_text})

        # 添加工具调用块
        content_blocks.extend(directive.tool_blocks)

        return {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "model": standard_request.response_model,
            "content": content_blocks,
            "stop_reason": directive.stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": len(standard_request.prompt),
                "output_tokens": len(execution.state.answer_text),
            },
        }

    @staticmethod
    def to_gemini_response(execution, standard_request: StandardRequest) -> dict:
        """
        StandardRequest 执行结果 -> Google Gemini 响应格式

        Args:
            execution: 执行结果对象
            standard_request: 原始标准请求

        Returns:
            dict: Gemini 格式的响应
        """
        return {
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": execution.state.answer_text}],
                        "role": "model",
                    },
                    "finishReason": "STOP",
                    "index": 0,
                }
            ],
            "usageMetadata": {
                "promptTokenCount": len(standard_request.prompt),
                "candidatesTokenCount": len(execution.state.answer_text),
                "totalTokenCount": len(standard_request.prompt) + len(execution.state.answer_text),
            },
        }

    @staticmethod
    def log_conversion(surface: str, model: str, prompt_len: int, tool_count: int):
        """记录协议转换日志"""
        log.info(
            f"[CLIProxy] {surface.upper()} -> StandardRequest: "
            f"model={model}, prompt_len={prompt_len}, tools={tool_count}"
        )
