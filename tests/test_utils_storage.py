import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from facebook_caretool.account_io import build_export_payload, merge_accounts, parse_import_payload
from facebook_caretool.care_planner import build_care_plan, format_care_plan, recommend_care_profile
from facebook_caretool.analytics import summarize_accounts, summarize_logs
from facebook_caretool.storage import JsonStorage, SQLiteStorage
from facebook_caretool.utils import build_comment_payloads, load_json, parse_delay, parse_proxy, save_json, spin_content


class UtilsTest(unittest.TestCase):
    def test_parse_proxy_empty(self):
        self.assertIsNone(parse_proxy(""))
        self.assertIsNone(parse_proxy(None))

    def test_parse_proxy_host_port(self):
        self.assertEqual(parse_proxy("127.0.0.1:8080"), {"server": "http://127.0.0.1:8080"})

    def test_parse_proxy_with_auth_and_colon_password(self):
        self.assertEqual(
            parse_proxy("proxy.local:3128:user:p:a:s:s"),
            {"server": "http://proxy.local:3128", "username": "user", "password": "p:a:s:s"},
        )

    def test_parse_proxy_with_scheme(self):
        self.assertEqual(parse_proxy("socks5://proxy.local:1080"), {"server": "socks5://proxy.local:1080"})

    def test_parse_proxy_invalid(self):
        with self.assertRaises(ValueError):
            parse_proxy("proxy.local")

    def test_parse_delay_range_and_single(self):
        self.assertEqual(parse_delay("4-9"), (4.0, 9.0))
        self.assertEqual(parse_delay("5"), (5.0, 5.0))
        self.assertEqual(parse_delay(""), (4.0, 9.0))

    def test_parse_delay_invalid_order(self):
        with self.assertRaises(ValueError):
            parse_delay("9-4")

    def test_spin_content_uses_injected_chooser(self):
        def first(options):
            return options[0]

        self.assertEqual(spin_content("{Chào|Hi} {bạn|anh}" , chooser=first), "Chào bạn")

    def test_spin_content_nested(self):
        def last(options):
            return options[-1]

        self.assertEqual(spin_content("Xin {chào|{hi|hello}}", chooser=last), "Xin hello")

    def test_build_comment_payloads_keeps_media_with_comment(self):
        payloads = build_comment_payloads("cmt 1\ncmt 2\ncmt 3", ["a.jpg", "b.jpg"])

        self.assertEqual(
            payloads,
            [
                {"text": "cmt 1", "media_path": "a.jpg"},
                {"text": "cmt 2", "media_path": "b.jpg"},
                {"text": "cmt 3", "media_path": "a.jpg"},
            ],
        )

    def test_build_comment_payloads_allows_text_only_comments(self):
        self.assertEqual(
            build_comment_payloads("cmt 1\n\ncmt 2"),
            [{"text": "cmt 1", "media_path": ""}, {"text": "cmt 2", "media_path": ""}],
        )


class JsonStorageTest(unittest.TestCase):
    def test_load_save_json_helpers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.json"
            self.assertEqual(load_json(path, []), [])
            save_json(path, [{"name": "Tuấn"}])
            self.assertEqual(load_json(path, []), [{"name": "Tuấn"}])

    def test_json_storage_normalizes_account_and_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            accounts_path = Path(tmp) / "accounts.json"
            logs_path = Path(tmp) / "logs.json"
            storage = JsonStorage(str(accounts_path), str(logs_path))
            storage.save_accounts([{"name": "A", "status": "bad"}])
            storage.save_logs([{"account": "A", "status": "done", "extra": 1}])

            accounts = storage.load_accounts()
            logs = storage.load_logs()

            self.assertEqual(accounts[0]["status"], "active")
            self.assertEqual(logs[0]["extra"], 1)

    def test_sqlite_storage_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = SQLiteStorage(str(Path(tmp) / "caretool.db"))
            storage.save_accounts([{"name": "A", "proxy": "127.0.0.1:8080"}])
            storage.save_logs([{"account": "A", "status": "done", "extra": 1}])

            self.assertEqual(storage.load_accounts()[0]["name"], "A")
            self.assertEqual(storage.load_logs()[0]["extra"], 1)


