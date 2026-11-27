import os
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(os.name == "nt", "Linux installer")
class InstallPolicyTests(unittest.TestCase):
    def check(self, version, permission):
        # Simulate every mutation. The effective permission comes from a fresh
        # query executed with the service identity, which the fixture checks.
        script = r"""
source ./install.sh
pkaction() { echo "pkaction version $TEST_VERSION"; }
install() { test -r "${@: -2:1}"; echo "POLICY ${@: -1}"; }
rm() { :; }
sleep() { :; }
runuser() {
  [[ "$1 $2 $3" == "-u campus-autologin --" ]] || return 1
  echo "org.freedesktop.NetworkManager.network-control:$TEST_PERMISSION"
}
install_network_policy
"""
        return subprocess.run(
            ["bash", "-c", script], cwd=str(ROOT), capture_output=True, text=True,
            env={**os.environ, "TEST_VERSION": version, "TEST_PERMISSION": permission},
        )

    def test_backend_and_effective_permission(self):
        self.assertIn(".pkla", self.check("0.105", "yes").stdout)
        self.assertIn(".rules", self.check("124", "yes").stdout)
        self.assertEqual(self.check("0.105", "yes").returncode, 0)
        self.assertNotEqual(self.check("124", "auth").returncode, 0)
