"""Dispatcher 主循环：周期性 dispatch_once + reap_stale_tasks。

生产环境由 paper-dispatcher 容器跑（python -m server.dispatcher.loop）。
单实例即可——dispatch 的并发安全由 DB CAS 保证，多实例也能跑（文档 §9）。
"""
from __future__ import annotations
import time

from server import config as platform_config
from server.db.session import get_sessionmaker
from server.dispatcher import DispatcherLimits, dispatch_once, reap_stale_tasks


class _NoopPublisher:
    """dispatcher 主循环只负责「把 QUEUED → DISPATCHED 并投递 Celery 消息」。

    Celery 消息的真正发送在 dispatch_once 内部完成（publisher.publish = Celery .delay）。
    本循环跑在 dispatcher 容器里，不直接执行任务，所以这里 publish = 发 Celery 消息。
    """
    def publish(self, task_id: str) -> None:
        from server.worker.core import run_task
        if run_task is not None:
            run_task.delay(task_id)
        # 离线无 Celery 时，dispatcher 不就地执行（执行交给测试或人工 kick）。


def main() -> None:
    limits = DispatcherLimits(
        global_max_running=platform_config.GLOBAL_MAX_RUNNING_TASKS,
        per_user_max_running=platform_config.PER_USER_MAX_RUNNING_TASKS,
        per_project_max_running=platform_config.PER_PROJECT_MAX_RUNNING_TASKS,
        per_user_max_queued=platform_config.PER_USER_MAX_QUEUED_TASKS,
    )
    factory = lambda: get_sessionmaker()()
    publisher = _NoopPublisher()
    print("[dispatcher] started; interval="
          f"{platform_config.DISPATCHER_INTERVAL_SECONDS}s, "
          f"reaper interval={platform_config.REAPER_INTERVAL_SECONDS}s", flush=True)

    last_reap = 0.0
    while True:
        try:
            dispatch_once(factory, limits, publisher)
        except Exception as exc:  # noqa: BLE001
            print(f"[dispatcher] dispatch error: {exc}", flush=True)

        now = time.monotonic()
        if now - last_reap >= platform_config.REAPER_INTERVAL_SECONDS:
            try:
                reaped = reap_stale_tasks(factory, platform_config.STALE_HEARTBEAT_SECONDS)
                if reaped:
                    print(f"[dispatcher] reaped stale tasks: {reaped}", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[dispatcher] reap error: {exc}", flush=True)
            last_reap = now

        time.sleep(platform_config.DISPATCHER_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
