"""ローカルファースト取り込み用パス境界の契約。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from knowledge_hub.config import (
    ConfigError,
    ensure_pipeline_directories,
    resolve_pipeline_paths,
    resolve_vault,
)


PIPELINE_ENV = {
    "KH_VAULT_PATH",
    "KH_ARCHIVE_PATH",
    "KH_INBOX_PATH",
    "KH_CARDS_PATH",
    "KH_STATE_DB_PATH",
}


class TestPipelinePaths(unittest.TestCase):
    def setUp(self):
        # 開発機の実設定がテスト結果へ混ざらないよう、対象変数だけ無効化する。
        self.environment = patch.dict(
            os.environ, {name: "" for name in PIPELINE_ENV}, clear=False
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_explicit_values_win_over_environment(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            explicit_vault = root_path / "explicit-vault"
            explicit_archive = root_path / "explicit-archive"
            env_vault = root_path / "env-vault"
            env_archive = root_path / "env-archive"
            for directory in (explicit_vault, explicit_archive, env_vault, env_archive):
                directory.mkdir()
            with patch.dict(os.environ, {
                "KH_VAULT_PATH": str(env_vault),
                "KH_ARCHIVE_PATH": str(env_archive),
            }, clear=False):
                paths = resolve_pipeline_paths(
                    str(explicit_vault), archive_value=str(explicit_archive)
                )
            self.assertEqual(paths.vault, explicit_vault.resolve())
            self.assertEqual(paths.archive, explicit_archive.resolve())

    def test_environment_and_derived_defaults(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            vault = root_path / "vault"
            archive = root_path / "archive"
            vault.mkdir()
            archive.mkdir()
            default_state_db = root_path / "local-state" / "jobs.sqlite3"
            with patch("knowledge_hub.config.DEFAULT_STATE_DB", default_state_db):
                with patch.dict(os.environ, {
                    "KH_VAULT_PATH": str(vault),
                    "KH_ARCHIVE_PATH": str(archive),
                }, clear=False):
                    paths = resolve_pipeline_paths()
            self.assertEqual(paths.inbox, (archive / "Inbox").resolve())
            self.assertEqual(paths.cards, (vault / "Cards").resolve())
            self.assertEqual(
                paths.state_db, default_state_db.resolve(strict=False)
            )
            # SQLiteの状態はGoogle Drive同期領域（Archive）へ置かない。
            with self.assertRaises(ValueError):
                paths.state_db.relative_to(archive.resolve())
            self.assertFalse(paths.inbox.exists())
            self.assertFalse(paths.cards.exists())
            self.assertFalse(paths.state_db.parent.exists())

    def test_environment_values_override_derived_defaults(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            vault = root_path / "vault"
            archive = root_path / "archive"
            vault.mkdir()
            archive.mkdir()
            custom_inbox = root_path / "incoming"
            custom_cards = root_path / "knowledge"
            custom_state = root_path / "state" / "ledger.sqlite3"
            with patch.dict(os.environ, {
                "KH_VAULT_PATH": str(vault),
                "KH_ARCHIVE_PATH": str(archive),
                "KH_INBOX_PATH": str(custom_inbox),
                "KH_CARDS_PATH": str(custom_cards),
                "KH_STATE_DB_PATH": str(custom_state),
            }, clear=False):
                paths = resolve_pipeline_paths()
            self.assertEqual(paths.inbox, custom_inbox.resolve())
            self.assertEqual(paths.cards, custom_cards.resolve())
            self.assertEqual(paths.state_db, custom_state.resolve())

    def test_explicit_subpaths_and_ensure_are_separate(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            vault = root_path / "vault"
            archive = root_path / "archive"
            vault.mkdir()
            archive.mkdir()
            inbox = root_path / "custom-inbox"
            cards = root_path / "custom-cards"
            state_db = root_path / "state" / "jobs.sqlite3"
            paths = resolve_pipeline_paths(
                str(vault), archive_value=str(archive), inbox_value=str(inbox),
                cards_value=str(cards), state_db_value=str(state_db),
            )
            self.assertFalse(inbox.exists())
            ensure_pipeline_directories(paths)
            self.assertTrue(inbox.is_dir())
            self.assertTrue(cards.is_dir())
            self.assertTrue(state_db.parent.is_dir())
            self.assertFalse(state_db.exists())

    def test_missing_archive_is_configuration_error(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            with self.assertRaisesRegex(ConfigError, "KH_ARCHIVE_PATH"):
                resolve_pipeline_paths(str(vault))

    def test_nonexistent_vault_and_archive_are_configuration_errors(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            archive = root_path / "archive"
            archive.mkdir()
            with self.assertRaises(ConfigError):
                resolve_pipeline_paths(str(root_path / "missing-vault"), archive_value=str(archive))
            vault = root_path / "vault"
            vault.mkdir()
            with self.assertRaises(ConfigError):
                resolve_pipeline_paths(str(vault), archive_value=str(root_path / "missing-archive"))

    def test_existing_resolve_vault_contract_is_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            with patch.dict(os.environ, {"KH_VAULT_PATH": str(vault)}, clear=False):
                self.assertEqual(resolve_vault(None), vault)
            self.assertEqual(resolve_vault(str(vault)), vault)


if __name__ == "__main__":
    unittest.main()
