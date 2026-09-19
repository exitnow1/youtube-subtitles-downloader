"""대시보드 영상 목록 테스트 (stdlib unittest, 네트워크 없음).

runs행 ↔ 로그파일 ↔ 시도 영상 목록의 연결을 검증.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yt_dlp_subtitle_downloader as app
from subtitle_db import (
    db_init, db_run_start, db_run_finish, db_recent_runs, db_record_start,
    db_record_finish, db_run_items,
)


class DashboardItemsTest(unittest.TestCase):
    def setUp(self):
        self.conn = db_init(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_runs_has_log_file_column(self):
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(runs)").fetchall()}
        self.assertIn("log_file", cols)

    def test_run_start_records_log_file(self):
        rid = db_run_start(self.conn, "single", "http://x", log_file="run_20260920_000000.log")
        row = self.conn.execute("SELECT * FROM runs WHERE id = ?", (rid,)).fetchone()
        self.assertEqual(row["log_file"], "run_20260920_000000.log")

    def _seed_run_with_items(self):
        rid = db_run_start(self.conn, "single", "3 urls", log_file="run_t.log")
        j1 = db_record_start(self.conn, "http://v1", "영상1", "single",
                             ["ko"], True, "vtt/best")
        db_record_finish(self.conn, j1, "success", "", "영상1", "/p/1.ko.vtt", "vid00000001")
        j2 = db_record_start(self.conn, "http://v2", "영상2", "single",
                             ["ko"], True, "vtt/best")
        db_record_finish(self.conn, j2, "failed", "429", "영상2", "", "vid00000002")
        db_run_finish(self.conn, rid, 2, 1, 1, 0, 0, "")
        run = [r for r in db_recent_runs(self.conn) if r["id"] == rid][0]
        return run

    def test_run_items_returns_window_attempts(self):
        run = self._seed_run_with_items()
        items = db_run_items(self.conn, run)
        self.assertEqual([it["title"] for it in items], ["영상1", "영상2"])
        self.assertEqual(items[0]["url"], "http://v1")
        self.assertEqual(items[0]["status"], "success")

    def test_run_items_excludes_outside_window(self):
        run = self._seed_run_with_items()
        j3 = db_record_start(self.conn, "http://v3", "영상3", "single",
                             ["ko"], True, "vtt/best")
        db_record_finish(self.conn, j3, "success", "", "영상3", "/p/3.ko.vtt", "vid00000003")
        # 바깥 시도의 started_at을 실행창 이전으로 당김
        self.conn.execute(
            "UPDATE downloads SET started_at = '2000-01-01T00:00:00',"
            " finished_at = '2000-01-01T00:00:01' WHERE id = ?", (j3,))
        self.conn.commit()
        items = db_run_items(self.conn, run)
        self.assertEqual([it["title"] for it in items], ["영상1", "영상2"])

    def test_match_log_prefers_exact_name(self):
        runs = [{"id": 1, "log_file": "run_A.log", "started_at": "2026-09-20T00:00:00"},
                {"id": 2, "log_file": "", "started_at": "2026-09-20T00:00:05"}]
        self.assertEqual(app.match_log_to_run("run_A.log", "2026-09-20T00:00:00", runs)["id"], 1)

    def test_match_log_falls_back_to_nearest_time(self):
        from datetime import datetime
        runs = [{"id": 7, "log_file": "", "started_at": "2026-09-20T00:10:00"}]
        hit = app.match_log_to_run(
            "run_20260920_001000.log", datetime(2026, 9, 20, 0, 10, 3).timestamp(), runs)
        self.assertEqual(hit["id"], 7)
        far = app.match_log_to_run(
            "run_20260920_050000.log", datetime(2026, 9, 20, 5, 0, 0).timestamp(), runs)
        self.assertIsNone(far)


if __name__ == "__main__":
    unittest.main()
