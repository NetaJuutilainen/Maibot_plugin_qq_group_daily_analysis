"""统一后台任务状态机（P2）：WebUI / 命令 / 定时 / 漫画联动共用一个 job 通道。

同群互斥、可取消、保留有限历史供总览展示。work 协程收到 job dict 本身，
可随时更新 stage/error；返回 "ok" / "failed" / "insufficient"，抛异常按失败记录。
"""

import asyncio
from datetime import datetime
import uuid

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
ACTIVE = frozenset({"queued", "running", "cancelling"})
HISTORY_LIMIT = 200
SOURCE_NAMES = {"webui": "WebUI", "command": "命令", "schedule": "定时", "auto": "日报联动"}


class JobManager:
    def __init__(self, spawn, logger=None):
        self._spawn = spawn
        self._logger = logger
        self._jobs: dict[str, dict] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._active: dict[str, str] = {}
        self._seq: dict[str, int] = {}
        self._counter = 0

    def active_for(self, group_id: str) -> dict | None:
        job_id = self._active.get(group_id)
        job = self._jobs.get(job_id) if job_id else None
        return job if job is not None and job["status"] in ACTIVE else None

    def active_count(self) -> int:
        return sum(1 for job in self._jobs.values() if job["status"] in ACTIVE)

    def submit(self, kind: str, group_id: str, day: str, work, source: str = "webui") -> dict:
        active = self.active_for(group_id)
        if active is not None:
            return {"accepted": False, "status": "busy", "job_id": active["job_id"],
                    "message": f"该群已有任务正在进行（{SOURCE_NAMES.get(active['source'], active['source'])}），请完成或取消后再试"}
        job = {"job_id": uuid.uuid4().hex[:12], "kind": kind, "group_id": group_id, "day": day,
               "source": source, "status": "running", "stage": "已受理", "error": "",
               "started_at": datetime.now().isoformat(timespec="seconds"), "finished_at": ""}
        self._jobs[job["job_id"]] = job
        self._active[group_id] = job["job_id"]
        self._counter += 1
        self._seq[job["job_id"]] = self._counter
        self._prune()

        async def run():
            outcome = await work(job)
            if outcome == "ok":
                job.update(status="succeeded", stage="已完成")
            else:
                job.update(status="failed", stage="未完成",
                           error="消息不足，未生成日报" if outcome == "insufficient"
                           else (job.get("error") or "任务未完成，请查看插件日志"))

        def finished(task):
            if task.cancelled():
                job.update(status="cancelled", stage="已取消")
            elif task.exception() is not None:
                job.update(status="failed", stage="失败",
                           error=job.get("error") or "任务失败，请查看插件日志；不要连续重复提交")
                if self._logger is not None:
                    self._logger.warning("[weekly] 后台任务失败: %s %s", kind, type(task.exception()).__name__)
            job["finished_at"] = datetime.now().isoformat(timespec="seconds")
            if self._active.get(group_id) == job["job_id"]:
                self._active.pop(group_id, None)
            self._tasks.pop(job["job_id"], None)

        task = self._spawn(run())
        self._tasks[job["job_id"]] = task
        task.add_done_callback(finished)
        return {"accepted": True, "status": "running", "job_id": job["job_id"],
                "message": "已受理，进度见任务状态；接受不代表已完成"}

    def cancel(self, group_id: str) -> dict:
        job = self.active_for(group_id)
        if job is None:
            return {"accepted": False, "status": "idle", "message": "该群没有可取消的任务"}
        task = self._tasks.get(job["job_id"])
        if task is None or task.done():
            return {"accepted": False, "status": "idle", "message": "该群任务已结束，无可取消项"}
        job.update(status="cancelling", stage="正在取消")
        task.cancel()
        return {"accepted": True, "status": "cancelling", "job_id": job["job_id"],
                "message": "已请求取消；已提交的模型请求、绘图任务、存档和发送无法撤回"}

    async def wait(self, job_id: str, poll: float = 1.0) -> dict | None:
        """轮询等待任务终结（定时入口用）；自身被取消时正常向外传播 CancelledError。"""
        while True:
            job = self._jobs.get(job_id)
            if job is None or job["status"] in TERMINAL:
                return job
            await asyncio.sleep(poll)

    def snapshot(self, limit: int = 100) -> list[dict]:
        jobs = sorted(self._jobs.values(),
                      key=lambda item: (item["started_at"], self._seq.get(item["job_id"], 0)), reverse=True)
        return [dict(job) for job in jobs[:limit]]

    def _prune(self) -> None:
        overflow = len(self._jobs) - HISTORY_LIMIT
        if overflow <= 0:
            return
        terminal = sorted((job for job in self._jobs.values() if job["status"] in TERMINAL),
                          key=lambda item: item["finished_at"] or item["started_at"])
        for job in terminal[:overflow]:
            self._jobs.pop(job["job_id"], None)
            self._seq.pop(job["job_id"], None)