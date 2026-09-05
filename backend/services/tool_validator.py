"""
工具参数验证和修正模块
确保通义千问生成的工具调用参数符合 Claude Code 的要求
"""
import logging
from typing import Any

log = logging.getLogger("qwen2api.tool_validator")


def validate_and_fix_tool_call(tool_name: str, tool_input: dict) -> dict:
    """
    验证并修正工具调用参数

    Args:
        tool_name: 工具名称
        tool_input: 工具输入参数

    Returns:
        修正后的工具输入参数
    """
    if tool_name == "AskUserQuestion":
        return _fix_ask_user_question(tool_input)
    elif tool_name == "Agent":
        return _fix_agent(tool_input)
    elif tool_name == "Read":
        return _fix_read(tool_input)
    elif tool_name == "Bash":
        return _fix_bash(tool_input)
    else:
        return tool_input


def _fix_ask_user_question(tool_input: dict) -> dict:
    """
    修正 AskUserQuestion 工具参数

    Claude Code 期望的格式:
    {
        "questions": [
            {
                "question": "...",
                "header": "...",
                "options": [
                    {"label": "...", "description": "..."},
                    ...
                ],
                "multiSelect": false
            }
        ]
    }
    """
    fixed = dict(tool_input)

    # 如果只有 question 字段，转换为 questions 数组
    if "question" in fixed and "questions" not in fixed:
        question_text = fixed.pop("question")
        fixed["questions"] = [{
            "question": question_text,
            "header": "Question",
            "options": [
                {"label": "Yes", "description": "Confirm"},
                {"label": "No", "description": "Decline"}
            ],
            "multiSelect": False
        }]
        log.info(f"[ToolValidator] Fixed AskUserQuestion: converted 'question' to 'questions' array")

    # 确保 questions 是数组
    if "questions" in fixed:
        if not isinstance(fixed["questions"], list):
            fixed["questions"] = [fixed["questions"]]

        # 验证每个问题的格式
        for i, q in enumerate(fixed["questions"]):
            if not isinstance(q, dict):
                continue

            # 确保有 question 字段
            if "question" not in q:
                q["question"] = "Please provide your input"

            # 确保有 header 字段
            if "header" not in q:
                q["header"] = "Question"

            # 确保有 options 字段
            if "options" not in q:
                q["options"] = [
                    {"label": "Continue", "description": "Proceed with the task"},
                    {"label": "Cancel", "description": "Stop the task"}
                ]

            # 确保 options 格式正确
            if isinstance(q.get("options"), list):
                for j, opt in enumerate(q["options"]):
                    if isinstance(opt, str):
                        # 如果 option 是字符串，转换为对象
                        q["options"][j] = {
                            "label": opt,
                            "description": opt
                        }
                    elif isinstance(opt, dict):
                        # 确保有 label 和 description
                        if "label" not in opt:
                            opt["label"] = opt.get("description", f"Option {j+1}")
                        if "description" not in opt:
                            opt["description"] = opt.get("label", "")

            # 确保有 multiSelect 字段
            if "multiSelect" not in q:
                q["multiSelect"] = False

    return fixed


def _fix_agent(tool_input: dict) -> dict:
    """
    修正 Agent 工具参数

    Claude Code 期望的格式:
    {
        "description": "...",
        "prompt": "..."
    }
    """
    fixed = dict(tool_input)

    # 确保有 description 字段
    if "description" not in fixed:
        fixed["description"] = "Execute sub-task"

    # 确保有 prompt 字段
    if "prompt" not in fixed:
        fixed["prompt"] = fixed.get("description", "Execute the task")

    return fixed


def _fix_read(tool_input: dict) -> dict:
    """
    修正 Read 工具参数

    Claude Code 期望的格式:
    {
        "file_path": "..."
    }
    """
    fixed = dict(tool_input)

    # 确保有 file_path 字段
    if "file_path" not in fixed:
        if "path" in fixed:
            fixed["file_path"] = fixed.pop("path")
        elif "filename" in fixed:
            fixed["file_path"] = fixed.pop("filename")

    return fixed


def _fix_bash(tool_input: dict) -> dict:
    """
    修正 Bash 工具参数

    Claude Code 期望的格式:
    {
        "command": "...",
        "description": "..." (optional)
    }
    """
    fixed = dict(tool_input)

    # 确保有 command 字段
    if "command" not in fixed:
        if "cmd" in fixed:
            fixed["command"] = fixed.pop("cmd")
        elif "script" in fixed:
            fixed["command"] = fixed.pop("script")

    return fixed
