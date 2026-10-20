"""Offline P6 observations. Fixture files only; no USB, image repair or service."""
import json
import os
from pathlib import Path
import tempfile
import unittest
import types
from unittest.mock import patch

import offline_support
import status_view as sv
REAL_ACTIVE = sv._active


class StatusViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=offline_support.SCRATCH)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ("state", "run", "proc", "cfg/functions/mass_storage.0/lun.0"):
            (self.root / name).mkdir(parents=True)
        self.c = {"state": str(self.root / "state"), "run": str(self.root / "run"),
                  "proc_root": str(self.root / "proc"), "cfg": str(self.root / "cfg"),
                  "img": str(self.root / "image"), "services": "serve,pipeline",
                  "msd": "/data/teslacam/bin/msd.sh",
                  "services_conf": str(self.root / "services.json")}
        (self.root / "cfg/functions/mass_storage.0/lun.0/file").write_text("")
        (self.root / "image").write_bytes(b"fixture")
        (self.root / "state/initialized.json").write_text(json.dumps(
            {"image_id": sv.gate.image_id(self.c["img"])}))
        self.conf({})
        self.active_patch = patch.object(sv, "_active", return_value=("missing", None))
        self.active_patch.start()
        self.addCleanup(self.active_patch.stop)

    def conf(self, value):
        (self.root / "services.json").write_text(json.dumps(value))

    def proc(self, pid=123, pgid=123, sid=123, start=50, argv=None, token="fixture-secret"):
        folder = self.root / "proc" / str(pid)
        folder.mkdir(exist_ok=True)
        fields = ["S", "1", str(pgid), str(sid)] + ["0"] * 16
        fields[19] = str(start)
        (folder / "stat").write_text(str(pid) + " (test worker) " + " ".join(fields))
        (folder / "cmdline").write_bytes(b"\0".join(x.encode() for x in (argv or ["sh", "/data/teslacam/bin/serve.py"])) + b"\0")
        (folder / "environ").write_bytes((sv.pi.TOKEN_KEY + "=" + token).encode() + b"\0AUTH=must-not-leak\0")
        return folder

    def record(self, name="serve", pid=123, **updates):
        rec = {"instance": "fixture-secret", "pgid": pid, "sid": pid,
               "leader_pid": pid, "leader_starttime": 50}
        rec.update(updates)
        (self.root / "run" / (name + ".group")).write_text(json.dumps(rec))

    def build(self):
        return sv.build(self.c)

    def test_stopped_is_forbidden_and_dirty_detached_requires_check(self):
        (self.root / "state/stop_requested").write_text("1")
        (self.root / "state/export_dirty").write_text("1")
        out = self.build()
        self.assertEqual(out["permission"], "forbidden")
        self.assertIn("stop_requested", out["permission_reasons"])
        self.assertIn("requires_check:dirty", out["permission_reasons"])

    def test_normal_exported_dirty_is_not_failure(self):
        (self.root / "cfg/functions/mass_storage.0/lun.0/file").write_text("/data/teslacam/cam_disk.img")
        (self.root / "state/export_dirty").write_text("1")
        out = self.build()
        self.assertEqual(out["permission"], "allowed")
        self.assertEqual(out["permission_reasons"], [])

    def test_unknown_lun_is_forbidden(self):
        (self.root / "cfg/functions/mass_storage.0/lun.0/file").unlink()
        out = self.build()
        self.assertEqual(out["lun"], "unknown")
        self.assertIn("lun_unknown", out["permission_reasons"])

    def test_registered_alive_is_separate_from_process_ready(self):
        self.proc()
        self.record()
        self.conf({"serve": {"cmd": ["python3", "/data/teslacam/bin/serve.py"], "ready": {"type": "alive"}}})
        entry = self.build()["services"]["serve"]
        self.assertTrue(entry["alive"])
        self.assertTrue(entry["ready"])
        self.assertEqual(entry["ready_kind"], "process_alive")
        self.assertEqual(entry["identity"], "verified")

    def test_bad_token_is_unknown_and_reported_residue(self):
        self.proc(token="other-instance")
        self.record()
        out = self.build()
        self.assertEqual(out["services"]["serve"]["identity"], "unknown")
        self.assertIsNone(out["services"]["serve"]["ready"])
        self.assertTrue(out["residue"])
        self.assertTrue(out["unmanaged"])
        self.assertFalse(out["inspection"]["complete"])

    def test_unregistered_live_service_is_not_stopped(self):
        self.proc()
        out = self.build()
        entry = out["services"]["serve"]
        self.assertEqual(entry["state"], "unregistered")
        self.assertIs(entry["alive"], True)
        self.assertIsNone(entry["ready"])
        self.assertEqual(out["unmanaged"][0]["pid"], 123)

    def test_unregistered_without_match_stays_unknown(self):
        entry = self.build()["services"]["serve"]
        self.assertIsNone(entry["alive"])
        self.assertEqual(entry["state"], "unregistered")

    def test_config_deleted_registration_still_visible(self):
        self.c["services"] = ""
        self.proc(argv=["sh", "/data/teslacam/bin/old-worker.sh"])
        self.record("removed-service")
        out = self.build()
        self.assertIn("removed-service", out["services"])
        self.assertTrue(out["services"]["removed-service"]["registered"])

    def test_configuration_names_extend_legacy_names(self):
        self.conf({"new-worker": {"enabled": False}, "_comment": "ignore"})
        out = self.build()
        self.assertEqual(set(out["services"]), {"serve", "pipeline", "new-worker"})
        self.assertEqual(out["services"]["new-worker"]["state"], "disabled")

    def test_disabled_but_still_alive_is_residue(self):
        self.proc()
        self.record()
        self.conf({"serve": {"enabled": False}})
        out = self.build()
        self.assertEqual(out["services"]["serve"]["state"], "disabled")
        self.assertTrue(any(x["reason"] == "disabled_but_alive" for x in out["residue"]))

    def test_malformed_record_reports_unknown_without_echo(self):
        (self.root / "run/serve.group").write_text('{bad: "do-not-echo"')
        out = self.build()
        self.assertEqual(out["services"]["serve"]["identity"], "unknown")
        self.assertFalse(out["inspection"]["complete"])
        self.assertNotIn("do-not-echo", json.dumps(out))

    def test_partial_proc_never_becomes_empty_group(self):
        self.record()
        folder = self.root / "proc/123"
        folder.mkdir()
        (folder / "stat").write_text("malformed")
        out = self.build()
        self.assertIsNone(out["services"]["serve"]["alive"])
        self.assertEqual(out["services"]["serve"]["identity"], "unknown")
        self.assertFalse(out["inspection"]["proc"]["stat_complete"])

    def test_proc_root_unreadable_is_not_complete_empty(self):
        self.c["proc_root"] = str(self.root / "missing")
        out = self.build()
        self.assertFalse(out["inspection"]["complete"])
        self.assertIn("proc_unreadable", out["inspection"]["proc"]["reasons"])

    def test_proc_scan_has_explicit_bound(self):
        self.proc(123)
        self.proc(124)
        self.c["status_scan_limit"] = 1
        out = self.build()
        self.assertFalse(out["inspection"]["complete"])
        self.assertIn("scan_bound_reached", out["inspection"]["proc"]["reasons"])

    def test_exact_argv_match_only_not_substring(self):
        self.proc(argv=["python3", "/tmp/wrapper.py", "prefix/data/teslacam/bin/serve.py-suffix"])
        self.assertEqual(self.build()["unmanaged"], [])
        self.proc(argv=["sh", "-c", "sh /data/teslacam/bin/serve.py"])
        self.assertEqual(self.build()["unmanaged"], [])
        self.proc(argv=["python3", "/data/teslacam/bin/serve.py"])
        self.assertEqual(len(self.build()["unmanaged"]), 1)

    def test_unmanaged_collector_found(self):
        self.proc(argv=["sh", "/data/teslacam/bin/live_collect.sh"])
        out = self.build()
        self.assertEqual(out["unmanaged"][0]["services"], ["live_collector"])

    def test_config_exact_custom_path_is_recognized(self):
        self.conf({"custom": {"cmd": ["/opt/teslacam/private-worker"]}})
        self.proc(argv=["/opt/teslacam/private-worker", "--password", "must-not-echo"])
        out = self.build()
        self.assertEqual(out["unmanaged"][0]["services"], ["custom"])
        self.assertNotIn("must-not-echo", json.dumps(out))

    def test_generic_interpreter_path_does_not_claim_unrelated_python(self):
        self.conf({"serve": {"cmd": ["/usr/bin/python3.13", "/data/teslacam/bin/serve.py"]}})
        self.proc(argv=["/usr/bin/python3.13", "/opt/unrelated/server.py"])
        self.assertEqual(self.build()["unmanaged"], [])

    def tcp_service(self):
        self.proc()
        self.record()
        self.conf({"serve": {"ready": {"type": "tcp", "host": "127.0.0.1", "port": 8081}}})

    def test_other_process_port_cannot_make_ready(self):
        self.tcp_service()
        with patch.object(sv, "_tcp_owner", return_value=None), patch.object(sv, "_tcp_connect") as conn:
            out = self.build()
        self.assertTrue(out["services"]["serve"]["alive"])
        self.assertIsNone(out["services"]["serve"]["ready"])
        self.assertEqual(out["services"]["serve"]["reason"], "listener_owner_unknown")
        conn.assert_not_called()

    def test_owned_port_and_connection_are_both_required(self):
        self.tcp_service()
        with patch.object(sv, "_tcp_owner", return_value=True), patch.object(sv, "_tcp_connect", return_value=True):
            self.assertIs(self.build()["services"]["serve"]["ready"], True)
        with patch.object(sv, "_tcp_owner", return_value=True), patch.object(sv, "_tcp_connect", return_value=False):
            self.assertIs(self.build()["services"]["serve"]["ready"], False)

    def test_remote_ready_endpoint_never_connects(self):
        self.tcp_service()
        self.conf({"serve": {"ready": {"type": "tcp", "host": "192.0.2.99", "port": 8081}}})
        with patch.object(sv, "_tcp_connect") as conn:
            out = self.build()
        conn.assert_not_called()
        self.assertIsNone(out["services"]["serve"]["ready"])

    def test_socket_owner_parser_rejects_foreign_or_ambiguous_inode(self):
        self.proc()
        (self.root / "proc/net").mkdir()
        table = self.root / "proc/net/tcp"
        header = "sl local_address rem_address st tx rx tr tm retr uid timeout inode\n"
        own = "0: 0100007F:1F91 00000000:0000 0A 0:0 00:0 0 0 0 600\n"
        foreign = "1: 0100007F:1F91 00000000:0000 0A 0:0 00:0 0 0 0 700\n"
        table.write_text(header + own)
        with patch.object(sv, "_socket_inodes", return_value={"600"}):
            self.assertIs(sv._tcp_owner(self.c["proc_root"], "127.0.0.1", 8081, [123], {}), True)
            table.write_text(header + own + foreign)
            self.assertIsNone(sv._tcp_owner(self.c["proc_root"], "127.0.0.1", 8081, [123], {}))

    def test_running_incomplete_unknown_and_recovery_required_ops_block_export(self):
        for state, rec, expected in (
            ("unknown", None, "active_op_unknown"),
            ("valid", {"status": "running", "exit_code": None}, "active_op_running"),
            ("valid", {"status": "incomplete", "exit_code": 3}, "active_op_incomplete"),
            ("valid", {"status": "finished", "exit_code": 0, "recovery_required": True}, "active_op_recovery_required"),
        ):
            with self.subTest(expected=expected), patch.object(sv, "_active", return_value=(state, rec)):
                self.assertIn(expected, self.build()["permission_reasons"])

    def test_no_environment_or_secret_files_are_exported(self):
        self.proc()
        self.record()
        secret = self.root / "auth"
        secret.write_text("private-access-code")
        self.conf({"serve": {"cmd": ["python3", "/data/teslacam/bin/serve.py"],
                             "env_files": {"AUTH": str(secret)}, "env": {"TOKEN": "inline-secret"}}})
        original = __import__("builtins").open
        def guarded(path, *args, **kwargs):
            self.assertNotEqual(os.fspath(path), str(secret), "status must not open auth files")
            return original(path, *args, **kwargs)
        with patch("builtins.open", side_effect=guarded):
            out = self.build()
        text = json.dumps(out)
        for value in ("private-access-code", "inline-secret", "fixture-secret", "must-not-leak"):
            self.assertNotIn(value, text)

    def test_active_operation_is_sanitized_including_failure_summary(self):
        rec = {"op": "start", "id": "operation-id", "phase": "services", "status": "incomplete",
               "exit_code": 3, "recovery_required": True, "instance": "never-output-token",
               "owner": {"pid": 123, "starttime": 50, "boot_id": "not-needed"},
               "summary": {"failed_at": "services", "services_detail": [
                   {"name": "serve", "state": "failed", "reason": "spawn_failed:private-secret-argument",
                    "cmd": ["must-not-echo"]}],
                   "residual": [{"name": "serve", "reason": "identity_unknown", "pgid": 123}]}}
        fake = types.SimpleNamespace(read_active=lambda c: ("valid", rec))
        with patch.dict("sys.modules", {"lifecycle": fake}):
            state, safe = REAL_ACTIVE(self.c)
        self.assertEqual(state, "valid")
        self.assertEqual(safe["op"], "start")
        self.assertTrue(safe["recovery_required"])
        self.assertEqual(safe["summary"]["services_detail"][0]["reason"], "spawn_failed")
        text = json.dumps(safe)
        self.assertNotIn("private-secret-argument", text)
        self.assertNotIn("never-output-token", text)
        self.assertNotIn("must-not-echo", text)
        with patch.object(sv, "_active", return_value=(state, safe)):
            out = self.build()
        self.assertTrue(any(x.get("reason") == "spawn_failed" for x in out["failures"]))
        self.assertTrue(any(x.get("source") == "last_operation" for x in out["residue"]))

    def test_status_is_read_only(self):
        self.proc()
        self.record()
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.build()
        after = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
