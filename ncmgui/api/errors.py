"""网易云接口的错误类型。

页面不应该认识 ``code=301`` 这类数字：把服务端语义翻译成异常，界面只处理
「要登录」「被限流」「接口不支持」这几类情况。接口漂移时也只改这一层。
"""

from __future__ import annotations


class ApiError(RuntimeError):
    """所有接口错误的基类。消息可以直接显示给用户。"""

    #: 服务端返回的 ``code``；没有时为 0
    code: int = 0

    def __init__(self, message: str, code: int = 0) -> None:
        super().__init__(message)
        self.code = int(code or 0)

    @property
    def retryable(self) -> bool:
        """重试是否有意义。"""
        return False


class AuthRequired(ApiError):
    """未登录或登录已过期。"""

    @property
    def retryable(self) -> bool:
        return False


class RateLimited(ApiError):
    """请求过快或触发风控，可以稍后再试。"""

    @property
    def retryable(self) -> bool:
        return True


class RequestCancelled(ApiError):
    """调用方在请求真正发出前撤销了它（换号、关闭同步、退出等）。

    队列里已经取出、但还排在限流退避/写锁后面的任务会收到这个错误；
    发送方应当静默丢弃，不当成接口失败。
    """


class AntiCheatRequired(ApiError):
    """写操作被风控拦截，通常需要网页端完成。"""


class UnsupportedEndpoint(ApiError):
    """当前版本没有可用的接口路径。"""


class ApiChanged(ApiError):
    """响应结构不符合预期，接口可能已经改版。"""


class NetworkError(ApiError):
    """网络、代理、TLS 或超时问题。"""

    @property
    def retryable(self) -> bool:
        return True
