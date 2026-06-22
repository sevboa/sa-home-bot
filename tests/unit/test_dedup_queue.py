from dataclasses import dataclass

from sentinel_bot.worker.queue import DedupQueue


@dataclass
class FakeJob:
    key: str

    @property
    def dedup_key(self) -> str:
        return self.key


async def test_duplicate_job_is_dropped():
    q = DedupQueue()
    assert await q.put(FakeJob("scan")) is True
    assert await q.put(FakeJob("scan")) is False
    assert q.qsize() == 1


async def test_distinct_jobs_both_enqueued():
    q = DedupQueue()
    assert await q.put(FakeJob("scan")) is True
    assert await q.put(FakeJob("housekeeping")) is True
    assert q.qsize() == 2


async def test_key_released_on_get_allows_requeue():
    q = DedupQueue()
    await q.put(FakeJob("scan"))
    job = await q.get()
    assert job.dedup_key == "scan"
    # После get() ключ свободен — можно поставить снова.
    assert await q.put(FakeJob("scan")) is True


async def test_stop_sentinel_is_none():
    q = DedupQueue()
    await q.stop()
    assert await q.get() is None
