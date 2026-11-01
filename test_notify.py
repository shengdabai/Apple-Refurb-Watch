"""发信退避、积压摘要、日志单路与轮转、飞书/SMTP 通道回落的离线测试（假 SMTP、假飞书、合成页面、注入时间，无网络、不真实发送）。"""
import email
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import monitor

CONFIG = {
    "recipient": "to@example.invalid",
    "smtp": {"host": "smtp.example.invalid", "port": 465, "sender": "from@example.invalid"},
}
T0 = datetime(2026, 10, 1, 8, 0, 0)


def tile(pn, title="翻新 Mac Studio M4 Max"):
    return {
        "title": title,
        "partNumber": pn,
        "productDetailsUrl": f"/shop/product/{pn.lower()}",
        "price": {"currentPrice": {"amount": "RMB 9,999"}},
        "filters": {"dimensions": {"refurbClearModel": "macstudio"}},
    }


def page(*pns):
    payload = json.dumps({"tiles": [tile(pn) for pn in pns]})
    return f"<html><script>window.REFURB_GRID_BOOTSTRAP = {payload};</script></html>"


class FakeSMTP:
    """替身 SMTP：记录每次 login/sendmail；ok=False 时 login 抛断连。"""
    ok = False
    logins = []
    sent = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, *args):
        FakeSMTP.logins.append(args)
        if not FakeSMTP.ok:
            raise monitor.smtplib.SMTPServerDisconnected("Connection unexpectedly closed")

    def sendmail(self, sender, recipients, raw):
        body = email.message_from_string(raw).get_payload(decode=True).decode("utf-8")
        FakeSMTP.sent.append(body)


