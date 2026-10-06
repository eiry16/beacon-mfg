#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selfupdate.py 的单元测试（纯标准库；不联网、不安装、不写用户目录）

运行：python test_update.py
覆盖本次修复的三点：
  1) 任何接入方式都会检查（server.py 侧拉起；本文件测 run() 的行为）
  2) BEACON_SOURCE（自定义 HTTP 源）不再被跳过
  3) 按接入方式分流动作 + 一定落状态戳/给可见提示（不再静默）
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import selfupdate as su  # noqa: E402


class TempStateMixin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = (su.STATE_DIR, su.STAMP, su.LOCK)
        su.STATE_DIR = self.tmp.name
        su.STAMP = os.path.join(self.tmp.name, "update-check.json")
        su.LOCK = os.path.join(self.tmp.name, "update.lock")
        for k in ("BEACON_REPO", "BEACON_SOURCE", "BEACON_MCP_NO_UPDATE",
                  "BEACON_MCP_UPDATE_INTERVAL_H", "BEACON_MCP_UPDATE_QUIET"):
            os.environ.pop(k, None)

    def tearDown(self):
        su.STATE_DIR, su.STAMP, su.LOCK = self._orig
        for k in ("BEACON_REPO", "BEACON_SOURCE", "BEACON_MCP_NO_UPDATE",
                  "BEACON_MCP_UPDATE_INTERVAL_H", "BEACON_MCP_UPDATE_QUIET"):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def stamp(self):
        return su.read_stamp()


class TestSemver(TempStateMixin):
    def test_basic(self):
        self.assertTrue(su.semver_gt("1.5.8", "1.5.7"))
        self.assertTrue(su.semver_gt("1.6.0", "1.5.99"))
        self.assertTrue(su.semver_gt("2.0.0", "1.99.99"))
        self.assertFalse(su.semver_gt("1.5.7", "1.5.7"))
        self.assertFalse(su.semver_gt("1.5.6", "1.5.7"))

    def test_non_numeric_segments(self):
        self.assertTrue(su.semver_gt("1.5.10", "1.5.9"))
        self.assertFalse(su.semver_gt("1.5.7-rc1", "1.5.7"))


class TestModeDetection(TempStateMixin):
    def test_local_repo(self):
        os.environ["BEACON_REPO"] = self.tmp.name
        self.assertEqual(su.detect_mode(), su.MODE_LOCAL_REPO)

    def test_local_repo_missing_dir_is_not_local(self):
        os.environ["BEACON_REPO"] = os.path.join(self.tmp.name, "nope")
        self.assertNotEqual(su.detect_mode(), su.MODE_LOCAL_REPO)

    def test_npm_global(self):
        p = r"C:\Users\x\AppData\Roaming\npm\node_modules\beacon-mfg-mcp"
        self.assertEqual(su.detect_mode(p), su.MODE_NPM_GLOBAL)

    def test_npx_cache(self):
        p = r"C:\Users\x\AppData\Local\npm-cache\_npx\abc123\node_modules\beacon-mfg-mcp"
        self.assertEqual(su.detect_mode(p), su.MODE_NPX_CACHE)

    def test_other(self):
        self.assertEqual(su.detect_mode(r"C:\src\beacon-mfg\mcp"), su.MODE_OTHER)


class TestSkipLogic(TempStateMixin):
    def test_disabled_only_by_explicit_flag(self):
        """修复点 2：BEACON_SOURCE 不再导致跳过。"""
        os.environ["BEACON_SOURCE"] = "https://example.invalid"
        self.assertFalse(su.disabled())
        os.environ["BEACON_MCP_NO_UPDATE"] = "1"
        self.assertTrue(su.disabled())

    def test_custom_source_still_checks_registry(self):
        os.environ["BEACON_SOURCE"] = "https://example.invalid"
        with mock.patch.object(su, "fetch_latest", return_value="1.5.8"), \
             mock.patch.object(su, "detect_mode", return_value=su.MODE_NPM_GLOBAL), \
             mock.patch.object(su, "npm_install", return_value=(True, "")) as inst:
            r = su.run(apply=True)
        self.assertEqual(r["result"], "updated")
        inst.assert_called_once()
        self.assertEqual(self.stamp()["result"], "updated")


