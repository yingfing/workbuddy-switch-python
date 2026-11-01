"""platform_paths 三平台路径解析测试（路径规则移植自 WorkBuddy-Switch）。"""

import tempfile
import unittest
from pathlib import Path

from wbspy.platform_paths import candidate_dirs, find_local_auth

HOME = Path("/home/u")
WIN_ENV = {"APPDATA": "/home/u/AppData/Roaming",
           "LOCALAPPDATA": "/home/u/AppData/Local"}


class TestPlatformPaths(unittest.TestCase):
    def test_windows_config_root(self):
        dirs = candidate_dirs("trae", platform="win32", home=HOME, env=WIN_ENV)
        self.assertEqual(dirs[0], Path("/home/u/AppData/Roaming/Trae CN"))
        # 同名候选聚在前， LOCALAPPDATA / home 殿后
        self.assertEqual([d.name for d in dirs[:2]], ["Trae CN", "Trae"])

    def test_windows_qoder_reverse_domain(self):
        dirs = candidate_dirs("qodercn", platform="win32", home=HOME, env=WIN_ENV)
        self.assertEqual([d.name for d in dirs[:3]],
                         ["com.qodercn.app.stable", "Qoder CN", "QoderCN"])

    def test_macos(self):
        dirs = candidate_dirs("buddy", platform="darwin", home=HOME, env={})
        self.assertEqual(dirs[0],
                         Path("/home/u/Library/Application Support/.workbuddy"))

    def test_linux_xdg(self):
        dirs = candidate_dirs("workbuddy", platform="linux", home=HOME, env={})
        self.assertEqual(dirs[0], Path("/home/u/.config/.workbuddy-ai"))

    def test_find_hits_existing_file(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / ".config" / "Trae CN"
            target.mkdir(parents=True)
            (target / "workbuddy-desktop.info").write_text('{"account":{}}',
                                                           encoding="utf-8")
            self.assertEqual(
                find_local_auth("trae", platform="linux", home=td, env={}),
                target / "workbuddy-desktop.info")

    def test_find_miss_lists_searched(self):
        with self.assertRaises(FileNotFoundError) as cm:
            find_local_auth("trae", platform="linux", home="/nonexistent", env={})
        self.assertIn("已搜索", str(cm.exception))

    def test_unknown_channel(self):
        with self.assertRaises(RuntimeError):
            find_local_auth("nope")


if __name__ == "__main__":
    unittest.main()
