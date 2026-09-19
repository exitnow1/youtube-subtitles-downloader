"""실시간 진행 대시보드 테스트 (stdlib unittest, 네트워크 없음)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yt_dlp_subtitle_downloader as app
from subtitle_db import db_init, db_record_start, db_record_finish, db_running_now


class LiveBoardTest(unittest.TestCase):
    def setUp(self):
        self.conn = db_init(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_running_now_returns_recent_running(self):
        j = db_record_start(self.conn, "http://v1", "진행중영상", "single",
                            ["ko"], True, "vtt/best")
        rows = db_running_now(self.conn, stale_minutes=30)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "진행중영상")
        self.assertEqual(rows[0]["url"], "http://v1")
        db_record_finish(self.conn, j, "success", "", "진행중영상", "/p", "vid00000001")
        self.assertEqual(db_running_now(self.conn, stale_minutes=30), [])

    def test_running_now_excludes_stale(self):
        j = db_record_start(self.conn, "http://v0", "좀비", "single",
                            ["ko"], True, "vtt/best")
        self.conn.execute(
            "UPDATE downloads SET started_at = '2000-01-01T00:00:00' WHERE id = ?", (j,))
        self.conn.commit()
        self.assertEqual(db_running_now(self.conn, stale_minutes=30), [])

    def test_read_log_tail_returns_last_lines(self):
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tail_tmp.log")
        try:
            with open(p, "w", encoding="utf-8") as f:
                for i in range(10):
                    f.write(f"line{i}\n")
            self.assertEqual(app.read_log_tail(p, n=3), ["line7", "line8", "line9"])
            self.assertEqual(app.read_log_tail(p + ".missing", n=3), [])
        finally:
            if os.path.exists(p):
                os.remove(p)

    def test_dashboard_has_live_cards(self):
        runs = [{"kind": "single", "target": "3 urls", "started_at": "2026-09-20T00:00:00",
                 "finished_at": "2026-09-20T00:01:00", "total": 3, "success": 3,
                 "failed": 0, "note": "", "items": []}]
        progress = [{"title": "진행중영상", "url": "http://v1", "mode": "single",
                     "started_at": "2026-09-20T00:02:00", "elapsed_sec": 42}]
        h = app.build_dashboard_html([], "2026-09-20 00:03:00", runs, [],
                                     progress=progress, logtail=["lineA", "lineB"])
        self.assertIn("http-equiv=\"refresh\"", h)
        self.assertIn("현재 진행 중", h)
        self.assertIn("진행중영상", h)
        self.assertIn("http://v1", h)
        self.assertIn("42초", h)
        self.assertIn("최근 로그", h)
        self.assertIn("lineA", h)


if __name__ == "__main__":
    unittest.main()
