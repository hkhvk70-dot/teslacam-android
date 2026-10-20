import contextlib
import tempfile
import unittest
from unittest.mock import patch

import offline_support
import teslacam as tc


class CleanGuardTests(unittest.TestCase):
    def test_exported_or_unknown_never_probe_or_change_resources(self):
        for state in ("exported", "unknown"):
            with self.subTest(state=state), tempfile.TemporaryDirectory(dir=offline_support.SCRATCH) as tmp:
                c = dict(tc.DEFAULTS, run=tmp, state=tmp, cfg=tmp)
                with patch.object(tc.gate, "control_lock", return_value=contextlib.nullcontext()), \
                        patch.object(tc.gate, "read_lun", return_value=(state, None)), \
                        patch.object(tc.locks, "MountLock") as lock, \
                        patch.object(tc, "cleanup_orphans") as cleanup, \
                        patch.object(tc.locks, "probe_all") as probe:
                    out = {"steps": []}
                    code = tc.clean_orphans_body.__wrapped__(c, out, True)
                self.assertEqual(code, tc.EXIT_REFUSED)
                self.assertEqual(out.get("failed_at"), "lun_precondition")
                lock.assert_not_called()
                cleanup.assert_not_called()
                probe.assert_not_called()

    def test_confirm_is_exact_boolean_and_does_not_eat_next_option(self):
        cmd, c, reason = tc.parse(["teslacam.py", "clean-orphans", "--confirm", "--state", "fixture"])
        self.assertEqual(cmd, "clean-orphans")
        self.assertIs(c["confirm"], True)
        self.assertEqual(c["state"], "fixture")
        _, c, _ = tc.parse(["teslacam.py", "clean-orphans", "unconfirmed"])
        self.assertIsNot(c.get("confirm"), True)


if __name__ == "__main__":
    unittest.main()
