"""자막 다운로드 작업 기록 DB (SQLite).

왜 DB인가: 텍스트 로그는 사람이 읽긴 좋지만 "실패한 것만 골라 다시 시도" 같은
기계적인 작업엔 DB가 정확합니다. 아래 1개 테이블에 시도~완료 전 과정을 남깁니다.

downloads 테이블 컬럼:
  id            시도 번호 (자동 증가, 재시도마다 새 행 → 이력이 남음)
  url           영상 주소
  title         영상 제목 (시작 시엔 목록 제목/URL, 완료 시 실제 제목으로 갱신)
  mode          channel / playlist / single / retry
  langs         'ko,en' 형태
  auto_subs     1/0
  sub_format    'vtt/best' 등
  status        running(진행중-비정상종료 흔적) / success / skipped / failed
  reason        스킵 사유 또는 실패 오류 메시지
  subtitle_path 저장된 자막 파일 경로 (여러 개면 줄바꿈 구분)
  attempt_no    같은 URL의 몇 번째 시도인지
  started_at    시도 시작 시각 (ISO, 예: 2026-09-05T20:30:01)
  finished_at   시도 종료 시각 (ISO)
"""

import os
import sqlite3
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS downloads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url TEXT NOT NULL,
  title TEXT,
  mode TEXT,
  langs TEXT,
  auto_subs INTEGER DEFAULT 1,
  sub_format TEXT,
  status TEXT NOT NULL,
  reason TEXT DEFAULT '',
  subtitle_path TEXT DEFAULT '',
  attempt_no INTEGER DEFAULT 1,
  started_at TEXT NOT NULL,
  finished_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads(status);
CREATE INDEX IF NOT EXISTS idx_downloads_url ON downloads(url);
"""


def now_iso() -> str:
    """현재 시각 ISO 문자열 (초 단위)."""
    return datetime.now().isoformat(timespec="seconds")


def db_init(path: str) -> sqlite3.Connection:
    """DB 파일 준비 (없으면 생성 + 테이블 생성). 호출부는 사용 후 conn.close()."""
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def db_next_attempt_no(conn: sqlite3.Connection, url: str) -> int:
    """같은 URL의 다음 시도 번호 (처음이면 1)."""
    row = conn.execute(
        "SELECT COALESCE(MAX(attempt_no), 0) + 1 AS n FROM downloads WHERE url = ?",
        (url,),
    ).fetchone()
    return int(row["n"])


def db_record_start(conn: sqlite3.Connection, url: str, title: str, mode: str,
                    langs, auto_subs: bool, sub_format: str) -> int:
    """시도 시작 기록 → 행 id 반환. 비정상 종료 시 status='running'으로 남음."""
    langs_str = ",".join(langs) if isinstance(langs, (list, tuple)) else str(langs)
    cur = conn.execute(
        """INSERT INTO downloads
           (url, title, mode, langs, auto_subs, sub_format, status, attempt_no, started_at)
           VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?)""",
        (url, title, mode, langs_str, 1 if auto_subs else 0, sub_format,
         db_next_attempt_no(conn, url), now_iso()),
    )
    conn.commit()
    return int(cur.lastrowid)


def db_record_finish(conn: sqlite3.Connection, job_id: int, status: str,
                     reason: str = "", title: str | None = None,
                     subtitle_path: str = "") -> None:
    """시도 종료 기록. status는 success / skipped / failed 중 하나."""
    if title is not None:
        conn.execute(
            """UPDATE downloads SET status=?, reason=?, title=?,
               subtitle_path=?, finished_at=? WHERE id=?""",
            (status, reason, title, subtitle_path, now_iso(), job_id),
        )
    else:
        conn.execute(
            """UPDATE downloads SET status=?, reason=?,
               subtitle_path=?, finished_at=? WHERE id=?""",
            (status, reason, subtitle_path, now_iso(), job_id),
        )
    conn.commit()


def db_last_success(conn: sqlite3.Connection, url: str, langs_str: str):
    """같은 URL+언어의 가장 최근 시도가 success면 그 행 dict, 아니면 None.

    P1-1 중복 방지용: 이미 받은 자막은 유튜브에 다시 물어보지 않습니다.
    """
    row = conn.execute(
        """SELECT * FROM downloads WHERE url = ? AND langs = ?
           ORDER BY id DESC LIMIT 1""",
        (url, langs_str),
    ).fetchone()
    if row is not None and row["status"] == "success":
        return dict(row)
    return None


def db_latest_failed(conn: sqlite3.Connection):
    """URL별 '가장 최근 시도'가 failed인 목록 (재시도 대상). 오래된 실패 순으로 정렬."""
    rows = conn.execute(
        """SELECT * FROM downloads d1
           WHERE id = (SELECT MAX(id) FROM downloads d2 WHERE d2.url = d1.url)
             AND status = 'failed'
           ORDER BY finished_at ASC"""
    ).fetchall()
    return [dict(r) for r in rows]


def db_count_by_status(conn: sqlite3.Connection) -> dict:
    """상태별 최신( URL별 마지막 시도) 개수 요약."""
    rows = conn.execute(
        """SELECT status, COUNT(*) AS c FROM downloads d1
           WHERE id = (SELECT MAX(id) FROM downloads d2 WHERE d2.url = d1.url)
           GROUP BY status"""
    ).fetchall()
    return {r["status"]: r["c"] for r in rows}
