"""下载任务队列与历史。

只负责「记录什么、排到谁」；真正启动 ``ncmctl`` 仍由下载页走
:meth:`MainWindow.submit`，这样控制台、停止按钮与敏感输入处理都只有一套。

历史落在 ``config_dir()/downloads.json``（POSIX 下 0600）。记录里只有命令行
参数、标题与时间，不含 Cookie 或直链。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

from .config import config_dir, private_tmp_dir

#: 保留多少条历史。只留最近的一批，不让配置文件无限膨胀
HISTORY_LIMIT = 200

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

STATUS_LABELS = {
    STATUS_QUEUED: "排队中",
    STATUS_RUNNING: "下载中",
    STATUS_DONE: "已完成",
    STATUS_FAILED: "失败",
    STATUS_CANCELLED: "已取消",
}


def _now() -> float:
    return time.time()


@dataclass
class DownloadRecord:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    title: str = "下载"
    argv: list[str] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)
    output: str = ""
    status: str = STATUS_QUEUED
    created_at: float = field(default_factory=_now)
    started_at: float = 0.0
    finished_at: float = 0.0
    exit_code: int | None = None
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "argv": list(self.argv),
            "targets": list(self.targets),
            "output": self.output,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, data: object) -> "DownloadRecord | None":
        if not isinstance(data, dict):
            return None
        argv = data.get("argv")
        if not isinstance(argv, list) or not argv:
            return None
        try:
            created = float(data.get("created_at") or 0.0)
        except (TypeError, ValueError):
            created = 0.0
        status = str(data.get("status") or STATUS_QUEUED)
        if status not in STATUS_LABELS:
            status = STATUS_FAILED
        exit_code = data.get("exit_code")
        try:
            exit_code = int(exit_code) if exit_code is not None else None
        except (TypeError, ValueError):
            exit_code = None
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex),
            title=str(data.get("title") or "下载"),
            argv=[str(item) for item in argv],
            targets=[str(item) for item in (data.get("targets") or [])],
            output=str(data.get("output") or ""),
            status=status,
            created_at=created,
            started_at=float(data.get("started_at") or 0.0),
            finished_at=float(data.get("finished_at") or 0.0),
            exit_code=exit_code,
            message=str(data.get("message") or ""),
        )

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def retryable(self) -> bool:
        return self.status in (STATUS_FAILED, STATUS_CANCELLED)


class DownloadManager(QObject):
    changed = pyqtSignal()

    def __init__(self, path: Path | str | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.path = Path(path) if path else config_dir() / "downloads.json"
        self._records: list[DownloadRecord] = []
        self.load()

    # ------------------------------------------------------------ 持久化

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = []
        records = []
        if isinstance(raw, list):
            for item in raw:
                record = DownloadRecord.from_dict(item)
                if record is not None:
                    # 上次退出时还在跑的任务不可能真的在跑，标成中断
                    if record.status in (STATUS_RUNNING, STATUS_QUEUED):
                        record.status = STATUS_CANCELLED
                        record.message = record.message or "程序退出，任务未完成"
                    records.append(record)
        self._records = records[-HISTORY_LIMIT:]

    def save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp.write_text(
                json.dumps(
                    [record.to_dict() for record in self._records],
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            if os.name == "posix":
                os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------ 查询

    @property
    def records(self) -> list[DownloadRecord]:
        """最新的在前。"""
        return list(reversed(self._records))

    def get(self, record_id: str) -> DownloadRecord | None:
        for record in self._records:
            if record.id == record_id:
                return record
        return None

    def by_status(self, status: str) -> list[DownloadRecord]:
        return [record for record in self.records if record.status == status]

    def next_queued(self) -> DownloadRecord | None:
        for record in self._records:
            if record.status == STATUS_QUEUED:
                return record
        return None

    @property
    def active(self) -> DownloadRecord | None:
        for record in self._records:
            if record.status == STATUS_RUNNING:
                return record
        return None

    # ------------------------------------------------------------ 写入

    def add(
        self,
        title: str,
        argv: list[str],
        targets: list[str] | None = None,
        output: str = "",
        status: str = STATUS_QUEUED,
    ) -> DownloadRecord:
        record = DownloadRecord(
            title=title,
            argv=[str(item) for item in argv],
            targets=[str(item) for item in (targets or [])],
            output=output,
            status=status,
        )
        self._records.append(record)
        if len(self._records) > HISTORY_LIMIT:
            self._records = self._records[-HISTORY_LIMIT:]
        self.save()
        self.changed.emit()
        return record

    def start(self, record_id: str) -> bool:
        record = self.get(record_id)
        if record is None or record.status == STATUS_RUNNING:
            return False
        record.status = STATUS_RUNNING
        record.started_at = _now()
        record.finished_at = 0.0
        record.exit_code = None
        record.message = ""
        self.save()
        self.changed.emit()
        return True

    def finish(self, record_id: str, exit_code: int, message: str = "") -> None:
        record = self.get(record_id)
        if record is None:
            return
        record.exit_code = int(exit_code)
        record.finished_at = _now()
        if exit_code == 0:
            record.status = STATUS_DONE
        else:
            record.status = STATUS_FAILED
            record.message = message or f"退出码 {exit_code}"
        self.save()
        self.changed.emit()

    def fail(self, record_id: str, message: str) -> None:
        record = self.get(record_id)
        if record is None:
            return
        record.status = STATUS_FAILED
        record.finished_at = _now()
        record.message = message
        self.save()
        self.changed.emit()

    def cancel(self, record_id: str, message: str = "已取消") -> None:
        record = self.get(record_id)
        if record is None or record.status not in (STATUS_QUEUED, STATUS_RUNNING):
            return
        record.status = STATUS_CANCELLED
        record.finished_at = _now()
        record.message = message
        self.save()
        self.changed.emit()

    def retry(self, record_id: str) -> DownloadRecord | None:
        """把失败记录重新排进队列，返回排队用的新记录。"""
        record = self.get(record_id)
        if record is None or not record.retryable:
            return None
        record.status = STATUS_QUEUED
        record.exit_code = None
        record.finished_at = 0.0
        record.message = ""
        self.save()
        self.changed.emit()
        return record

    def clear_finished(self) -> int:
        keep = [
            record
            for record in self._records
            if record.status in (STATUS_QUEUED, STATUS_RUNNING)
        ]
        removed = len(self._records) - len(keep)
        if not removed:
            return 0
        self._records = keep
        self.save()
        self.changed.emit()
        return removed


def _selftest() -> int:
    import tempfile

    from . import ensure_utf8_stdout

    ensure_utf8_stdout()
    failures: list[str] = []

    def check(name: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{name}: 期望 {want!r}，实际 {got!r}")

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "downloads.json"
        manager = DownloadManager(path)
        first = manager.add("下载 1 首", ["download", "1"], ["1"], "/tmp")
        check("初始排队", first.status, STATUS_QUEUED)
        check("排队可取出", manager.next_queued().id, first.id)
        manager.start(first.id)
        check("运行状态", manager.get(first.id).status, STATUS_RUNNING)
        manager.finish(first.id, 1, "网络错误")
        check("失败状态", manager.get(first.id).status, STATUS_FAILED)
        check("可重试", manager.get(first.id).retryable, True)
        manager.retry(first.id)
        check("重试后排队", manager.get(first.id).status, STATUS_QUEUED)

        second = manager.add("下载 2 首", ["download", "2"], ["2"], "/tmp")
        manager.finish(second.id, 0)
        check("完成状态", manager.get(second.id).status, STATUS_DONE)
        check("最新在前", manager.records[0].id, second.id)

        reloaded = DownloadManager(path)
        check("历史落盘", len(reloaded.records), 2)
        # 进程退出后不可能还有任务真的在排队/运行，统一标成可重试的「已取消」
        check("排队任务标为中断", reloaded.get(first.id).status, STATUS_CANCELLED)
        check("完成状态保留", reloaded.get(second.id).status, STATUS_DONE)

        removed = reloaded.clear_finished()
        check("清理完成", removed, 2)
        check("清理后为空", len(reloaded.records), 0)

        running = reloaded.add("运行中", ["download", "3"], ["3"], "/tmp")
        reloaded.start(running.id)
        interrupted = DownloadManager(path)
        check("未完成任务标为中断", interrupted.get(running.id).status, STATUS_CANCELLED)

    if failures:
        print(f"下载任务自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("下载任务自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
