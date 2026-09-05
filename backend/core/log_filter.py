"""日志过滤器 - 精简和中文化日志输出"""
import logging
import re


class SimplifiedLogFilter(logging.Filter):
    """精简日志过滤器 - 移除冗余信息"""

    # 需要完全过滤的日志模式
    SKIP_PATTERNS = [
        r"HTTP Request: POST https://chat\.qwen\.ai",
        r"HTTP Request: GET https://chat\.qwen\.ai",
        r"HTTP Request: DELETE https://chat\.qwen\.ai",
        r"\[ToolParse\].*原始回复",
        r"prompt preview \(first \d+ chars\)",
        r"feature_config:",
        r"prompt contains ##TOOL_CALL##",
    ]

    # 需要简化的日志模式（保留但精简）
    SIMPLIFY_PATTERNS = {
        r"\[SessionPlan\] surface=\S+ enabled=(\S+) reuse_chat=(\S+) reason=(\S+).*":
            r"会话计划: 启用=\1 复用=\2 原因=\3",

        r"\[Executor\] acquired account=(\S+) model=(\S+) attempt=(\d+)":
            r"获取账号: \1 模型=\2 尝试=\3",

        r"\[Executor\] created chat_id=(\S+) account=(\S+)":
            r"创建会话: \1 账号=\2",

        r"\[Executor\] stream start chat_id=(\S+) model=(\S+)":
            r"开始流式: 会话=\1 模型=\2",

        r"\[Executor\] stream finish chat_id=(\S+) total=([\d.]+)s":
            r"完成流式: 会话=\1 耗时=\2秒",

        r"\[Executor\] first parsed event after ([\d.]+)s chat_id=(\S+)":
            r"首次响应: \1秒 会话=\2",

        r"\[Collect\] ✓ Tool Sieve 刷新检测到工具调用: tools=\[([^\]]+)\]":
            r"检测到工具: \1",

        r"\[Collect\] finalize reason=(\S+) chat_id=(\S+) tool_calls=(\d+)":
            r"完成收集: 原因=\1 工具数=\3",

        r"\[ANT\] model=(\S+), stream=(\S+), tool_enabled=(\S+)":
            r"请求配置: 模型=\1 流式=\2 工具=\3",
    }

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()

        # 检查是否需要完全过滤
        for pattern in self.SKIP_PATTERNS:
            if re.search(pattern, msg):
                return False

        # 检查是否需要简化
        for pattern, replacement in self.SIMPLIFY_PATTERNS.items():
            match = re.search(pattern, msg)
            if match:
                record.msg = re.sub(pattern, replacement, msg)
                record.args = ()
                break

        return True


class ChineseLogFilter(logging.Filter):
    """中文化日志过滤器 - 将英文日志转换为中文"""

    TRANSLATIONS = {
        # 启动/关闭
        "Starting": "启动中",
        "Shutting down": "关闭中",
        "Startup complete": "启动完成",

        # 账号相关
        "Account": "账号",
        "account": "账号",
        "token": "令牌",
        "Token": "令牌",

        # 请求相关
        "Request": "请求",
        "request": "请求",
        "Response": "响应",
        "response": "响应",

        # 模型相关
        "model": "模型",
        "Model": "模型",

        # 工具相关
        "tool": "工具",
        "Tool": "工具",
        "tools": "工具",
        "Tools": "工具",

        # 状态
        "success": "成功",
        "Success": "成功",
        "failed": "失败",
        "Failed": "失败",
        "error": "错误",
        "Error": "错误",
        "warning": "警告",
        "Warning": "警告",

        # 动作
        "created": "已创建",
        "Created": "已创建",
        "deleted": "已删除",
        "Deleted": "已删除",
        "updated": "已更新",
        "Updated": "已更新",
    }

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()

        # 简单替换（仅替换独立单词）
        for en, zh in self.TRANSLATIONS.items():
            msg = re.sub(rf'\b{en}\b', zh, msg)

        record.msg = msg
        record.args = ()
        return True


def apply_log_filters(logger: logging.Logger) -> None:
    """应用日志过滤器到指定 logger"""
    logger.addFilter(SimplifiedLogFilter())
    # logger.addFilter(ChineseLogFilter())  # 可选：启用中文化
