"""
账号池 - 完整实现（对齐 ds2api）

将 pool_core.py 和 pool_acquire.py 合并为完整的 AccountPool
"""
from backend.core.account_pool.pool_core import Account, AccountPool as CorePool
from backend.core.account_pool.pool_acquire import AccountAcquireMixin


class AccountPool(AccountAcquireMixin, CorePool):
    """
    完整的账号池实现

    对齐 ds2api 的 4 层并发控制：
    1. max_inflight_per_account: 每账号最大并发
    2. recommended_concurrency: 推荐并发值
    3. max_queue_size: 等待队列上限
    4. global_max_inflight: 全局最大并发
    """
    pass


__all__ = ["Account", "AccountPool"]
