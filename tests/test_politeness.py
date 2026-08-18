"""Offline tests for HostPoliteness -- concurrency cap and inter-request
spacing per host, no network."""
import asyncio
import time
import unittest

from crawl.politeness import HostPoliteness


class TestHostPolitenessDelay(unittest.IsolatedAsyncioTestCase):
    async def test_second_request_to_same_host_waits_out_the_delay(self):
        delay = 0.15
        politeness = HostPoliteness(max_concurrent_per_host=5, default_delay_seconds=delay)
        async with politeness.hold("x.test"):
            pass
        start = time.monotonic()
        async with politeness.hold("x.test"):
            pass
        # small tolerance for event-loop timer granularity, not a flaky margin
        self.assertGreaterEqual(time.monotonic() - start, delay * 0.9)

    async def test_different_hosts_not_throttled_against_each_other(self):
        politeness = HostPoliteness(max_concurrent_per_host=5, default_delay_seconds=1.0)
        async with politeness.hold("a.test"):
            pass
        start = time.monotonic()
        async with politeness.hold("b.test"):
            pass
        self.assertLess(time.monotonic() - start, 0.5)  # no cross-host spacing

    async def test_crawl_delay_override_takes_precedence_over_default(self):
        delay = 0.2
        politeness = HostPoliteness(max_concurrent_per_host=5, default_delay_seconds=0.01)
        async with politeness.hold("x.test", crawl_delay=delay):
            pass
        start = time.monotonic()
        async with politeness.hold("x.test", crawl_delay=delay):
            pass
        self.assertGreaterEqual(time.monotonic() - start, delay * 0.9)


class TestHostPolitenessConcurrency(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_requests_to_one_host_capped(self):
        politeness = HostPoliteness(max_concurrent_per_host=2, default_delay_seconds=0.0)
        in_flight = 0
        max_seen = 0

        async def worker():
            nonlocal in_flight, max_seen
            async with politeness.hold("x.test"):
                in_flight += 1
                max_seen = max(max_seen, in_flight)
                await asyncio.sleep(0.05)
                in_flight -= 1

        await asyncio.gather(*(worker() for _ in range(6)))
        self.assertLessEqual(max_seen, 2)

    async def test_slot_released_even_when_caller_raises(self):
        politeness = HostPoliteness(max_concurrent_per_host=1, default_delay_seconds=0.0)
        with self.assertRaises(ValueError):
            async with politeness.hold("x.test"):
                raise ValueError("simulated fetch failure")
        # a second acquire must not hang if the first slot wasn't released
        await asyncio.wait_for(politeness.hold("x.test").__aenter__(), timeout=1)


if __name__ == "__main__":
    unittest.main()
