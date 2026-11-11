import asyncio
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('job_manager_under_test', ROOT / 'job_manager.py')
jm = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = jm
spec.loader.exec_module(jm)


class JobManagerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.spawned = []

        def spawn(coro):
            task = asyncio.create_task(coro)
            self.spawned.append(task)
            task.add_done_callback(lambda t: self.spawned.remove(t) if t in self.spawned else None)
            return task

        self.manager = jm.JobManager(spawn, Mock())

    async def asyncTearDown(self):
        for task in list(self.spawned):
            task.cancel()
        await asyncio.gather(*self.spawned, return_exceptions=True)

    async def drain(self):
        await asyncio.gather(*list(self.spawned), return_exceptions=True)

    async def test_submit_runs_work_and_records_success(self):
        seen = {}

        async def work(job):
            seen['job'] = job
            job['stage'] = '分析中'
            return 'ok'

        result = self.manager.submit('report', '123', '2026-11-11', work)
        self.assertTrue(result['accepted'])
        self.assertEqual(result['status'], 'running')
        self.assertIn('job_id', result)
        await self.drain()
        job = self.manager.snapshot()[0]
        self.assertEqual(job['status'], 'succeeded')
        self.assertEqual(job['stage'], '已完成')
        self.assertEqual(job['kind'], 'report')
        self.assertEqual(job['source'], 'webui')
        self.assertTrue(job['finished_at'])
        self.assertIs(seen['job'], self.manager._jobs[result['job_id']])

    async def test_same_group_mutual_exclusion_across_sources(self):
        gate = asyncio.Event()

        async def work(job):
            await gate.wait()
            return 'ok'

        first = self.manager.submit('report', '123', 'd', work, source='command')
        self.assertTrue(first['accepted'])
        busy = self.manager.submit('comic', '123', 'd', work, source='webui')
        self.assertFalse(busy['accepted'])
        self.assertEqual(busy['status'], 'busy')
        self.assertEqual(busy['job_id'], first['job_id'])
        other = self.manager.submit('report', '456', 'd', work)
        self.assertTrue(other['accepted'])
        self.assertEqual(self.manager.active_count(), 2)
        gate.set()
        await self.drain()
        self.assertEqual(self.manager.active_count(), 0)
        self.assertIsNone(self.manager.active_for('123'))

    async def test_failure_and_insufficient_outcomes(self):
        async def failed(job):
            return 'failed'

        async def insufficient(job):
            return 'insufficient'

        self.manager.submit('report', '123', 'd', failed)
        self.manager.submit('report', '456', 'd', insufficient)
        await self.drain()
        jobs = {job['group_id']: job for job in self.manager.snapshot()}
        self.assertEqual(jobs['123']['status'], 'failed')
        self.assertEqual(jobs['456']['status'], 'failed')
        self.assertIn('消息不足', jobs['456']['error'])

    async def test_exception_does_not_leak_message_and_frees_group(self):
        async def boom(job):
            raise RuntimeError('secret-detail')

        self.manager.submit('report', '123', 'd', boom)
        await self.drain()
        job = self.manager.snapshot()[0]
        self.assertEqual(job['status'], 'failed')
        self.assertNotIn('secret-detail', job['error'])
        self.assertIsNone(self.manager.active_for('123'))
        self.assertTrue(self.manager.submit('report', '123', 'd', boom)['accepted'])

    async def test_work_error_field_is_preserved(self):
        async def work(job):
            job['error'] = '未检测到麦麦绘图'
            return 'failed'

        self.manager.submit('comic', '123', 'd', work)
        await self.drain()
        self.assertEqual(self.manager.snapshot()[0]['error'], '未检测到麦麦绘图')

    async def test_cancel_marks_cancelled_and_releases_group(self):
        gate = asyncio.Event()

        async def work(job):
            await gate.wait()
            return 'ok'

        self.manager.submit('report', '123', 'd', work)
        result = self.manager.cancel('123')
        self.assertTrue(result['accepted'])
        self.assertEqual(result['status'], 'cancelling')
        await self.drain()
        job = self.manager.snapshot()[0]
        self.assertEqual(job['status'], 'cancelled')
        self.assertIsNone(self.manager.active_for('123'))
        self.assertFalse(self.manager.cancel('123')['accepted'])

    async def test_cancel_idle_group(self):
        result = self.manager.cancel('999')
        self.assertFalse(result['accepted'])
        self.assertEqual(result['status'], 'idle')

    async def test_wait_returns_terminal_job(self):
        async def work(job):
            await asyncio.sleep(0.01)
            return 'ok'

        result = self.manager.submit('report', '123', 'd', work)
        job = await asyncio.wait_for(self.manager.wait(result['job_id'], poll=0.005), 2)
        self.assertEqual(job['status'], 'succeeded')
        self.assertIsNone(await self.manager.wait('nonexistent'))

    async def test_snapshot_order_and_history_cap(self):
        async def work(job):
            return 'ok'

        for index in range(jm.HISTORY_LIMIT + 20):
            self.manager.submit('report', str(index), 'd', work)
            await self.drain()
        snapshot = self.manager.snapshot(limit=1000)
        self.assertEqual(len(snapshot), jm.HISTORY_LIMIT)
        self.assertEqual(snapshot[0]['group_id'], str(jm.HISTORY_LIMIT + 19))
        self.assertEqual(self.manager.snapshot(limit=5)[0]['group_id'], str(jm.HISTORY_LIMIT + 19))

    async def test_active_jobs_are_never_pruned(self):
        gate = asyncio.Event()

        async def work(job):
            await gate.wait()
            return 'ok'

        async def quick(job):
            return 'ok'

        active = self.manager.submit('report', 'active', 'd', work)
        for index in range(jm.HISTORY_LIMIT + 10):
            result = self.manager.submit('report', str(index), 'd', quick)
            job = await self.manager.wait(result['job_id'], poll=0.001)
            self.assertEqual(job['status'], 'succeeded')
        snapshot = self.manager.snapshot(limit=1000)
        self.assertEqual(len(snapshot), jm.HISTORY_LIMIT)
        self.assertIn(active['job_id'], [job['job_id'] for job in snapshot])
        self.assertIsNotNone(self.manager.active_for('active'))
        gate.set()
        await self.drain()


if __name__ == '__main__':
    unittest.main()