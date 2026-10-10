"""Локальные проверки конфигурации облачного запуска без Telegram и Railway."""

import json
import os
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot import Settings

PROJECT_DIR = Path(__file__).resolve().parents[1]


class DeploymentConfigCase(unittest.TestCase):
    def test_python_version_is_pinned_to_314(self):
        self.assertEqual((PROJECT_DIR / ".python-version").read_text().strip(), "3.14")

    def test_railpack_starts_existing_bot_without_web_server(self):
        config = json.loads((PROJECT_DIR / "railpack.json").read_text())
        self.assertEqual(config["$schema"], "https://schema.railpack.com")
        self.assertEqual(shlex.split(config["deploy"]["startCommand"]), ["python", "-u", "bot.py"])
        self.assertTrue((PROJECT_DIR / "bot.py").is_file())
        self.assertTrue((PROJECT_DIR / "requirements.txt").is_file())
        self.assertNotIn("steps", config)  # Установка зависимостей остаётся автоматической.

    def test_build_excludes_secrets_databases_and_local_environment(self):
        rules = set((PROJECT_DIR / ".dockerignore").read_text().splitlines())
        self.assertTrue({
            "**/.env", "**/.env.*", "**/.venv", "**/data", "**/backups",
            "**/*.sqlite3", "**/*.sqlite3-*", "**/*.db", "**/*.log",
        }.issubset(rules))

    def test_hosting_variables_take_priority_over_local_env(self):
        # Временный .env содержит только вымышленные значения; рабочий файл не читается.
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / ".env").write_text(
                "TELEGRAM_BOT_TOKEN=123456:LOCAL_EXAMPLE_FOR_TESTS_ONLY\n"
                "TIMEZONE=UTC\nDATABASE_PATH=local.sqlite3\n",
                encoding="utf-8",
            )
            cloud_database = base / "mounted-volume" / "tasks.sqlite3"
            values = {
                "TELEGRAM_BOT_TOKEN": "123456:CLOUD_EXAMPLE_FOR_TESTS_ONLY",
                "TIMEZONE": "Europe/Moscow",
                "DATABASE_PATH": str(cloud_database),
            }
            with patch("bot.BASE_DIR", base), patch.dict(os.environ, values, clear=True):
                settings = Settings.load()
            self.assertEqual(settings.token, values["TELEGRAM_BOT_TOKEN"])
            self.assertEqual(settings.timezone.key, "Europe/Moscow")
            self.assertEqual(settings.database, cloud_database)
            self.assertFalse(cloud_database.exists())  # Загрузка настроек не создаёт базу.


if __name__ == "__main__":
    unittest.main()
