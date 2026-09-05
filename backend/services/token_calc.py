import tiktoken
import logging

log = logging.getLogger("qwen2api.token")

try:
    # 默认使用 cl100k_base，因为目前这是最通用的 GPT-4 级分词器
    encoder = tiktoken.get_encoding("cl100k_base")
except Exception as e:
    log.warning(f"Failed to load tiktoken: {e}")
    encoder = None

def count_tokens(text: str) -> int:
    """计算文本的精确 Token 数"""
    if not text:
        return 0
    if encoder:
        try:
            return len(encoder.encode(text))
        except Exception:
            pass
    # Fallback：每汉字 1 token，每 3 个英文字母 1 token 的粗略估算
    return max(1, len(text.encode('utf-8')) // 2)

def calculate_usage(prompt: str, completion: str) -> dict:
    """结算：精确扣费"""
    prompt_tokens = count_tokens(prompt)
    completion_tokens = count_tokens(completion)
    total_tokens = prompt_tokens + completion_tokens
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens
    }
