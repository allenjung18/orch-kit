"""Runs the lean-claude-md trial suite (stub claude, no network) and asserts it is green."""
import os
import re
import shutil
import subprocess
import unittest

SUITE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "skills", "lean-claude-md", "trial.test.sh")


class LeanTrialSuite(unittest.TestCase):
    def test_trial_suite_passes(self):
        if not (shutil.which("bash") and shutil.which("jq")):
            self.skipTest("needs bash and jq")
        r = subprocess.run(["bash", SUITE, "--quiet"], capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, re.compile(r"\d+ passed, 0 failed"))


if __name__ == "__main__":
    unittest.main()
