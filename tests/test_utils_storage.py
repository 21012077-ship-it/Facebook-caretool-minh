import tempfile
import unittest
from pathlib import Path

from facebook_caretool.storage import JsonStorage, SQLiteStorage
from facebook_caretool.utils import load_json, parse_delay, parse_proxy, save_json, spin_content


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


if __name__ == "__main__":
    unittest.main()