class FakeFeishu:
    """替身 send_feishu：记录每次调用文本；ok=False 时抛 RuntimeError（与真实实现的失败语义一致）。"""
    ok = False
    sent = []

    def __call__(self, config, text):
        if not FakeFeishu.ok:
            raise RuntimeError("fake feishu down")
        FakeFeishu.sent.append(text)


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.state_file = self.dir / "state.json"
        self.log_file = self.dir / "monitor.log"
        FakeSMTP.ok = False
        FakeSMTP.logins = []
        FakeSMTP.sent = []
        FakeFeishu.ok = False
        FakeFeishu.sent = []
        self.html = page()
        patches = [
            patch.object(monitor, "STATE_FILE", self.state_file),
            patch.object(monitor, "LOG_FILE", self.log_file),
            patch.object(monitor, "fetch_html", side_effect=lambda url: self.html),
            patch.object(monitor, "read_secret", return_value="fixture"),
            patch.object(monitor, "resolve_real_ip", return_value="192.0.2.1"),
            patch.object(monitor, "get_src_ip", return_value="192.0.2.2"),
            patch.object(monitor, "_DirectSMTP_SSL", FakeSMTP),
            patch.object(monitor.smtplib, "SMTP_SSL", FakeSMTP),
            # 默认飞书不可用 → 走 SMTP，旧用例语义不变；底层子进程一旦被触达即失败，保证测试不会真实发送
            patch.object(monitor, "send_feishu", FakeFeishu()),
            patch.object(monitor, "_feishu_exec",
                         side_effect=AssertionError("测试不得触达真实飞书子进程")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def write_state(self, state):
        self.state_file.write_text(json.dumps(state), encoding="utf-8")

    def state(self):
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def run_at(self, minutes):
        out = io.StringIO()
        with redirect_stdout(out):
            monitor.run_once(CONFIG, now=T0 + timedelta(minutes=minutes))
        return out.getvalue()


class BackoffTest(Harness):
    def test_consecutive_failures_back_off_exponentially_with_cap(self):
        # 旧版 state.json 形状（无 pending/send_failures/next_send_after），货架上有 1 款未通知
        self.write_state({"first_run_done": True, "seen": {}, "last_check": None,
                          "last_status": "new_found_email_failed: Connection unexpectedly closed"})
        self.html = page("FAKE1CH/A")
        attempts_at = []
        for minute in range(0, 24 * 60, 5):  # 一天的 launchd 节奏（每 5 分钟）
            before = len(FakeSMTP.logins)
            self.run_at(minute)
            if len(FakeSMTP.logins) > before:
                attempts_at.append(minute)
        # 每次尝试 = 直连 + 标准回退两次 login；尝试间隔按 10,20,40,80,160,320,360,360… 分钟拉开
        gaps = [b - a for a, b in zip(attempts_at, attempts_at[1:])]
        self.assertEqual(gaps[:7], [10, 20, 40, 80, 160, 320, 360])
        self.assertTrue(all(g <= 360 for g in gaps))
        self.assertLessEqual(len(attempts_at), 12)  # 旧逻辑一天 288 次尝试 / 576 次 login
        self.assertEqual(len(FakeSMTP.logins), 2 * len(attempts_at))
        state = self.state()
        self.assertEqual(state["send_failures"], len(attempts_at))
        self.assertIn("FAKE1CH/A", state["pending"])  # 从未通知成功的货号不丢
        self.assertEqual(state["seen"], {})
        self.assertEqual(FakeSMTP.sent, [])

    def test_backoff_formula(self):
        self.assertEqual(monitor.send_backoff(1), timedelta(minutes=10))
        self.assertEqual(monitor.send_backoff(3), timedelta(minutes=40))
        self.assertEqual(monitor.send_backoff(50), timedelta(minutes=360))

    def test_garbage_backoff_fields_fall_back_to_defaults(self):
        self.write_state({"first_run_done": True, "seen": {}, "pending": [],
                          "send_failures": "x", "next_send_after": "not-a-date"})
        self.html = page("FAKE1CH/A")
        FakeSMTP.ok = True
        self.run_at(0)
        self.assertEqual(len(FakeSMTP.sent), 1)
        self.assertEqual(self.state()["send_failures"], 0)


class RecoveryDigestTest(Harness):
    def test_recovery_sends_one_digest_with_all_backlog(self):
        self.write_state({"first_run_done": True, "seen": {}})
        self.html = page("A1CH/A")
        self.run_at(0)                       # 失败 1 次，退避到 +10
        self.html = page("A1CH/A", "B2CH/A")
        self.run_at(5)                       # 退避中，B2 进入积压，不发信
        self.html = page("B2CH/A", "C3CH/A")  # A1 期间下架但从未通知；C3 新上架
        self.run_at(10)                      # 再失败 1 次，退避到 +30
        self.assertEqual(len(FakeSMTP.logins), 4)
        self.assertEqual(sorted(self.state()["pending"]), ["A1CH/A", "B2CH/A", "C3CH/A"])

        FakeSMTP.ok = True
        self.run_at(15)                      # 仍在退避，不发
        self.assertEqual(FakeSMTP.sent, [])
        self.run_at(30)                      # 恢复：只发 1 封，包含 3 款
        self.assertEqual(len(FakeSMTP.sent), 1)
        body = FakeSMTP.sent[0]
        for pn in ("A1CH/A", "B2CH/A", "C3CH/A"):
            self.assertIn(pn, body)
        self.assertEqual(body.count("本次检测时已下架"), 1)  # 只有 A1 标注已下架
        state = self.state()
        self.assertEqual(state["pending"], {})
        self.assertEqual(sorted(state["seen"]), ["B2CH/A", "C3CH/A"])  # 已下架的 A1 通知后不进 seen
        self.assertEqual(state["send_failures"], 0)
        self.assertIsNone(state["next_send_after"])
        self.assertEqual(state["last_status"], "ok")

        self.run_at(35)                      # 下一轮无新货，不重复发
        self.assertEqual(len(FakeSMTP.sent), 1)

    def test_digest_marks_items_that_went_off_shelf(self):
        html = monitor.build_email_html({
            "A1CH/A": {"title": "t", "price": "p", "url": "u", "first_seen": "2026-10-01T08:00:00", "on_shelf": False},
            "B2CH/A": {"title": "t", "price": "p", "url": "u", "on_shelf": True},
        })
        self.assertEqual(html.count("本次检测时已下架"), 1)

    def test_relist_after_notify_does_not_push_again(self):
        self.write_state({"first_run_done": True, "seen": {}})
        FakeSMTP.ok = True
        self.html = page("A1CH/A")
        self.run_at(0)
        self.assertEqual(len(FakeSMTP.sent), 1)
        self.assertEqual(list(self.state()["seen"]), ["A1CH/A"])
        self.assertIn("A1CH/A", self.state()["notified"])
        self.html = page()
        self.run_at(5)                       # 下架 → 从 seen 移除，但已推送台账保留
        self.assertEqual(self.state()["seen"], {})
        self.assertIn("A1CH/A", self.state()["notified"])
        self.html = page("A1CH/A")
        self.run_at(10)                      # 重新上架 → 不再推送
        self.assertEqual(len(FakeSMTP.sent), 1)
        self.assertEqual(list(self.state()["seen"]), ["A1CH/A"])
        self.assertIn("1 款已推送过的货号重新在架", self.log_file.read_text(encoding="utf-8"))


class LoggingTest(Harness):
    def test_log_goes_to_file_only_when_not_a_tty(self):
        self.write_state({"first_run_done": True, "seen": {}})
        out = self.run_at(0)
        self.assertEqual(out, "")  # launchd 下 stdout 不是 tty → 不重复写 launchd.out.log
        self.assertIn("巡检完成", self.log_file.read_text(encoding="utf-8"))

    def test_size_rotation_keeps_numbered_backups(self):
        with patch.object(monitor, "LOG_MAX_BYTES", 200), patch.object(monitor, "LOG_BACKUPS", 3):
            for i in range(60):
                monitor.log(f"line {i:03d} " + "x" * 40)
        names = sorted(p.name for p in self.dir.glob("monitor.log*"))
        self.assertEqual(names, ["monitor.log", "monitor.log.1", "monitor.log.2", "monitor.log.3"])
        for p in self.dir.glob("monitor.log*"):
            self.assertLess(p.stat().st_size, 200 + 80)
        self.assertIn("line 059", self.log_file.read_text(encoding="utf-8"))

    def test_oversized_legacy_log_is_archived_not_deleted(self):
        legacy = "旧日志\n" * 100
        self.log_file.write_text(legacy, encoding="utf-8")
        with patch.object(monitor, "LOG_MAX_BYTES", 200), patch.object(monitor, "LOG_BACKUPS", 2):
            for i in range(40):
                monitor.log(f"line {i:03d} " + "x" * 40)
        archived = self.dir / "monitor.log.pre-rotation"
        self.assertEqual(archived.read_text(encoding="utf-8"), legacy)


class ChannelFallbackTest(Harness):
    def setUp(self):
        super().setUp()
        self.write_state({"first_run_done": True, "seen": {}})
        self.html = page("A1CH/A")

    def test_feishu_success_skips_smtp_and_clears_queue(self):
        FakeFeishu.ok = True
        FakeSMTP.ok = True
        self.run_at(0)
        self.assertEqual(len(FakeFeishu.sent), 1)
        self.assertIn("A1CH/A", FakeFeishu.sent[0])
        self.assertEqual(FakeSMTP.logins, [])
        self.assertEqual(FakeSMTP.sent, [])
        state = self.state()
        self.assertEqual(state["pending"], {})
        self.assertEqual(list(state["seen"]), ["A1CH/A"])
        self.assertEqual(state["send_failures"], 0)
        self.assertIsNone(state["next_send_after"])
        self.assertEqual(state["last_status"], "ok")
        self.assertIn("通道 feishu", self.log_file.read_text(encoding="utf-8"))

    def test_feishu_failure_falls_back_to_smtp(self):
        FakeFeishu.ok = False
        FakeSMTP.ok = True
        self.run_at(0)
        self.assertEqual(len(FakeSMTP.sent), 1)
        self.assertIn("A1CH/A", FakeSMTP.sent[0])
        state = self.state()
        self.assertEqual(state["pending"], {})
        self.assertEqual(state["send_failures"], 0)
        log = self.log_file.read_text(encoding="utf-8")
        self.assertIn("通道 feishu 失败", log)
        self.assertIn("通道 smtp", log)

    def test_both_fail_keeps_queue_and_backs_off(self):
        self.run_at(0)
        state = self.state()
        self.assertIn("A1CH/A", state["pending"])
        self.assertEqual(state["seen"], {})
        self.assertEqual(state["send_failures"], 1)
        self.assertEqual(state["next_send_after"], (T0 + timedelta(minutes=10)).isoformat())
        self.assertTrue(state["last_status"].startswith("new_found_email_failed"))
        self.assertIn("feishu", state["last_status"])
        logins = len(FakeSMTP.logins)
        self.run_at(5)                       # 退避期内两个通道都不再尝试
        self.assertEqual(len(FakeSMTP.logins), logins)
        self.assertEqual(self.state()["send_failures"], 1)
        FakeFeishu.ok = True                 # 退避结束 + 飞书恢复 → 积压送出
        self.run_at(10)
        self.assertEqual(len(FakeFeishu.sent), 1)
        self.assertEqual(self.state()["pending"], {})

    def test_channels_config_controls_order_and_membership(self):
        FakeFeishu.ok = True
        FakeSMTP.ok = True
        cfg = {**CONFIG, "channels": ["smtp", "feishu"]}
        with redirect_stdout(io.StringIO()):
            monitor.run_once(cfg, now=T0)
        self.assertEqual(len(FakeSMTP.sent), 1)
        self.assertEqual(FakeFeishu.sent, [])
        self.assertEqual(monitor.notify_channels({"channels": ["feishu"]}), ["feishu"])
        self.assertEqual(monitor.notify_channels({"channels": ["bogus"]}), ["feishu", "smtp"])
        self.assertEqual(monitor.notify_channels({}), ["feishu", "smtp"])

    def test_channel_change_resets_stale_smtp_backoff(self):
        # 真实现场：SMTP 连续失败 36 次，退避到 5 小时后；新增飞书通道后下一轮就应能送出
        self.write_state({"first_run_done": True, "seen": {}, "send_failures": 36,
                          "next_send_after": (T0 + timedelta(hours=5)).isoformat(),
                          "pending": {"A1CH/A": {"title": "t", "price": "p", "url": "u",
                                                 "first_seen": "2026-10-01T08:00:00"}}})
        FakeFeishu.ok = True
        self.run_at(0)
        self.assertEqual(len(FakeFeishu.sent), 1)
        state = self.state()
        self.assertEqual(state["pending"], {})
        self.assertEqual(state["send_failures"], 0)
        self.assertEqual(state["send_channels"], ["feishu", "smtp"])

    def test_same_channels_keep_backoff(self):
        self.write_state({"first_run_done": True, "seen": {}, "send_failures": 3,
                          "send_channels": ["feishu", "smtp"],
                          "next_send_after": (T0 + timedelta(hours=1)).isoformat(),
                          "pending": {"A1CH/A": {"title": "t", "price": "p", "url": "u"}}})
        FakeFeishu.ok = True
        self.run_at(0)
        self.assertEqual(FakeFeishu.sent, [])
        self.assertEqual(self.state()["send_failures"], 3)

    def test_smtp_section_missing_is_a_failure_not_a_crash(self):
        cfg = {"recipient": "x@example.invalid"}   # 无 smtp 段
        with redirect_stdout(io.StringIO()):
            monitor.run_once(cfg, now=T0)
        self.assertIn("A1CH/A", self.state()["pending"])
        self.assertEqual(self.state()["send_failures"], 1)


class SendFeishuTest(unittest.TestCase):
    """真实走 bash 子进程 + source，但库是临时目录里的假库，只记录调用、不联网。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.record = self.dir / "calls.txt"
        p = patch.object(monitor, "log")
        p.start()
        self.addCleanup(p.stop)

    def fake_lib(self, body):
        lib = self.dir / "fake-feishu-lib.sh"
        lib.write_text(body, encoding="utf-8")
        return str(lib)

    def cfg(self, lib, **extra):
        return {"feishu": {"lib": lib, "timeout": 2, **extra}}

    def test_real_path_calls_feishu_send_text_with_text_and_timeout(self):
        lib = self.fake_lib(
            f'feishu_send_text() {{ printf "%s|%s|%s|%s|%s\\n" "$1" "$2" "$FEISHU_RECEIVE_ID" '
            f'"$FEISHU_APP_ID" "$(basename "$FEISHU_PYTHON")" >> "{self.record}"; '
            f'echo "code=0 msg=success"; return 0; }}\n')
        monitor.send_feishu(self.cfg(lib, receive_id="oc_test", app_id="cli_test"), "你好 Mac Studio")
        line = self.record.read_text(encoding="utf-8").strip()
        self.assertEqual(line, f"你好 Mac Studio|2|oc_test|cli_test|{os.path.basename(monitor._feishu_python())}")

    def test_nonzero_code_raises(self):
        lib = self.fake_lib('feishu_send_text() { echo "code=99991 msg=bad"; return 1; }\n')
        with self.assertRaisesRegex(RuntimeError, "飞书发送失败"):
            monitor.send_feishu(self.cfg(lib), "x")

    def test_missing_lib_raises_without_hanging(self):
        with self.assertRaisesRegex(RuntimeError, "飞书库不存在"):
            monitor.send_feishu(self.cfg(str(self.dir / "nope.sh")), "x")

    def test_lib_without_function_raises(self):
        lib = self.fake_lib("# empty lib\n")
        with self.assertRaisesRegex(RuntimeError, "飞书库不可用"):
            monitor.send_feishu(self.cfg(lib), "x")

    def test_hung_lib_times_out_and_kills_process_group(self):
        marker = self.dir / "still-running"
        lib = self.fake_lib(f'feishu_send_text() {{ sleep 30 & echo $! > "{marker}"; wait; }}\n')
        with patch.object(monitor, "FEISHU_HARD_TIMEOUT_GRACE", 0):
            t0 = datetime.now()
            with self.assertRaisesRegex(RuntimeError, "未返回"):
                monitor.send_feishu(self.cfg(lib, timeout=1), "x")
            self.assertLess((datetime.now() - t0).total_seconds(), 10)
        pid = int(marker.read_text().strip())
        with self.assertRaises(ProcessLookupError):   # 子孙 sleep 也被整组杀掉
            os.kill(pid, 0)

    def test_notify_falls_back_to_smtp_when_lib_missing(self):
        calls = []
        with patch.object(monitor, "send_email", side_effect=lambda *a: calls.append(a)):
            used = monitor.notify(self.cfg(str(self.dir / "nope.sh")), "s", "<b>h</b>", "t")
        self.assertEqual(used, "smtp")
        self.assertEqual(len(calls), 1)

    def test_feishu_text_lists_all_items_and_off_shelf_mark(self):
        text = monitor.build_feishu_text({
            "A1CH/A": {"title": "T1", "price": "P1", "url": "https://u/1", "first_seen": "2026-10-01T08:00:00", "on_shelf": False},
            "B2CH/A": {"title": "T2", "price": "P2", "url": "https://u/2", "on_shelf": True},
        })
        for token in ("共 2 款", "A1CH/A", "B2CH/A", "https://u/1", "https://u/2", "P1", "P2"):
            self.assertIn(token, text)
        self.assertEqual(text.count("本次检测时已下架"), 1)

    def test_feishu_text_with_link_is_one_line_per_item(self):
        link = "http://example.invalid/apple-refurb/?v=202610010800"
        text = monitor.build_feishu_text({
            "A1CH/A": {"title": "翻新 Mac mini M4", "price": "RMB 3,999", "url": "https://u/1"},
        }, link)
        self.assertIn("汇总页有更新 · 新上架 1 款", text)
        self.assertIn("1. Mac mini M4  RMB 3,999（货号 A1CH/A）", text)
        self.assertIn(link, text)
        self.assertNotIn("https://u/1", text)       # 详情看汇总页，不再逐款贴官网链接
        self.assertIn("只提醒这一次", text)


SUMMARY_CONFIG = {**CONFIG, "summary": {"url": "http://example.invalid/apple-refurb/",
                                        "remote": "example:/srv/apple-refurb/"}}


class NotifiedLedgerTest(Harness):
    def test_migrates_from_seen_and_history_logs(self):
        # 旧 state：seen 里只剩在架的，已下架的推送记录只在日志里
        self.write_state({"first_run_done": True,
                          "seen": {"A1CH/A": {"title": "翻新 Mac Studio", "price": "P", "url": "u",
                                              "first_seen": "2026-09-01T10:00:00"}}})
        (self.dir / "monitor.log.pre-rotation").write_text(
            "[2026-09-20 13:01:02] 已通知 2 款（合并为 1 条，通道 feishu）: ['B2CH/A', 'C3CH/A']\n", encoding="utf-8")
        self.log_file.write_text(
            "[2026-09-21 12:54:00] 已通知 1 款（合并为 1 条，通道 smtp）: ['B2CH/A']\n"
            "[2026-09-21 12:55:00] 发现 1 款新上架: ['D4CH/A']\n", encoding="utf-8")
        FakeFeishu.ok = True
        self.html = page("A1CH/A", "B2CH/A", "C3CH/A")
        self.run_at(0)
        self.assertEqual(FakeFeishu.sent, [])          # 三款都推过/基线，一条都不推
        notified = self.state()["notified"]
        self.assertEqual(set(notified), {"A1CH/A", "B2CH/A", "C3CH/A"})
        self.assertEqual(notified["A1CH/A"]["via"], "migrated")
        self.assertEqual(notified["B2CH/A"]["first_notified"], "2026-09-20T13:01:02")   # 取最早一次
        self.assertNotIn("D4CH/A", notified)            # “发现”行不算推送过
        self.assertIn("从历史日志补回 2 款", self.log_file.read_text(encoding="utf-8"))
        self.run_at(5)                                  # 台账已建立，不再重复迁移
        self.assertEqual(self.log_file.read_text(encoding="utf-8").count("建立已推送台账"), 1)

    def test_pending_already_notified_is_dropped(self):
        self.write_state({"first_run_done": True, "seen": {},
                          "notified": {"A1CH/A": {"first_notified": "2026-09-01T10:00:00"}},
                          "pending": {"A1CH/A": {"title": "t", "price": "p", "url": "u"},
                                      "B2CH/A": {"title": "t2", "price": "p2", "url": "u2"}}})
        FakeFeishu.ok = True
        self.html = page("A1CH/A")
        self.run_at(0)
        self.assertEqual(len(FakeFeishu.sent), 1)
        self.assertIn("B2CH/A", FakeFeishu.sent[0])
        self.assertNotIn("A1CH/A", FakeFeishu.sent[0])
        self.assertIn("待通知里 1 款已推送过", self.log_file.read_text(encoding="utf-8"))
        self.assertEqual(set(self.state()["notified"]), {"A1CH/A", "B2CH/A"})

    def test_first_run_baseline_goes_into_ledger_without_push(self):
        FakeFeishu.ok = True
        self.html = page("A1CH/A")
        self.run_at(0)
        self.assertEqual(FakeFeishu.sent, [])
        self.assertEqual(self.state()["notified"]["A1CH/A"]["via"], "baseline")
        self.html = page()
        self.run_at(5)
        self.html = page("A1CH/A")
        self.run_at(10)
        self.assertEqual(FakeFeishu.sent, [])           # 基线货号下架再上架也不推

    def test_reset_baseline_keeps_ledger(self):
        self.write_state({"first_run_done": True, "seen": {},
                          "notified": {"A1CH/A": {"first_notified": "2026-09-01T10:00:00"}},
                          "catalog": {"A1CH/A": {"first_seen": "", "last_seen": "2026-09-01T10:00:00"}},
                          "pending": {"B2CH/A": {"title": "t", "price": "p", "url": "u"}}})
        monitor.cmd_reset_baseline()
        state = self.state()
        self.assertFalse(state["first_run_done"])
        self.assertEqual(set(state["notified"]), {"A1CH/A"})
        self.assertIn("A1CH/A", state["catalog"])
        self.assertNotIn("pending", state)
        FakeFeishu.ok = True
        self.html = page("A1CH/A", "C3CH/A")
        self.run_at(0)                                  # 重建基线：不推
        self.assertEqual(FakeFeishu.sent, [])
        self.assertEqual(set(self.state()["notified"]), {"A1CH/A", "C3CH/A"})


def row(pn, model="macstudio", price="RMB 9,999", raw="9999", is_new=False, watched=True,
        title="Mac Studio M4", url=None):
    return {"pn": pn, "title": title, "price": price, "raw_price": raw, "model": model,
            "url": url or f"https://www.apple.com.cn/shop/product/{pn.lower()}", "is_new": is_new, "watched": watched}


class SummaryRenderTest(unittest.TestCase):
    def test_safe_url_only_allows_apple_site(self):
        self.assertEqual(monitor._safe_url("https://www.apple.com.cn/shop/product/x"),
                         "https://www.apple.com.cn/shop/product/x")
        for bad in ("javascript:alert(1)", "https://www.apple.com.cn.evil.example/x", "", None, 42):
            self.assertEqual(monitor._safe_url(bad), monitor.REFURB_URL)

    def test_safe_url_drops_per_fetch_query(self):
        # 官网每次抓取给的 fnode 都不同；留着它，页面哈希每轮都变，每 5 分钟都会重传。
        a = monitor._safe_url("https://www.apple.com.cn/shop/product/g1cench/a?fnode=aaa#x")
        b = monitor._safe_url("https://www.apple.com.cn/shop/product/g1cench/a?fnode=bbb")
        self.assertEqual(a, "https://www.apple.com.cn/shop/product/g1cench/a")
        self.assertEqual(a, b)

    def test_rows_flag_new_from_pending_and_catalog(self):
        now = T0
        items = {"A1CH/A": {"title": "翻新 Mac Studio", "price": "P", "url": "javascript:x", "model": "macstudio"},
                 "B2CH/A": {"title": "翻新 MacBook Air", "price": "P", "url": "https://www.apple.com.cn/a", "model": "macbookair"},
                 "C3CH/A": {"title": "翻新 iMac", "price": "P", "url": "https://www.apple.com.cn/b", "model": "imac"}}
        catalog = {"A1CH/A": {"first_seen": ""},
                   "B2CH/A": {"first_seen": (now - timedelta(hours=2)).isoformat()},
                   "C3CH/A": {"first_seen": (now - timedelta(hours=30)).isoformat()}}
        rows = {r["pn"]: r for r in monitor.summary_rows(items, {"A1CH/A": {}}, {"A1CH/A": {}}, catalog, now)}
        self.assertTrue(rows["A1CH/A"]["is_new"])        # 在待通知里
        self.assertTrue(rows["B2CH/A"]["is_new"])        # 24 小时内首次发现
        self.assertFalse(rows["C3CH/A"]["is_new"])
        self.assertEqual(rows["A1CH/A"]["title"], "Mac Studio")
        self.assertEqual(rows["A1CH/A"]["url"], monitor.REFURB_URL)
        self.assertTrue(rows["A1CH/A"]["watched"])
        self.assertFalse(rows["B2CH/A"]["watched"])

    def test_render_sections_and_escaping(self):
        rows = [row("A1CH/A", is_new=True, title='<script>alert("x")</script>'),
                row("B2CH/A", price="RMB 12,999", raw="12999"),
                row("C3CH/A", model="macbookair", watched=False, price="RMB 6,999", raw="6999"),
                row("D4CH/A", model="macbookair", watched=False, price="RMB 5,999", raw="5999", is_new=True)]
        out = monitor.render_summary(rows, {"macstudio", "macmini"}, "2026-10-01T08:00:00", "2026-10-01T08:05:00")
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertIn("新上架 · 1 款", out)              # 非关注机型的新货号不进“新上架”区
        self.assertIn("Mac Studio · 2 款", out)
        self.assertIn("Mac mini · 0 款", out)
        self.assertIn("当前没有在架货号", out)
        self.assertIn("2 款 · 最低 RMB 5,999", out)      # 其余机型折叠，按价格排序
        self.assertEqual(out.count('<span class="badge">新</span>'), 3)   # 新上架区 + Mac Studio 区 + 折叠区
        self.assertLess(out.index("新上架 · 1 款"), out.index("Mac Studio · 2 款"))
        self.assertIn("共 4 款在架", out)
        self.assertIn("内容更新于 2026-10-01 08:00 · 最近核对 2026-10-01 08:05", out)

    def test_catalog_first_build_has_no_new_then_marks_and_prunes(self):
        state = {}
        monitor.update_catalog(state, {"A1CH/A": {}}, T0)
        self.assertEqual(state["catalog"]["A1CH/A"]["first_seen"], "")
        monitor.update_catalog(state, {"A1CH/A": {}, "B2CH/A": {}}, T0 + timedelta(minutes=5))
        self.assertEqual(state["catalog"]["B2CH/A"]["first_seen"], "2026-10-01T08:05:00")
        self.assertEqual(state["catalog"]["A1CH/A"]["first_seen"], "")
        later = T0 + timedelta(days=monitor.CATALOG_KEEP_DAYS + 1)
        monitor.update_catalog(state, {"B2CH/A": {}}, later)
        self.assertNotIn("A1CH/A", state["catalog"])    # 下架超过 60 天清掉
        self.assertIn("B2CH/A", state["catalog"])


class SummaryPublishTest(Harness):
    def setUp(self):
        super().setUp()
        site = self.dir / "site"
        self.uploads = []
        self.upload_ok = True

        def fake_upload(remote):
            if not self.upload_ok:
                raise RuntimeError("汇总页上传失败 rc=255: ssh: connect timed out")
            self.uploads.append((remote, (site / "index.html").read_text(encoding="utf-8")))

        for p in (patch.object(monitor, "SUMMARY_DIR", site),
                  patch.object(monitor, "SUMMARY_FILE", site / "index.html"),
                  patch.object(monitor, "upload_summary", side_effect=fake_upload)):
            p.start()
            self.addCleanup(p.stop)
        FakeFeishu.ok = True

    def run_cfg(self, minutes):
        with redirect_stdout(io.StringIO()):
            monitor.run_once(SUMMARY_CONFIG, now=T0 + timedelta(minutes=minutes))

    def test_new_item_push_carries_link_and_page_lists_it(self):
        self.write_state({"first_run_done": True, "seen": {}, "notified": {}})
        self.html = page("A1CH/A")
        self.run_cfg(0)
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(self.uploads[0][0], "example:/srv/apple-refurb/")
        self.assertIn("A1CH/A", self.uploads[0][1])
        self.assertIn("新上架 · 1 款", self.uploads[0][1])
        self.assertEqual(len(FakeFeishu.sent), 1)
        self.assertIn("http://example.invalid/apple-refurb/?v=202610010800", FakeFeishu.sent[0])
        self.assertIn("汇总页有更新", FakeFeishu.sent[0])
        state = self.state()
        self.assertEqual(state["page_status"], "ok")
        self.assertEqual(state["page_uploaded_hash"], state["page_hash"])

    def test_upload_only_on_change_or_heartbeat(self):
        self.html = page("A1CH/A")
        self.run_cfg(0)                                 # 首次运行：建基线，同时上传
        self.assertEqual(len(self.uploads), 1)
        self.run_cfg(5)                                 # 内容没变 → 不传
        self.run_cfg(25)
        self.assertEqual(len(self.uploads), 1)
        self.run_cfg(30)                                # 满 30 分钟 → 心跳重传
        self.assertEqual(len(self.uploads), 2)
        self.html = page("A1CH/A", "B2CH/A")
        self.run_cfg(35)                                # 内容变了 → 立即传
        self.assertEqual(len(self.uploads), 3)
        self.assertEqual(len(FakeFeishu.sent), 1)       # 只有新货号 B2 推送一次
        self.html = page("A1CH/A")
        self.run_cfg(40)                                # 下架只改页面，不推送
        self.assertEqual(len(self.uploads), 4)
        self.assertEqual(len(FakeFeishu.sent), 1)

    def test_upload_failure_does_not_block_push_and_falls_back_to_detail(self):
        self.write_state({"first_run_done": True, "seen": {}, "notified": {}})
        self.upload_ok = False
        self.html = page("A1CH/A")
        self.run_cfg(0)
        self.assertEqual(len(FakeFeishu.sent), 1)
        self.assertNotIn("example.invalid/apple-refurb", FakeFeishu.sent[0])
        self.assertIn("https://www.apple.com.cn/shop/product/a1ch/a", FakeFeishu.sent[0])
        state = self.state()
        self.assertTrue(state["page_status"].startswith("error:"))
        self.assertEqual(state["pending"], {})
        self.assertIn("A1CH/A", state["notified"])
        self.assertIn("汇总页生成或上传失败（不影响推送）", self.log_file.read_text(encoding="utf-8"))
        self.upload_ok = True
        self.run_cfg(5)                                 # 上传恢复：补传页面，但不再推送
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(len(FakeFeishu.sent), 1)
        self.assertEqual(self.state()["page_status"], "ok")

    def test_render_crash_does_not_block_push(self):
        self.write_state({"first_run_done": True, "seen": {}, "notified": {}})
        self.html = page("A1CH/A")
        with patch.object(monitor, "render_summary", side_effect=ValueError("boom")):
            self.run_cfg(0)
        self.assertEqual(len(FakeFeishu.sent), 1)
        self.assertEqual(self.uploads, [])
        self.assertIn("boom", self.state()["page_status"])


if __name__ == "__main__":
    unittest.main()
