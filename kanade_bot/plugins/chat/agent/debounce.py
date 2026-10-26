"""消息防抖：把连续群消息收束成「批」，等说话人把话说完再触发后续流程

纯内存状态机：只负责批次划分与到期时刻计算，到期唤醒由调用方安排定时任务完成。
"""

from dataclasses import dataclass
from time import monotonic

QUIET_GAP = 30.0
"""安静阈值：距上一条消息超过该秒数，视为群聊已安静"""

OPEN_QUIET_WAIT = 4.0
"""安静开批等待：安静后新批次的第一条消息，稍等确认没有后续"""

OPEN_ACTIVE_WAIT = 7.0
"""活跃开批等待：安静阈值内有过消息时，新批次首条后等更久（消息可能还在路上）"""

SAME_AUTHOR_WAIT = 10.0
"""同人续发等待：批内同一位作者继续说话时，等他把话说完"""

NEW_AUTHOR_WAIT = 10.0
"""换人续发等待：批内换人发言时更快收批（多人在场对话节奏快）"""

BATCH_MAX_WAIT = 20.0
"""整批上限：防抖单批最长等待秒数，防止批次被无限拖长"""


@dataclass
class _Batch:
    """一个进行中的消息批"""

    first_at: float
    """批次首条消息时刻（monotonic 时钟）"""

    due_at: float
    """本批当前到期时刻，被 `BATCH_MAX_WAIT` 以首条时刻钳制"""

    last_author: str
    """批内最近一条消息的作者标识"""

    count: int
    """批内消息条数"""


class MessageDebouncer:
    """会话级消息防抖器

    语义要点：

    - **到期不被新消息延长**：`deadline()` 一到，批次即视为收束（调用方 consume）；
      到期时刻之后到达的消息属于下一批。批内新消息只会重算等待窗口，且始终不超过
      首条时刻 + `BATCH_MAX_WAIT`。
    - **安静/活跃开批**：新批次第一条按群聊是否安静（30s 内有无消息）选择
      开批等待：安静 `OPEN_QUIET_WAIT`、活跃 `OPEN_ACTIVE_WAIT`
      （活跃时消息可能成串在路上）。
    - 时间基准为 `time.monotonic()`，不受系统时钟跳变影响。
    """

    def __init__(self) -> None:
        self._batch: _Batch | None = None
        self._last_message_at: float | None = None

    def observe(self, author: str, *, now: float | None = None) -> None:
        """记录一条消息：开新批或推进当前批的到期时刻

        :param author: 消息作者标识（用户 ID），用于同人/换人判定
        :param now: monotonic 时间戳，默认取当前值（测试可注入）
        """
        now = monotonic() if now is None else now
        if self._batch is None:
            quiet = self._last_message_at is None or now - self._last_message_at >= QUIET_GAP
            wait = OPEN_QUIET_WAIT if quiet else OPEN_ACTIVE_WAIT
            self._batch = _Batch(first_at=now, due_at=now + wait, last_author=author, count=1)
        else:
            wait = SAME_AUTHOR_WAIT if author == self._batch.last_author else NEW_AUTHOR_WAIT
            self._batch.due_at = min(now + wait, self._batch.first_at + BATCH_MAX_WAIT)
            self._batch.last_author = author
            self._batch.count += 1
        self._last_message_at = now

    def consume(self) -> None:
        """收束当前批次：之后到达的消息开新批"""
        self._batch = None

    @property
    def batch_size(self) -> int:
        """当前批内消息条数，无批次时为 0"""
        return self._batch.count if self._batch else 0

    def deadline(self) -> float | None:
        """当前批次到期时刻（monotonic 时钟），无批次时返回 None"""
        return self._batch.due_at if self._batch else None