class TestLocalRepoBranch(TempStateMixin):
    def test_behind_notifies_and_records(self):
        os.environ["BEACON_REPO"] = self.tmp.name
        state = {"local": "a" * 40, "remote": "b" * 40, "behind": True}
        with mock.patch.object(su, "git_repo_state", return_value=state), \
             mock.patch.object(su, "note") as nt:
            r = su.run(apply=True)
        self.assertEqual(r["result"], "local_repo_behind")
        self.assertEqual(self.stamp()["result"], "local_repo_behind")
        nt.assert_called()                      # 有可见提示
        self.assertNotIn("latest", r)           # 不联网、不安装

    def test_uptodate_silent(self):
        os.environ["BEACON_REPO"] = self.tmp.name
        state = {"local": "a" * 40, "remote": "a" * 40, "behind": False}
        with mock.patch.object(su, "git_repo_state", return_value=state), \
             mock.patch.object(su, "note") as nt:
            r = su.run(apply=True)
        self.assertEqual(r["result"], "local_repo_uptodate")
        nt.assert_not_called()

    def test_remote_unreachable(self):
        os.environ["BEACON_REPO"] = self.tmp.name
        with mock.patch.object(su, "git_repo_state", return_value={"local": "a" * 40, "remote": None}):
            r = su.run(apply=True)
        self.assertEqual(r["result"], "local_repo_remote_unreachable")
        self.assertEqual(self.stamp()["result"], "local_repo_remote_unreachable")


class TestManualBranch(TempStateMixin):
    def test_npx_cache_gives_visible_notice_not_install(self):
        """修复点 3：npx 缓存无法自动升级 → 给可见提示而非静默。"""
        with mock.patch.object(su, "detect_mode", return_value=su.MODE_NPX_CACHE), \
             mock.patch.object(su, "fetch_latest", return_value="1.5.8"), \
             mock.patch.object(su, "npm_install") as inst, \
             mock.patch.object(su, "note") as nt:
            r = su.run(apply=True)
        self.assertEqual(r["result"], "update_available_manual")
        inst.assert_not_called()
        nt.assert_called()
        self.assertEqual(self.stamp()["latest"], "1.5.8")

    def test_registry_unreachable_records_stamp(self):
        with mock.patch.object(su, "detect_mode", return_value=su.MODE_NPM_GLOBAL), \
             mock.patch.object(su, "fetch_latest", return_value=None):
            r = su.run(apply=True)
        self.assertEqual(r["result"], "registry_unreachable")
        self.assertEqual(self.stamp()["result"], "registry_unreachable")

    def test_auto_update_failure_is_visible(self):
        with mock.patch.object(su, "detect_mode", return_value=su.MODE_NPM_GLOBAL), \
             mock.patch.object(su, "fetch_latest", return_value="1.5.8"), \
             mock.patch.object(su, "npm_install", return_value=(False, "EACCES")), \
             mock.patch.object(su, "note") as nt:
            r = su.run(apply=True)
        self.assertEqual(r["result"], "auto_update_failed")
        nt.assert_called()
        self.assertEqual(self.stamp()["error"], "EACCES")


class TestThrottleAndLock(TempStateMixin):
    def test_throttle_skips_when_recent(self):
        su.write_stamp({"result": "uptodate"})
        with mock.patch.object(su, "run") as runm:
            su.main(["selfupdate.py"])
        runm.assert_not_called()

    def test_force_bypasses_throttle(self):
        su.write_stamp({"result": "uptodate"})
        with mock.patch.object(su, "run", return_value={"result": "x"}) as runm:
            su.main(["selfupdate.py", "--force"])
        runm.assert_called_once()

    def test_interval_env_respected(self):
        su.write_stamp({"result": "uptodate"})
        os.environ["BEACON_MCP_UPDATE_INTERVAL_H"] = "0"
        with mock.patch.object(su, "run", return_value={"result": "x"}) as runm:
            su.main(["selfupdate.py"])
        runm.assert_called_once()

    def test_lock_fresh_blocks(self):
        os.makedirs(su.STATE_DIR, exist_ok=True)
        open(su.LOCK, "w").write("x")
        self.assertFalse(su.lock_held())
        su.unlock()

    def test_lock_stale_taken_over(self):
        os.makedirs(su.STATE_DIR, exist_ok=True)
        open(su.LOCK, "w").write("dead")
        old = time.time() - su.LOCK_STALE_S - 60
        os.utime(su.LOCK, (old, old))
        self.assertTrue(su.lock_held())
        su.unlock()
        self.assertFalse(os.path.exists(su.LOCK))

    def test_disabled_env_short_circuits(self):
        os.environ["BEACON_MCP_NO_UPDATE"] = "1"
        with mock.patch.object(su, "run") as runm:
            su.main(["selfupdate.py", "--force"])
        runm.assert_not_called()


class TestCli(TempStateMixin):
    def test_check_prints_json_without_installing(self):
        with mock.patch.object(su, "detect_mode", return_value=su.MODE_NPM_GLOBAL), \
             mock.patch.object(su, "fetch_latest", return_value="1.5.8"), \
             mock.patch.object(su, "npm_install") as inst, \
             mock.patch("sys.stdout") as out:
            rc = su.main(["selfupdate.py", "--check"])
        self.assertEqual(rc, 0)
        inst.assert_not_called()
        self.assertIn('"result": "update_available"', "".join(
            c.args[0] for c in out.write.call_args_list))

    def test_print_mode(self):
        with mock.patch("sys.stdout") as out:
            su.main(["selfupdate.py", "--print-mode"])
        self.assertTrue(out.write.called)


if __name__ == "__main__":
    unittest.main(verbosity=2)
