"""Offline regressions for untrusted email fields and fresh-clone defaults."""
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

import monitor


class Markup(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.tags = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class EmailSafetyTest(unittest.TestCase):
    def test_product_fields_are_text_and_cannot_add_markup(self):
        payload = '<img src=x onerror="alert(1)">'
        body = monitor.build_email_html({payload: {
            "title": payload, "price": payload, "first_seen": payload,
            "on_shelf": False,
            "url": monitor.SITE + '/shop/product/x" onclick="alert(1)',
        }})
        tags = Markup(body).tags
        self.assertFalse(any(tag == "img" for tag, _ in tags))
        self.assertFalse(any("onclick" in attrs for _, attrs in tags))
        self.assertEqual(body.count('&lt;img src=x onerror=&quot;alert(1)&quot;&gt;'), 4)

    def test_unsafe_product_links_fall_back_to_refurb_store(self):
        for url in ('javascript:alert(1)', 'data:text/html,evil',
                    'https://www.apple.com.cn.evil.invalid/x', None):
            with self.subTest(url=url):
                body = monitor.build_email_html({"PN": {
                    "title": "Mac", "price": "RMB 1", "url": url,
                }})
                links = [attrs["href"] for tag, attrs in Markup(body).tags if tag == "a"]
                self.assertEqual(links, [monitor.REFURB_URL, monitor.REFURB_URL])

    def test_valid_product_link_and_text_survive_escaping(self):
        url = monitor.SITE + '/shop/product/x'
        body = monitor.build_email_html({"PN": {
            "title": "Mac & Studio", "price": "RMB 1", "url": url,
        }})
        self.assertIn('href="' + url + '"', body)
        self.assertIn('Mac &amp; Studio', body)

    def test_numeric_price_remains_renderable(self):
        body = monitor.build_email_html({"PN": {
            "title": "Mac", "price": 9999, "url": monitor.REFURB_URL,
        }})
        self.assertIn('9999</td>', body)


class FeishuDefaultsTest(unittest.TestCase):
    def test_default_library_expands_home_in_actual_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp) / '.local/lib/_feishu-lib.sh'
            lib.parent.mkdir(parents=True)
            lib.write_text('feishu_send_text() { echo "code=0 msg=success"; }\n')
            with patch.dict(monitor.os.environ, {"HOME": tmp}), patch.object(monitor, "log"):
                monitor.send_feishu({}, "offline fixture")

    def test_configured_library_expands_home(self):
        with patch.dict(monitor.os.environ, {"HOME": "/fixture"}), \
                patch.object(monitor, "_feishu_exec", return_value=(0, "code=0", "")) as execute, \
                patch.object(monitor, "log"):
            monitor.send_feishu({"feishu": {"lib": "~/custom/lib.sh"}}, "fixture")
            self.assertEqual(execute.call_args.args[0][4], "/fixture/custom/lib.sh")

    def test_failure_markers_cannot_be_mistaken_for_success(self):
        for output in ("code=01 msg=failed", "code=000", "error_code=0", "code=0oops"):
            with self.subTest(output=output), \
                    patch.object(monitor, "_feishu_exec", return_value=(0, output, "")):
                with self.assertRaisesRegex(RuntimeError, "飞书发送失败"):
                    monitor.send_feishu({}, "fixture")

    def test_invalid_timeout_allows_smtp_fallback(self):
        for timeout in ("bad", -1, [1], {"bad": 1}):
            with self.subTest(timeout=timeout), patch.object(monitor, "log"), \
                    patch.object(monitor, "_feishu_exec", side_effect=AssertionError("must not send")), \
                    patch.object(monitor, "send_email") as smtp:
                used = monitor.notify({"feishu": {"timeout": timeout}}, "s", "h", "t")
                self.assertEqual(used, "smtp")
                smtp.assert_called_once()


class FreshCloneTest(unittest.TestCase):
    def test_first_log_creates_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            logfile = Path(tmp) / 'logs/monitor.log'
            with patch.object(monitor, "LOG_FILE", logfile):
                monitor.log("fixture")
            self.assertIn("fixture", logfile.read_text())

    def test_builtin_selftest_creates_previews_without_existing_logs(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(monitor, "BASE_DIR", Path(tmp)), \
                patch.object(monitor, "fetch_html", return_value='window.REFURB_GRID_BOOTSTRAP = {"tiles": []};'), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(monitor.cmd_selftest({}), 0)
            for name in ('selftest_email_preview.html', 'selftest_summary_preview.html'):
                self.assertTrue((Path(tmp) / 'logs' / name).is_file())


if __name__ == '__main__':
    unittest.main()
