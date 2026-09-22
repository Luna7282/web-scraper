"""config.py::validate_system_one_config -- pure, offline (no network, no
real key needed): checks the SHAPE of USE_SYSTEM_ONE/SYSTEM_ONE_PROVIDER/
the relevant API key or package, so a misconfiguration fails at startup
instead of on the first decision an hour into a crawl."""
import os
import unittest
from unittest.mock import patch

import config


class TestValidateSystemOneConfig(unittest.TestCase):
    def test_disabled_is_a_no_op_regardless_of_provider(self):
        with patch.object(config, "USE_SYSTEM_ONE", False), \
             patch.object(config, "SYSTEM_ONE_PROVIDER", "not-a-real-provider"):
            config.validate_system_one_config()  # must not raise

    def test_enabled_with_no_provider_raises(self):
        with patch.object(config, "USE_SYSTEM_ONE", True), \
             patch.object(config, "SYSTEM_ONE_PROVIDER", None):
            with self.assertRaises(config.SystemOneConfigError):
                config.validate_system_one_config()

    def test_enabled_with_invalid_provider_raises(self):
        with patch.object(config, "USE_SYSTEM_ONE", True), \
             patch.object(config, "SYSTEM_ONE_PROVIDER", "chatgpt"):
            with self.assertRaises(config.SystemOneConfigError):
                config.validate_system_one_config()

    def test_jev_without_typesafe_key_raises(self):
        env = dict(os.environ)
        env.pop("TYPESAFE_API_KEY", None)
        with patch.object(config, "USE_SYSTEM_ONE", True), \
             patch.object(config, "SYSTEM_ONE_PROVIDER", "jev"), \
             patch.dict(os.environ, env, clear=True):
            with self.assertRaises(config.SystemOneConfigError):
                config.validate_system_one_config()

    def test_jev_with_typesafe_key_passes(self):
        with patch.object(config, "USE_SYSTEM_ONE", True), \
             patch.object(config, "SYSTEM_ONE_PROVIDER", "jev"), \
             patch.dict(os.environ, {"TYPESAFE_API_KEY": "sk-test"}):
            config.validate_system_one_config()  # must not raise

    def test_laya_without_package_installed_raises(self):
        # laya IS a real dependency now (installed for real, see
        # LESSONS_LEARNED.md -- the numpy<2 dry-run check came back clean)
        # -- simulate absence via importlib.util.find_spec rather than
        # relying on ambient environment state, so this stays meaningful
        # regardless of what's actually installed on disk.
        with patch.object(config, "USE_SYSTEM_ONE", True), \
             patch.object(config, "SYSTEM_ONE_PROVIDER", "laya"), \
             patch("config.importlib.util.find_spec", return_value=None):
            with self.assertRaises(config.SystemOneConfigError):
                config.validate_system_one_config()

    def test_laya_with_package_installed_passes(self):
        with patch.object(config, "USE_SYSTEM_ONE", True), \
             patch.object(config, "SYSTEM_ONE_PROVIDER", "laya"), \
             patch("config.importlib.util.find_spec", return_value=object()):
            config.validate_system_one_config()  # must not raise


if __name__ == "__main__":
    unittest.main()
