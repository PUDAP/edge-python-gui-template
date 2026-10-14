import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import main


class ConfigTests(unittest.TestCase):
    def test_dotenv_loads_independent_settings_without_changing_environment(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            env = Path(directory) / ".env"
            base = "MACHINE_ID=test\nNATS_SERVERS=nats://localhost:4222\nTARGET_APP=Test\n"
            for ca, key in (("", ""), ("/ca.pem", ""), ("", "/key"), ("/ca.pem", "/key")):
                with self.subTest(ca=ca, key=key):
                    env.write_text(base + f"PUDA_NATS_CA={ca}\nPUDA_E2E_KEY_FILE={key}\n")
                    with patch.dict(main.Config.model_config, env_file=env):
                        config = main.load_config()
                    self.assertEqual(config.puda_nats_ca, ca)
                    self.assertEqual(config.puda_e2e_key_file, key)
                    self.assertNotIn("PUDA_NATS_CA", os.environ)
                    self.assertNotIn("PUDA_E2E_KEY_FILE", os.environ)
            with patch.dict(os.environ, PUDA_NATS_CA="/container/ca"):
                with patch.dict(main.Config.model_config, env_file=env):
                    self.assertEqual(main.load_config().puda_nats_ca, "/container/ca")