class AccountImportExportTest(unittest.TestCase):
    def test_export_payload_redacts_sensitive_fields_by_default(self):
        payload = build_export_payload([
            {"name": "A", "uid": "1", "password": "secret", "two_fa": "ABC"}
        ])

        exported = payload["accounts"][0]
        self.assertEqual(exported["password"], "")
        self.assertEqual(exported["two_fa"], "")

    def test_export_payload_can_include_sensitive_fields(self):
        payload = build_export_payload([
            {"name": "A", "uid": "1", "password": "secret", "two_fa": "ABC"}
        ], include_sensitive=True)

        exported = payload["accounts"][0]
        self.assertEqual(exported["password"], "secret")
        self.assertEqual(exported["two_fa"], "ABC")

    def test_parse_import_payload_accepts_wrapped_accounts(self):
        accounts = parse_import_payload({"accounts": [{"name": "A", "status": "active"}]})
        self.assertEqual(accounts[0]["name"], "A")

    def test_merge_accounts_skips_or_overwrites_duplicates(self):
        current = [{"name": "A", "uid": "1", "note": "old"}]
        imported = [{"name": "A new", "uid": "1", "note": "new"}, {"name": "B", "uid": "2"}]

        merged, stats = merge_accounts(current, imported, overwrite=False)
        self.assertEqual(stats, {"added": 1, "updated": 0, "skipped": 1})
        self.assertEqual(merged[0]["note"], "old")

        merged, stats = merge_accounts(current, imported, overwrite=True)
        self.assertEqual(stats, {"added": 1, "updated": 1, "skipped": 0})
        self.assertEqual(merged[0]["note"], "new")


class AnalyticsTest(unittest.TestCase):
    def test_summarize_accounts_and_logs(self):
        account_summary = summarize_accounts([
            {"status": "active"}, {"status": "checkpoint"}, {"status": "cookie_error"}, {}
        ])
        self.assertEqual(account_summary["total"], 4)
        self.assertEqual(account_summary["active"], 2)

        log_summary = summarize_logs([
            {"account": "A", "status": "done", "start_time": "01/05/2026 10:00"},
            {"account": "A", "status": "error", "time": "01/05/2026 11:00"},
            {"account": "B", "status": "done", "time": "02/05/2026 11:00"},
        ])
        self.assertEqual(log_summary["total"], 3)
        self.assertEqual(log_summary["by_day"]["01/05/2026"], 2)
        self.assertEqual(log_summary["by_account"]["A"], 2)
        self.assertEqual(log_summary["by_status"]["done"], 2)


class CarePlannerTest(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "newsfeed_minutes": 15,
            "reels_minutes": 15,
            "pause_range": "4-9",
            "auto_like": True,
        }
        self.now = datetime(2026, 5, 11, 12, 0)

    def test_recommend_warmup_for_never_cared_account(self):
        account = {"status": "active", "last_care": "Chưa nuôi", "note": "acc mới"}
        self.assertEqual(recommend_care_profile(account, now=self.now), "warmup")
        plan = build_care_plan(account, self.settings, now=self.now)
        self.assertEqual(plan["newsfeed_minutes"], 3)
        self.assertEqual(plan["reels_minutes"], 2)
        self.assertFalse(plan["auto_like"])

    def test_recommend_rest_for_checkpoint_account(self):
        account = {"status": "checkpoint", "last_care": "10/05/2026 10:00"}
        plan = build_care_plan(account, self.settings, now=self.now)
        self.assertEqual(plan["profile"], "rest")
        self.assertEqual(plan["newsfeed_minutes"], 0)
        self.assertEqual(plan["reels_minutes"], 0)

    def test_manual_profile_keeps_global_settings(self):
        account = {"status": "active", "care_profile": "manual", "last_care": "10/05/2026 10:00"}
        plan = build_care_plan(account, self.settings, now=self.now)
        self.assertEqual(plan["profile"], "manual")
        self.assertEqual(plan["newsfeed_minutes"], 15)
        self.assertEqual(plan["reels_minutes"], 15)
        self.assertIn("Newsfeed 15p", format_care_plan(plan))

    def test_plan_includes_notifications_and_occasional_group_joining(self):
        settings = {
            **self.settings,
            "read_notifications": True,
            "join_groups": True,
            "join_group_chance": 0.5,
            "max_join_groups": 2,
        }
        account = {"status": "active", "care_profile": "balanced", "last_care": "10/05/2026 10:00"}
        plan = build_care_plan(account, settings, now=self.now)

        self.assertTrue(plan["read_notifications"])
        self.assertTrue(plan["join_groups"])
        self.assertEqual(plan["max_join_groups"], 2)
        self.assertEqual(plan["join_group_chance"], 0.5)
        self.assertIn("đọc thông báo", format_care_plan(plan))
        self.assertIn("tham gia 1-2 group", format_care_plan(plan))


if __name__ == "__main__":
    unittest.main()
