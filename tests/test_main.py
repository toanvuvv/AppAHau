"""Tests for main.py config loading and processed_ids management."""
import json

from main import load_config, load_processed_ids, save_processed_ids, trim_processed_ids


class TestLoadConfig:
    def test_loads_valid_config(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "polling_interval_minutes: 5\naccounts:\n"
            "  - email: test@gmail.com\n"
            "    token_file: tokens/t.json\n"
            "    sheet_id: sid\n"
            "    sheet_tab: Sheet1\n"
            "    filter: 'is:unread'\n"
        )
        cfg = load_config(str(config_file))
        assert cfg["polling_interval_minutes"] == 5
        assert len(cfg["accounts"]) == 1
        assert cfg["accounts"][0]["email"] == "test@gmail.com"

    def test_raises_on_missing_file(self):
        try:
            load_config("nonexistent.yaml")
            assert False, "Should have raised"
        except FileNotFoundError:
            pass


class TestProcessedIds:
    def test_load_creates_empty_when_missing(self, tmp_path):
        path = str(tmp_path / "processed_ids.json")
        result = load_processed_ids(path)
        assert result == {}

    def test_load_reads_existing(self, tmp_path):
        path = str(tmp_path / "processed_ids.json")
        data = {"user@gmail.com": ["msg1", "msg2"]}
        with open(path, "w") as f:
            json.dump(data, f)
        result = load_processed_ids(path)
        assert result == data

    def test_save_and_reload(self, tmp_path):
        path = str(tmp_path / "processed_ids.json")
        data = {"u@g.com": ["a", "b"]}
        save_processed_ids(path, data)
        loaded = load_processed_ids(path)
        assert loaded == data

    def test_trim_keeps_max_10000(self):
        ids = list(range(10500))
        ids_str = [str(i) for i in ids]
        data = {"user@g.com": ids_str}
        trimmed = trim_processed_ids(data, max_per_account=10000)
        assert len(trimmed["user@g.com"]) == 10000
        assert trimmed["user@g.com"][0] == "500"
