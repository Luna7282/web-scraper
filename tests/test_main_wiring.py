"""Tests for main.py's new pipeline-wiring helpers -- offline, stubbed
dependencies. Not testing the interactive main() flow itself (that needs
live Prompt.ask input and a real crawl, out of scope for an automated
test and explicitly not run live this step)."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from crawl.frontier import Frontier
from crawl.pipeline import FetchTimeout, FetchHTTPError, RateLimitError
from main import _make_scope_check, _make_extract_fn, _make_fetch_fn, _is_rate_limit_exception, score_report_command


class TestMakeScopeCheck(unittest.TestCase):
    def test_accepts_url_matching_any_pair(self):
        scope_check = _make_scope_check([("x.com", "/docs/"), ("y.com", None)])
        self.assertTrue(scope_check("https://x.com/docs/page"))
        self.assertTrue(scope_check("https://y.com/anything"))

    def test_rejects_url_matching_no_pair(self):
        scope_check = _make_scope_check([("x.com", "/docs/")])
        self.assertFalse(scope_check("https://x.com/other/page"))
        self.assertFalse(scope_check("https://z.com/page"))


class TestIsRateLimitException(unittest.TestCase):
    def test_status_code_attribute_429(self):
        e = Exception("rate limited")
        e.status_code = 429
        self.assertTrue(_is_rate_limit_exception(e))

    def test_response_status_code_429(self):
        e = Exception("rate limited")
        e.response = MagicMock(status_code=429)
        self.assertTrue(_is_rate_limit_exception(e))

    def test_non_429_not_rate_limit(self):
        e = Exception("some other error")
        e.status_code = 500
        self.assertFalse(_is_rate_limit_exception(e))

    def test_no_status_code_at_all(self):
        self.assertFalse(_is_rate_limit_exception(ValueError("plain error")))


class TestMakeExtractFn(unittest.IsolatedAsyncioTestCase):
    async def test_returns_llm_response_content(self):
        fake_llm = MagicMock()
        fake_llm.invoke.return_value = MagicMock(content='[{"instruction": "Q", "response": "A"}]')
        extract_fn = _make_extract_fn(fake_llm)
        result = await extract_fn("some page content")
        self.assertEqual(result, '[{"instruction": "Q", "response": "A"}]')
        fake_llm.invoke.assert_called_once()

    async def test_truncates_content_to_4000_chars(self):
        fake_llm = MagicMock()
        fake_llm.invoke.return_value = MagicMock(content="[]")
        extract_fn = _make_extract_fn(fake_llm)
        await extract_fn("x" * 10000)
        call_args = fake_llm.invoke.call_args[0][0]
        human_message = call_args[1]
        self.assertLessEqual(len(human_message.content), 4000 + 100)  # +prefix text

    async def test_429_translated_to_rate_limit_error(self):
        fake_llm = MagicMock()
        err = Exception("rate limited")
        err.status_code = 429
        fake_llm.invoke.side_effect = err
        extract_fn = _make_extract_fn(fake_llm)
        with self.assertRaises(RateLimitError):
            await extract_fn("content")

    async def test_non_429_exception_propagates_unchanged(self):
        fake_llm = MagicMock()
        fake_llm.invoke.side_effect = ValueError("something else broke")
        extract_fn = _make_extract_fn(fake_llm)
        with self.assertRaises(ValueError):
            await extract_fn("content")


class TestMakeFetchFn(unittest.IsolatedAsyncioTestCase):
    async def test_successful_fetch_returns_markdown_and_hrefs(self):
        fake_crawler = MagicMock()
        fake_result = MagicMock(
            success=True,
            markdown="# Page content",
            links={"internal": [{"href": "https://x.com/a"}], "external": []},
        )
        fake_crawler.arun = MagicMock(return_value=_async_return(fake_result))
        fetch_fn = _make_fetch_fn(fake_crawler)
        markdown, hrefs = await fetch_fn("https://x.com")
        self.assertEqual(markdown, "# Page content")
        self.assertEqual(hrefs, ["https://x.com/a"])

    async def test_non_200_raises_fetch_http_error(self):
        fake_crawler = MagicMock()
        fake_result = MagicMock(success=False, status_code=404, redirected_status_code=None, error_message=None)
        fake_crawler.arun = MagicMock(return_value=_async_return(fake_result))
        fetch_fn = _make_fetch_fn(fake_crawler)
        with self.assertRaises(FetchHTTPError) as ctx:
            await fetch_fn("https://x.com/missing")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_failure_with_no_status_raises_fetch_timeout(self):
        fake_crawler = MagicMock()
        fake_result = MagicMock(
            success=False, status_code=None, redirected_status_code=None, error_message="connection reset"
        )
        fake_crawler.arun = MagicMock(return_value=_async_return(fake_result))
        fetch_fn = _make_fetch_fn(fake_crawler)
        with self.assertRaises(FetchTimeout):
            await fetch_fn("https://x.com")


async def _async_return(value):
    return value


class TestScoreReportCommand(unittest.IsolatedAsyncioTestCase):
    async def test_reads_real_frontier_db_and_prints_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "frontier.db")
            frontier = Frontier(db_path)
            await frontier.open()
            await frontier.seed([("https://x.com/a", None)])
            row = await frontier.claim()
            await frontier.score_and_resolve_children(row.url, score=0.75, promote=True)
            await frontier.close()

            with patch("main.console") as mock_console:
                await score_report_command(db_path)
                mock_console.print.assert_called_once()
                printed = mock_console.print.call_args[0][0]
                self.assertIn("0.7500", printed)

    async def test_missing_db_exits(self):
        with self.assertRaises(SystemExit):
            await score_report_command("/nonexistent/path/frontier.db")


if __name__ == "__main__":
    unittest.main()
