"""자막 다운로더 기본값 테스트 (stdlib unittest, 네트워크 없음).

기본 정책: 자동자막만 + 메인 1트랙 (ko 자동 -> en 자동, 첫 성공에서 중단).
수동 자막은 --with-manual 로만 포함. 전 트랙은 --no-main-only 로 복원.
"""
import argparse
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yt_dlp_subtitle_downloader as app
from subtitle_db import db_init


def _default_cli_ns(**over):
    kw = dict(langs="", no_auto=False, with_manual=False, main_only=False,
              no_main_only=False, format="", encoding="", type="", date="",
              dur="", range="", keyword="", status="all", order="asc",
              select="all", retry_select=None)
    kw.update(over)
    return argparse.Namespace(**kw)


class DefaultPolicyTest(unittest.TestCase):
    def test_main_only_is_default_on(self):
        self.assertTrue(app.MAIN_ONLY)

    def test_include_manual_is_default_off(self):
        self.assertFalse(app.INCLUDE_MANUAL)

    def test_fallback_plan_splits_single_tracks_by_default(self):
        plan = app._build_fallback_plan(["ko", "en"], True)
        self.assertEqual(plan, [
            (["ko"], True, "메인 후보(ko,자동)"),
            (["en"], True, "메인 후보(en,자동)"),
        ])

    def test_cli_has_opt_out_flags(self):
        args = app.parse_cli(["--single", "https://www.youtube.com/watch?v=xxxxxxxxxxx"])
        self.assertFalse(args.with_manual)
        self.assertFalse(args.no_main_only)

    def test_cli_common_sets_auto_only_single_track_globals(self):
        app._cli_common(_default_cli_ns())
        self.assertTrue(app.MAIN_ONLY)
        self.assertFalse(app.INCLUDE_MANUAL)

    def test_make_sub_ydl_excludes_manual_by_default(self):
        app.INCLUDE_MANUAL = False
        try:
            ydl = app.make_sub_ydl("outtmpl", ["ko"], True, "vtt/best")
            self.assertFalse(ydl.params["writesubtitles"])
            self.assertTrue(ydl.params["writeautomaticsub"])
        finally:
            app.INCLUDE_MANUAL = False

    def test_make_sub_ydl_includes_manual_when_enabled(self):
        app.INCLUDE_MANUAL = True
        try:
            ydl = app.make_sub_ydl("outtmpl", ["ko"], True, "vtt/best")
            self.assertTrue(ydl.params["writesubtitles"])
        finally:
            app.INCLUDE_MANUAL = False

    def test_db_has_manual_main_columns(self):
        conn = db_init(":memory:")
        try:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(downloads)").fetchall()}
            self.assertIn("manual_subs", cols)
            self.assertIn("main_only", cols)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
