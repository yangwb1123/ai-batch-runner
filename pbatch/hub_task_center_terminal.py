"""Reuse the standalone observer with authenticated Hub metadata, never Hub control widgets."""
from __future__ import annotations

from .hub_client import HubError
from .task_center_terminal import TaskCenterApp


class HubTaskCenterApp(TaskCenterApp):
    TITLE = "Hub 任务中心 · 认证只读"
    NOTICE = ("认证 Hub session/turn 元数据窗口；不是全机任务清单/Agent 会话或计算调度器。"
              "每次请求重新校验 tenant/instance；未知不是零；无执行、清场或上传权。")

    def __init__(self, client, *, compute=False, source_sessions=False, **kwargs):
        if source_sessions and not compute:
            raise ValueError("source-session references need explicit compute selection")
        if compute:
            self.TITLE = "Hub 计算任务 · 认证只读"
            self.NOTICE = ("独立的计算任务元数据窗口；不是设备清单/派发器；命令/结果未读取。"
                           "不合并原生/本地任务或计费；未知不是零；无重试/取消/清场或上传权。")
        if source_sessions:
            self.TITLE = "Hub 源会话引用 · 认证只读"
            self.NOTICE = ("实例/计算/会话三类读权限的身份引用窗口；不是执行父子关系或本地账本映射。"
                           "不去重或相加计费；未知不是零；无执行、清场或上传权。")
        read = client.compute_source_session_snapshot if source_sessions else (
            client.compute_task_snapshot if compute else client.task_snapshot)
        def load(limit, source):
            try:
                return read(limit, source)
            except HubError as exc:
                raise ValueError(f"Hub metadata unavailable (HTTP {exc.status}); no cached facts retained") from None
        super().__init__(loader=load, **kwargs)


def run_terminal(client, limit: int, source: str, task_id: str, interval: float, *, compute=False, source_sessions=False) -> int:
    HubTaskCenterApp(client, compute=compute, source_sessions=source_sessions, limit=limit,
                     source=source, task_id=task_id, interval=interval).run()
    return 0  # viewer exit is not remote PASS, lease release, control or cancellation
