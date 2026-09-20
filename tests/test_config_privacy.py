"""配置模板与凭据显示行为；只用构造的测试值，不访问外部服务。"""

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from omegaconf import OmegaConf

from app.clients.mysql_client_manager import MySQLClientManager
from app.conf.app_config import AppConfig, DBConfig, LLMConfig


class ConfigPrivacyTests(unittest.TestCase):
    def test_config_repr_does_not_show_credentials(self):
        db = DBConfig("localhost", 3306, "demo", "unit-test-only", "dw")
        llm = LLMConfig("demo", "unit-test-only", "https://example.invalid")
        self.assertNotIn("unit-test-only", repr(db))
        self.assertNotIn("unit-test-only", repr(llm))

    def test_url_encodes_special_characters_and_masks_password(self):
        value = "test-only:@/#%+ space"
        config = DBConfig("localhost", 3306, "demo", value, "dw")
        url = MySQLClientManager(config)._get_url()
        self.assertEqual(url.password, value)
        self.assertEqual(url.host, "localhost")
        self.assertEqual(url.database, "dw")
        self.assertNotIn(value, str(url))
        self.assertIn("***", str(url))

    def test_example_resolves_without_real_credentials(self):
        root = Path(__file__).resolve().parents[1]
        values = {
            "DB_META_PASSWORD": "unit-test-only",
            "DB_DW_PASSWORD": "unit-test-only",
            "LLM_API_KEY": "unit-test-only",
            "LLM_MODEL_NAME": "example-model",
            "LLM_BASE_URL": "https://example.invalid/v1",
        }
        with patch.dict(os.environ, values):
            configured = OmegaConf.to_object(
                OmegaConf.merge(
                    OmegaConf.structured(AppConfig),
                    OmegaConf.load(root / "conf/app_config.example.yaml"),
                )
            )
        self.assertEqual(configured.db_dw.password, values["DB_DW_PASSWORD"])
        self.assertEqual(configured.llm.api_key, values["LLM_API_KEY"])
        self.assertNotIn("unit-test-only", repr(configured))


if __name__ == "__main__":
    unittest.main()
