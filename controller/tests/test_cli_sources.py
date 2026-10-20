"""Consumer checks adapted from the isolation handoff; no production probes."""
import os
from pathlib import Path
import tempfile
import unittest

import offline_support
import teslacam as tc


class CLIFlags(unittest.TestCase):
    def test_hyphenated_flags_reach_expected_keys(self):
        flags = {"proc-root": "/fixture/proc", "services-conf": "/fixture/services.json",
                 "mount-lock": "/fixture/work.lock", "proc-mounts": "/fixture/mounts",
                 "sys-block": "/fixture/sys"}
        argv = ["teslacam.py", "status"]
        for flag, value in flags.items():
            argv.extend(["--" + flag, value])
        command, config, _ = tc.parse(argv)
        self.assertEqual(command, "status")
        for flag, value in flags.items():
            self.assertEqual(config[flag.replace("-", "_")], value)

    def test_no_hyphenated_keys(self):
        _, config, _ = tc.parse(["teslacam.py", "status", "--proc-root", "/fixture"])
        self.assertFalse([key for key in config if "-" in key])

    def test_confirm_is_boolean(self):
        _, config, _ = tc.parse(["teslacam.py", "clean-orphans", "--confirm"])
        self.assertIs(config["confirm"], True)


class ProbeSources(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=offline_support.SCRATCH, prefix="probe-source-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = self.root / "fixture.img"
        self.image.write_bytes(b"I" * 4096)
        self.blocks = self.root / "sys_block"
        self.blocks.mkdir()
        self.mounts = self.root / "mounts"
        self.mounts.write_text("")
        # Linux backing_file text starts with '/', even when its observation
        # files are hosted on Windows. probe_image never opens this identity.
        self.image_identity = "/fixture/teslacam.img"
        self.config = {"img": self.image_identity, "sys_block": str(self.blocks),
                       "proc_mounts": str(self.mounts)}

    def add_loop(self):
        backing = self.blocks / "loop77/loop/backing_file"
        backing.parent.mkdir(parents=True)
        backing.write_text(self.image_identity)

    def test_empty_sources_are_free(self):
        self.assertEqual(tc.probe_image(self.config)[0], "free")

    def test_only_injected_loop_is_detected(self):
        self.add_loop()
        state, info = tc.probe_image(self.config)
        self.assertEqual(state, "busy")
        self.assertEqual(info["loops"], ["loop77"])

    def test_injected_mount_table_is_consumed(self):
        self.add_loop()
        self.mounts.write_text("/dev/block/loop77 /fixture/mnt vfat ro 0 0\n")
        state, info = tc.probe_image(self.config)
        self.assertEqual(state, "busy")
        self.assertTrue(any("loop77" in item for item in info["mounts"]))


if __name__ == "__main__":
    unittest.main()
