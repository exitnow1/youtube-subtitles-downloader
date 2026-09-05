"""YouTube 자막 배치 다운로더 (yt-dlp 기반, 자막 전용 모드).

세 가지 모드 + 실패 재시도 모드:
  1) 채널 전체  : 채널 링크를 주면 영상 목록을 가져와 자막을 일괄 다운로드
                  (업로드 날짜 6종 연산자 + 길이 + 번호 범위로 조건 지정 가능)
  2) 개별 영상  : 영상 URL 1개의 자막 다운로드
  3) 재생목록   : 재생목록 내 영상 자막 다운로드 (전체 or 조건 지정)
  4) 실패 재시도: DB에 남은 실패 목록을 골라 즉시/예약 재시도 (안전 간격 + 차단기)

모든 시도는 SQLite DB(subtitle_jobs.db)에 시작/종료 시각, 제목, URL 등과 함께
기록됩니다. 저장된 각 자막 파일 상단에는 다운로드 완료 시각·영상 제목·URL이
기입됩니다 (vtt=NOTE 주석, 그 외=# 주석).

영상 파일은 받지 않습니다 (--skip-download 상당). 영상당 요청은
"영상 정보 1회 + 자막 파일 1~2회"라 미디어 다운로드 대비 요청량이 1/100 수준이라
차단에 훨씬 안전합니다. 그래도 player API 단계에서 429를 맞을 수 있어
요청 간 휴식(sleep/jitter)과 429 백오프, 쿠키 재사용 패턴을 유지합니다.

필요 패키지: pip install yt-dlp browser_cookie3
"""

import os
import re
import sys
import time
import random
import argparse
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
import http.cookiejar as cookielib
import browser_cookie3  # pip install browser-cookie3
from yt_dlp import YoutubeDL  # pip install yt-dlp

from subtitle_db import (
    db_init, db_record_start, db_record_finish,
    db_latest_failed, db_count_by_status, db_last_success,
    db_cleanup_stale_running,
)

# =============== 환경 설정 ===============
DOWNLOAD_DIR = r"C:\Users\rpt53\Downloads"
SUBTITLE_DIR = os.path.join(DOWNLOAD_DIR, "subtitles")
COOKIE_FILENAME = "cookies.txt"
COOKIE_PATH = os.path.join(DOWNLOAD_DIR, COOKIE_FILENAME)
LOG_FILE = os.path.join(DOWNLOAD_DIR, "subtitle_download.log")
DB_PATH = os.path.join(DOWNLOAD_DIR, "subtitle_jobs.db")

# 결과 집계용
SUCCESS_LIST = []  # 다운로드 시도된 영상 제목
SKIPPED_LIST = []  # 조건에 안 맞아 건너뛴 영상: dict {title, reason}
FAIL_LIST = []     # 실패: dict {title, url}
CACHED_LIST = []   # 이미 받아둠 (DB 완료 기록 + 파일 존재 → 재요청 안 함)
NO_SUB_LIST = []   # 자막 자체가 없음 (모든 언어 후보 소진): dict {title, url, reason}

HEADLESS = False  # True면 알림 팝업 생략 (--headless / --retry-failed)

# --- 차단 방지 튜닝 값 (자막은 가벼우니 미디어 모드보다 짧게) ---
SLEEP_BETWEEN_VIDEOS_MIN = 2.0
SLEEP_BETWEEN_VIDEOS_MAX = 5.0
SLEEP_ON_429_BASE = 60  # 429 시 대기
COOKIE_REFRESH_EVERY_N_VIDEOS = 20  # 20개 영상마다 쿠키 갱신

# --- 재시도 안전값 (실패했던 영상들이라 더 여유 있게) ---
SLEEP_RETRY_MIN = 8.0
SLEEP_RETRY_MAX = 15.0
MAX_CONSECUTIVE_429 = 3  # 429가 3회 연속이면 나머지 중단 (차단기)

# 없는 영상(삭제/비공개/찾을 수 없음) 신호 - 재시도해도 살아나지 않으므로 1회로 종료
UNAVAILABLE_PATTERNS = (
    "video unavailable",
    "private video",
    "this video is unavailable",
    "this video is no longer available",
    "has been removed",
    "has been deleted",
    "account associated with this video has been terminated",
    "http error 404",
    ": 404",
    "not found",
)

# 폴백 언어 (요청 언어에 자막이 없을 때 마지막으로 물어볼 언어)
FALLBACK_LANGS = ("en",)

# 자막 파일 확장자 (헤더 기입 대상 탐색용)
SUBTITLE_EXTS = ("vtt", "srt", "ass", "ssa", "lrc", "ttml", "srv1", "srv2", "srv3", "json3")

# 날짜 연산자 (사용자 입력 → 내부 연산)
DATE_OPS = {
    "=": "==", "==": "==",
    "!=": "!=",
    "<": "<", "<=": "<=", ">": ">", ">=": ">=",
}

# =============== 유틸 ===============

def ensure_dirs():
    """저장 폴더 보장"""
    try:
        os.makedirs(SUBTITLE_DIR, exist_ok=True)
    except Exception as e:
        print(f"[경고] 폴더 생성 실패 {SUBTITLE_DIR}: {e}")


def log(msg: str, to_file: bool = True):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    if to_file:
        try:
            ensure_dirs()
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            print(f"[로그 실패] {e}")


def rotate_old_cookie():
    """기존 쿠키 백업 (새 쿠키 저장 성공 후에만 호출)"""
    if os.path.exists(COOKIE_PATH):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = os.path.join(DOWNLOAD_DIR, f"{ts}_cookies.txt")
        counter = 1
        base_backup = backup_name
        while os.path.exists(backup_name):
            backup_name = base_backup.replace(".txt", f"_{counter}.txt")
            counter += 1
        try:
            os.rename(COOKIE_PATH, backup_name)
            log(f"[END] 기존 cookie.txt 백업 → {backup_name}")
        except Exception as e:
            log(f"[경고] 쿠키 백업 실패: {e}")


def _try_load_browser_cookies():
    """여러 브라우저/도메인 조합으로 쿠키 로드 시도 (실패해도 중단 안 함)"""
    loaders = [
        ("chrome youtube.com", lambda: browser_cookie3.chrome(domain_name="youtube.com")),
        ("chrome .youtube.com", lambda: browser_cookie3.chrome(domain_name=".youtube.com")),
        ("chrome 전체", lambda: browser_cookie3.chrome()),
        ("edge youtube.com", lambda: browser_cookie3.edge(domain_name="youtube.com")),
        ("firefox youtube.com", lambda: browser_cookie3.firefox(domain_name="youtube.com")),
    ]
    for name, fn in loaders:
        try:
            cj = fn()
            count = len(list(cj))
            if count == 0:
                continue
            log(f"쿠키 로드 성공: {name} ({count}개)")
            return cj
        except Exception as e:
            log(f"쿠키 로드 실패 ({name}): {e}", to_file=False)
            continue
    return None


def export_cookies_from_chrome() -> bool:
    """임시 파일에 저장 성공 후에만 교체 (실패 시 기존 쿠키 유지)"""
    log("[START] 쿠키 추출")
    cj = _try_load_browser_cookies()
    if cj is None:
        log("[경고] 쿠키 추출 실패 → 기존 cookies.txt 유지")
        return False

    tmp_path = COOKIE_PATH + ".tmp"
    try:
        ensure_dirs()
        jar = cookielib.MozillaCookieJar(tmp_path)
        saved = 0
        for c in cj:
            try:
                jar.set_cookie(c)
                saved += 1
            except Exception:
                continue
        if saved == 0:
            log("[경고] 저장할 쿠키가 0개 → 기존 쿠키 유지")
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            return False
        jar.save(ignore_discard=True, ignore_expires=True)
        log(f"임시 쿠키 저장 성공: {saved}개 → {tmp_path}")
    except Exception as e:
        log(f"[경고] 쿠키 임시 저장 실패: {e} → 기존 쿠키 유지")
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        return False

    try:
        rotate_old_cookie()
        os.replace(tmp_path, COOKIE_PATH)
        log(f"[END] 새 쿠키 저장 → {COOKIE_PATH}")
        return True
    except Exception as e:
        log(f"[경고] 쿠키 교체 실패: {e}")
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        return False


# =============== 날짜/조건 필터 ===============

def parse_ymd(s: str) -> str:
    """'20240115' 또는 '2024-01-15' → 'YYYYMMDD' (8자리 문자열). 잘못되면 ValueError."""
    digits = re.sub(r"\D", "", s.strip())
    if len(digits) != 8:
        raise ValueError(f"날짜 형식 오류: {s} (예: 20240115 또는 2024-01-15)")
    datetime.strptime(digits, "%Y%m%d")  # 월/일 유효성 검사
    return digits


def compare_date(upload_date: str, op: str, target: str) -> bool:
    """'YYYYMMDD' 문자열 비교 (자릿수가 같아 문자열 비교 = 날짜 비교)."""
    if op == "==":
        return upload_date == target
    if op == "!=":
        return upload_date != target
    if op == "<":
        return upload_date < target
    if op == "<=":
        return upload_date <= target
    if op == ">":
        return upload_date > target
    if op == ">=":
        return upload_date >= target
    raise ValueError(f"알 수 없는 연산자: {op}")


class SubtitleFilter:
    """yt-dlp match_filter로 쓰는 조건 검사기.

    yt-dlp가 영상 정보를 가져온 뒤, 자막을 받기 직전에 이 검사를 호출합니다.
    조건에 맞으면 None(진행), 안 맞으면 사유 문자열(건너뛰기)을 돌려줍니다.
    덕분에 조건에 안 맞는 영상은 자막 요청 자체를 보내지 않아 요청량을 아낍니다.
    """

    def __init__(self, date_op=None, date_val=None, dur_min_sec=None, dur_max_sec=None):
        self.date_op = date_op
        self.date_val = date_val
        self.dur_min_sec = dur_min_sec
        self.dur_max_sec = dur_max_sec
        self.last_kept = None   # True=진행, False=스킵 (호출부가 결과 분류에 사용)
        self.last_reason = ""
        self.seen_title = None  # yt-dlp가 넘겨준 실제 영상 제목 (DB/헤더용)
        self.seen_id = None     # yt-dlp가 넘겨준 실제 영상 ID (헤더용)

    def _skip(self, title, reason):
        self.last_kept = False
        self.last_reason = reason
        log(f"  스킵: {title} ({reason})", to_file=False)
        return reason  # 문자열을 돌려주면 yt-dlp가 이 영상을 건너뜀

    def __call__(self, info, incomplete=False):
        if incomplete:
            return None
        title = info.get("title") or info.get("id") or "?"
        # 실제 제목/ID를 기억해 둔다 (별도 요청 없이 DB와 자막 헤더에 사용)
        self.seen_title = info.get("title") or info.get("id") or "?"
        self.seen_id = info.get("id")

        # 1) 업로드 날짜 조건 (예: >= 20240101)
        if self.date_op:
            ud = info.get("upload_date")  # 'YYYYMMDD' 형식
            if not ud:
                return self._skip(title, "업로드 날짜 없음(예정된 라이브 등)")
            if not compare_date(ud, self.date_op, self.date_val):
                return self._skip(title, f"날짜 {ud}가 조건({self.date_op} {self.date_val}) 불만족")

        # 2) 길이 조건 (초 단위)
        if self.dur_min_sec is not None or self.dur_max_sec is not None:
            d = info.get("duration")
            if d is None:
                return self._skip(title, "길이 정보 없음(라이브 등)")
            if self.dur_min_sec is not None and d < self.dur_min_sec:
                return self._skip(title, f"길이 {d // 60}분이 최소 {self.dur_min_sec // 60}분 미만")
            if self.dur_max_sec is not None and d > self.dur_max_sec:
                return self._skip(title, f"길이 {d // 60}분이 최대 {self.dur_max_sec // 60}분 초과")

        self.last_kept = True
        self.last_reason = ""
        return None  # None이면 자막 다운로드 진행


# =============== yt-dlp 래퍼 (자막 전용) ===============

def make_sub_ydl(outtmpl: str, langs, auto_subs: bool, sub_format: str, match_filter=None):
    """자막 전용 yt-dlp 옵션. skip_download=True라 영상 파일은 절대 받지 않음."""
    ydl_opts = {
        "skip_download": True,          # 영상 본체는 받지 않음 (핵심)
        "writesubtitles": True,         # 수동 자막 저장
        "writeautomaticsub": auto_subs,  # 자동생성 자막 저장 여부
        "subtitleslangs": langs,        # 예: ['ko', 'en'] 또는 ['all']
        "subtitlesformat": sub_format,  # 예: 'vtt/best', 'srt/vtt/best'
        "outtmpl": outtmpl,
        "nooverwrites": True,  # P1-1: 같은 파일이 이미 있으면 덮어쓰지 않고 건너뜀
        "retries": 10,
        "fragment_retries": 3,
        "concurrent_fragments": 1,
        "extractor_retries": 3,
        "socket_timeout": 30,
        "sleep_interval": 1.0,
        "max_sleep_interval": 4,
        "sleep_interval_requests": 1,
        "sleep_interval_subtitles": 1,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
        },
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
        "geo_bypass": True,
        "ignoreerrors": False,
        "quiet": False,
        "no_warnings": False,
        "noplaylist": False,  # 재생목록/채널 모드 기본값 (개별 모드에서 True로 덮어씀)
    }
    if os.path.exists(COOKIE_PATH):
        ydl_opts["cookiefile"] = COOKIE_PATH
    if match_filter is not None:
        ydl_opts["match_filter"] = match_filter
    return YoutubeDL(ydl_opts)


def fetch_flat_list(url: str):
    """목록만 빠르게 가져오기 (각 영상 상세정보는 조회하지 않음 → 요청 절약)."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "extract_flat": True,
        "skip_download": True,
        "extractor_retries": 3,
        "socket_timeout": 30,
        "sleep_interval": 1.0,
        "max_sleep_interval": 4,
        "sleep_interval_requests": 1,
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
        },
    }
    if os.path.exists(COOKIE_PATH):
        ydl_opts["cookiefile"] = COOKIE_PATH
    with YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)


def _extract_video_url(entry: dict):
    """flat/non-flat entry 모두에서 재생 가능한 URL 추출"""
    if not entry:
        return None
    url = entry.get("webpage_url") or entry.get("url")
    if url and isinstance(url, str) and url.startswith("http"):
        return url
    vid_id = entry.get("id")
    if vid_id and isinstance(vid_id, str):
        if len(vid_id) == 11 and "/" not in vid_id:
            return f"https://www.youtube.com/watch?v={vid_id}"
        if vid_id.startswith("http"):
            return vid_id
        if url and isinstance(url, str):
            if len(url) == 11:
                return f"https://www.youtube.com/watch?v={url}"
            return url
        return f"https://www.youtube.com/watch?v={vid_id}"
    return url


def normalize_channel_url(url: str) -> str:
    """채널 URL을 '동영상' 탭으로 정규화 (@핸들/channel/c/user 모두 지원)."""
    u = url.strip()
    base, _, query = u.partition("?")
    base = base.rstrip("/")
    if re.search(r"youtube\.com/(@|channel/|c/|user/)", base):
        # 이미 특정 탭(shorts/streams/playlists 등)이면 그대로 둠
        if not re.search(r"/(videos|shorts|streams|playlists|about|featured)$", base):
            base = base + "/videos"
    u = base + ("?" + query if query else "")
    return u


# =============== 자막 다운로드 코어 ===============

def build_sub_outtmpl(mode: str) -> str:
    """모드별 저장 경로 템플릿 ([id] 포함으로 파일명 충돌 방지)."""
    ensure_dirs()
    if mode == "playlist":
        template = "%(playlist_title)s/%(playlist_index)03d_%(title).150s [%(id)s].%(ext)s"
    elif mode == "channel":
        template = "%(uploader)s/%(title).150s [%(id)s].%(ext)s"
    else:  # single
        template = "%(title).150s [%(id)s].%(ext)s"
    return os.path.join(SUBTITLE_DIR, template)


def parse_video_id_from_url(url: str):
    """watch?v= / youtu.be / shorts / embed / live 주소에서 11자 영상 ID 추출."""
    if not url or not isinstance(url, str):
        return None
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else None


def find_subtitle_files(video_id: str):
    """SUBTITLE_DIR 아래에서 해당 영상 ID가 들어간 자막 파일들 찾기."""
    found = []
    if not video_id:
        return found
    base = Path(SUBTITLE_DIR)
    if not base.exists():
        return found
    for ext in SUBTITLE_EXTS:
        found.extend(base.rglob(f"*{video_id}*.{ext}"))
    return sorted(set(found))


def add_header_to_subtitles(video_id, title, url, finished_at: str):
    """다운로드된 자막 파일들 상단에 완료시각·제목·URL 기입.

    vtt는 스펙상 허용된 NOTE 주석으로 (WEBVTT 바로 다음 줄),
    그 외 형식(srt 등)은 '#' 주석 줄로 앞에 붙입니다.
    이미 헤더가 있는 파일(재시도 등)은 건너뜁니다.
    """
    files = find_subtitle_files(video_id)
    if not files:
        log(f"[경고] 헤더 기입: 자막 파일을 찾지 못함 (id={video_id})")
        return []
    stamped = []
    for fp in files:
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            log(f"[경고] 헤더 기입 실패(읽기): {fp.name} / {e}")
            continue
        if "Downloaded-At:" in text[:500]:
            continue  # 이미 기입됨
        lines = text.splitlines()
        if fp.suffix.lower() == ".vtt" and lines and lines[0].startswith("WEBVTT"):
            header = ["NOTE Downloaded-At: " + finished_at,
                      "NOTE Video-Title: " + (title or "?"),
                      "NOTE Video-URL: " + (url or "?"),
                      ""]
            new_text = "\n".join([lines[0]] + header + lines[1:]) + "\n"
        else:
            header = ["# Downloaded-At: " + finished_at,
                      "# Video-Title: " + (title or "?"),
                      "# Video-URL: " + (url or "?"),
                      ""]
            new_text = "\n".join(header + lines) + "\n"
        try:
            fp.write_text(new_text, encoding="utf-8")
            stamped.append(str(fp))
        except Exception as e:
            log(f"[경고] 헤더 기입 실패(쓰기): {fp.name} / {e}")
    if stamped:
        log(f"자막 헤더 기입: {len(stamped)}개 파일")
    return stamped


def _db_finish(db_conn, job_id, status, reason="", title=None, subtitle_path=""):
    """DB 종료 기록 (DB 없거나 기록 실패해도 전체 흐름은 계속)."""
    if db_conn is None or job_id is None:
        return
    try:
        db_record_finish(db_conn, job_id, status, reason, title, subtitle_path)
    except Exception as e:
        log(f"[경고] DB 종료 기록 실패: {e}")


def _db_fail_once(db_conn, url, title, mode, langs, auto_subs, sub_format, reason):
    """추출 이전 단계(목록 조회 실패 등)의 실패를 DB에 1행으로 기록."""
    if db_conn is None:
        return
    try:
        job_id = db_record_start(db_conn, url, title, mode, langs, auto_subs, sub_format)
        db_record_finish(db_conn, job_id, "failed", reason, title, "")
    except Exception as e:
        log(f"[경고] DB 실패 기록 실패: {e}")


def detect_title_lang(title):
    """영상 제목의 주 언어 판별: 한글(가-힣) vs 영문 개수 비교.
    'ko' / 'en' / None(판별 불가)을 돌려줍니다. 동점이면 ko 우선.
    """
    if not title or not isinstance(title, str):
        return None
    ko = len(re.findall(r"[가-힣]", title))
    en = len(re.findall(r"[A-Za-z]", title))
    if ko == 0 and en == 0:
        return None
    return "ko" if ko >= en else "en"


def _build_fallback_plan(langs, auto_subs):
    """1순위 후보 (사용자 요청 설정). 나머지는 제목을 안 뒤에 동적으로 만듦."""
    lang_list = list(langs) if isinstance(langs, (list, tuple)) else [langs]
    return [(lang_list, auto_subs,
             f"요청 설정({','.join(lang_list)}{',자동' if auto_subs else ''})")]


def _build_title_rest(tried, req_langs, title):
    """1순위에서 자막이 0개일 때, 영상 제목 언어로 나머지 후보를 만듦.
    tried: 이미 물어본 (언어튜플, 자동여부) 집합 (중복 방지).
    순서: 제목언어 자동 → 요청언어 자동 → 제목언어 수동 → 영어 자동.
    """
    req_list = list(req_langs) if isinstance(req_langs, (list, tuple)) else [req_langs]
    tlang = detect_title_lang(title)
    rest = []

    def consider(lang_list, auto, label):
        key = (tuple(lang_list), auto)
        if key not in tried:
            tried.add(key)
            rest.append((list(lang_list), auto, label))

    if tlang:
        consider([tlang], True, f"제목언어 자동({tlang}+자동)")
        consider(req_list, True, "요청언어 자동")
        consider([tlang], False, f"제목언어 수동({tlang})")
    elif "all" not in req_list:
        consider(req_list, True, "자동자막 폴백")
    for fb in FALLBACK_LANGS:
        consider([fb], True, f"언어 폴백({fb}+자동)")
    return rest


def _download_round(url, outtmpl, sub_filter, langs, auto_subs, sub_format, noplaylist, max_attempts):
    """한 가지 (언어, 자동자막) 조합으로 최대 max_attempts회 시도.

    반환: (kept, saw_429, fatal, fatal_msg)
      kept=True  → 다운로드 정상 종료 (자막 파일 유무는 호출부가 확인)
      kept=False → 조건 스킵 (날짜·길이 불만족)
      fatal='gone'  → 없는 영상 (즉시 전체 종료해야 함)
      fatal='error' → 재시도 소진 실패
    """
    saw_429 = False
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            sub_filter.last_kept = None
            sub_filter.last_reason = ""
            sub_filter.seen_title = None
            sub_filter.seen_id = None
            ydl = make_sub_ydl(outtmpl, langs, auto_subs, sub_format, match_filter=sub_filter)
            if noplaylist:
                ydl.params["noplaylist"] = True
            with ydl:
                ydl.download([url])
            return (sub_filter.last_kept is not False), saw_429, None, ""
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            if any(p in msg for p in UNAVAILABLE_PATTERNS):
                return True, False, "gone", str(e)
            if "429" in msg or "too many requests" in msg:
                saw_429 = True
                wait = SLEEP_ON_429_BASE + random.uniform(10, 30)
                log(f"[경고] 429 감지 (시도 {attempt}/{max_attempts}): {e} → {wait:.0f}초 대기")
                time.sleep(wait)
                export_cookies_from_chrome()
            elif "403" in msg or "forbidden" in msg:
                log(f"[경고] 403 (시도 {attempt}/{max_attempts}): 쿠키 갱신 후 재시도")
                export_cookies_from_chrome()
                time.sleep(random.uniform(5, 10))
            else:
                log(f"[경고] 자막 실패 (시도 {attempt}/{max_attempts}): {e}")
                if attempt < max_attempts:
                    backoff = random.uniform(3, 6) * attempt
                    log(f"  → {backoff:.1f}초 후 재시도")
                    time.sleep(backoff)
    return True, saw_429, "error", str(last_err)


def download_subs_for_video(url: str, mode: str, sub_filter: SubtitleFilter,
                            langs, auto_subs: bool, sub_format: str, noplaylist: bool = False,
                            title_hint=None, db_conn=None, max_attempts: int = 3):
    """영상 1개의 자막 다운로드.

    DB에 시도 시작/종료(시각·제목·URL·결과)를 기록하고, 성공 시 자막 파일
    상단에 완료시각·제목·URL 헤더를 기입합니다.
    반환: (result, saw_429) - result는 downloaded / skipped / failed / cached / no_subtitle.
    cached는 DB에 같은 언어 완료 기록이 있고 파일도 그대로 있을 때
    (유튜브에 요청을 1번도 보내지 않음).
    no_subtitle은 요청 언어→자동자막→영어 폴백까지 다 물어봤는데 자막이 없을 때.
    """
    outtmpl = build_sub_outtmpl(mode if mode in ("playlist", "channel", "single") else "single")
    started_title = title_hint or url
    langs_str = ",".join(langs) if isinstance(langs, (list, tuple)) else str(langs)

    # P1-1: 이미 받은 자막이면 요청 자체를 생략 (DB 완료 기록 + 파일 존재 확인)
    if db_conn is not None:
        try:
            prev = db_last_success(db_conn, url, langs_str)
        except Exception as e:
            log(f"[경고] DB 완료기록 조회 실패: {e}")
            prev = None
        if prev:
            paths = [p for p in (prev.get("subtitle_path") or "").splitlines() if p.strip()]
            if paths and all(os.path.exists(p) for p in paths):
                real_title = prev.get("title") or started_title
                log(f"[SKIP] 이미 받음 ({prev.get('finished_at')}) → 재요청 안 함: {real_title}")
                CACHED_LIST.append(real_title)
                return "cached", False
            log(f"[안내] 완료 기록은 있으나 파일이 없음 → 다시 받음: {started_title}")

    job_id = None
    if db_conn is not None:
        try:
            job_id = db_record_start(db_conn, url, started_title, mode, langs, auto_subs, sub_format)
        except Exception as e:
            log(f"[경고] DB 시작 기록 실패: {e}")
            job_id = None

    saw_429 = False
    # 1순위는 요청 설정. 제목을 안 뒤에야 나머지 후보를 정할 수 있어 큐 방식 사용.
    req_list = list(langs) if isinstance(langs, (list, tuple)) else [langs]
    pending = _build_fallback_plan(langs, auto_subs)
    tried = {(tuple(l), a) for l, a, _ in pending}
    tried_labels = [label for _, _, label in pending]
    round_no = 0
    while pending:
        round_langs, round_auto, round_label = pending.pop(0)
        round_no += 1
        if round_no > 1:
            log(f"[폴백 {round_no}] {round_label}")
        kept, round_429, fatal, fatal_msg = _download_round(
            url, outtmpl, sub_filter, round_langs, round_auto, sub_format,
            noplaylist, max_attempts)
        saw_429 = saw_429 or round_429
        real_title = sub_filter.seen_title or started_title
        if fatal == "gone":
            # 없는 영상은 재시도해도 살아나지 않음 → 1회로 종료, DB·실패목록에 사유 기록
            reason = f"영상 없음(삭제/비공개/찾을 수 없음): {fatal_msg[:300]}"
            log(f"[영상 없음] 재시도 없이 실패 처리: {real_title}")
            FAIL_LIST.append({"title": real_title, "url": url})
            _db_finish(db_conn, job_id, "failed", reason, real_title, "")
            return "failed", False
        if fatal == "error":
            log(f"[END] 자막 실패: {real_title} / {fatal_msg}")
            FAIL_LIST.append({"title": real_title, "url": url})
            _db_finish(db_conn, job_id, "failed", str(fatal_msg)[:500], real_title, "")
            return "failed", saw_429
        if not kept:
            SKIPPED_LIST.append({"title": real_title, "reason": sub_filter.last_reason})
            _db_finish(db_conn, job_id, "skipped", sub_filter.last_reason, real_title, "")
            return "skipped", saw_429
        video_id = sub_filter.seen_id or parse_video_id_from_url(url)
        found = find_subtitle_files(video_id)
        if found:
            finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            add_header_to_subtitles(video_id, real_title, url, finished_at)
            note = "" if round_no == 1 else f" ({round_label}로 저장)"
            log(f"[END] 자막 저장: {real_title}{note} ({len(found)}개 파일)")
            SUCCESS_LIST.append(real_title)
            all_paths = [str(p) for p in found]
            _db_finish(db_conn, job_id, "success", note.strip(), real_title, "\n".join(all_paths))
            return "downloaded", saw_429
        log(f"[폴백] '{round_label}'에서 자막 0개 → 다음 후보 시도", to_file=False)
        if not pending:
            # 1순위가 비었으니 이제 제목 언어를 알 수 있음 → 나머지 후보 생성
            for cand in _build_title_rest(tried, req_list, real_title):
                pending.append(cand)
                tried_labels.append(cand[2])
    # 모든 후보 소진 → 자막 없음 확정 (실패가 아니라 별도 분류, 재시도 목록 제외)
    real_title = sub_filter.seen_title or started_title
    tried_str = " → ".join(tried_labels)
    reason = f"자막 없음(시도: {tried_str})"
    log(f"[자막 없음] {real_title} ({tried_str})")
    NO_SUB_LIST.append({"title": real_title, "url": url, "reason": reason})
    _db_finish(db_conn, job_id, "no_subtitle", reason, real_title, "")
    return "no_subtitle", saw_429


def _sleep_between_videos():
    jitter = random.uniform(SLEEP_BETWEEN_VIDEOS_MIN, SLEEP_BETWEEN_VIDEOS_MAX)
    log(f"영상 간 대기 {jitter:.1f}초 (차단 방지)")
    time.sleep(jitter)


def process_entries(entries, start_idx, end_idx, mode: str, label: str,
                    date_op=None, date_val=None, dur_min_sec=None, dur_max_sec=None,
                    langs=None, auto_subs=True, sub_format="vtt/best", db_conn=None):
    """목록(채널/재생목록) 공통 처리: 번호 범위 자르기 → 1개씩 자막 다운로드."""
    entries = [e for e in entries if e]
    total = len(entries)
    if total == 0:
        log("[경고] 목록이 비어있습니다 (전부 비공개/삭제 가능)")
        return

    s = start_idx if start_idx is not None else 1
    e = end_idx if end_idx is not None else total
    s = max(1, s)
    e = min(total, e)
    if s > e:
        log(f"[경고] 범위 오류 s={s} > e={e} → 전체 처리로 폴백")
        s, e = 1, total
    log(f"[START] {label}: 총 {total}개 중 {s} ~ {e} 처리")

    done = 0
    consec_429 = 0
    for idx, entry in enumerate(entries, start=1):
        if idx < s or idx > e:
            continue
        vid_url = _extract_video_url(entry)
        vid_title = (entry.get("title") if isinstance(entry, dict) else None) or f"item_{idx}"
        if not vid_url:
            log(f"[경고] URL 추출 실패 idx={idx}")
            FAIL_LIST.append({"title": vid_title, "url": str(entry)})
            _db_fail_once(db_conn, str(entry), vid_title, mode, langs, auto_subs, sub_format, "URL 추출 실패")
            continue
        done += 1
        log(f"[START] {label} {idx}/{total}: {vid_title}")
        sub_filter = SubtitleFilter(date_op, date_val, dur_min_sec, dur_max_sec)
        result, saw_429 = download_subs_for_video(
            vid_url, mode, sub_filter, langs, auto_subs, sub_format,
            title_hint=vid_title, db_conn=db_conn)
        log(f"[END] {label} {idx}/{total}: {vid_title} → {result}")
        # 차단기: 429가 연속되면 IP가 달아오른 상태 → 나머지 중단 (DB에 남아 나중에 재개)
        consec_429 = consec_429 + 1 if saw_429 else 0
        if consec_429 >= MAX_CONSECUTIVE_429:
            log(f"[차단기] 429가 {MAX_CONSECUTIVE_429}회 연속 → 나머지 중단 (실패 목록은 DB에 보관, 모드 4로 재개 가능)")
            break
        if done % COOKIE_REFRESH_EVERY_N_VIDEOS == 0:
            log(f"[주기] {COOKIE_REFRESH_EVERY_N_VIDEOS}개마다 쿠키 갱신")
            export_cookies_from_chrome()
        _sleep_between_videos()
    log(f"[END] {label} 처리 완료")


def _to_entries_list(info):
    """info['entries']가 list든 generator든 안전하게 리스트로."""
    entries = info.get("entries")
    if entries is None:
        return None
    try:
        if not isinstance(entries, list):
            entries = list(entries)
    except Exception as ex:
        log(f"[경고] entries 변환 실패: {ex}")
        return []
    return entries


# =============== 모드별 실행 ===============

def run_single(url: str, date_op=None, date_val=None, dur_min_sec=None, dur_max_sec=None,
               langs=None, auto_subs=True, sub_format="vtt/best", db_conn=None):
    """모드 2: 개별 영상 1개의 자막."""
    export_cookies_from_chrome()
    log(f"[START] 개별 영상 자막: {url}")
    sub_filter = SubtitleFilter(date_op, date_val, dur_min_sec, dur_max_sec)
    # URL에 &list= 가 섞여 있어도 영상 1개만 처리 (재생목록 전체 받는 사고 방지)
    download_subs_for_video(url, "single", sub_filter, langs, auto_subs, sub_format,
                            noplaylist=True, db_conn=db_conn)
    _sleep_between_videos()


def run_playlist(url: str, start_idx=None, end_idx=None, date_op=None, date_val=None,
                 dur_min_sec=None, dur_max_sec=None, langs=None, auto_subs=True, sub_format="vtt/best",
                 db_conn=None):
    """모드 3: 재생목록 내 영상 자막 (전체 or 조건)."""
    export_cookies_from_chrome()
    info = fetch_flat_list(url)
    if info is None:
        log(f"[END] 정보를 가져오지 못함: {url}")
        FAIL_LIST.append({"title": url, "url": url})
        _db_fail_once(db_conn, url, url, "playlist", langs, auto_subs, sub_format, "목록 조회 실패")
        return
    entries = _to_entries_list(info)
    if entries is None:  # 재생목록이 아니라 단일 영상이면 그대로 처리
        log("[안내] 재생목록이 아니라 단일 영상으로 처리합니다")
        run_single(url, date_op, date_val, dur_min_sec, dur_max_sec, langs, auto_subs, sub_format, db_conn)
        return
    title = info.get("title") or "재생목록"
    process_entries(entries, start_idx, end_idx, "playlist", f"재생목록({title})",
                    date_op, date_val, dur_min_sec, dur_max_sec, langs, auto_subs, sub_format, db_conn)


def run_channel(url: str, start_idx=None, end_idx=None, date_op=None, date_val=None,
                dur_min_sec=None, dur_max_sec=None, langs=None, auto_subs=True, sub_format="vtt/best",
                db_conn=None):
    """모드 1: 채널 전체 영상 자막 (전체 or 날짜/길이/번호 조건)."""
    url = normalize_channel_url(url)
    log(f"채널 URL 정규화 → {url}")
    export_cookies_from_chrome()
    log("[안내] 채널 목록을 가져옵니다 (영상 많은 채널은 시간이 걸릴 수 있음)")
    info = fetch_flat_list(url)
    if info is None:
        log(f"[END] 정보를 가져오지 못함: {url}")
        FAIL_LIST.append({"title": url, "url": url})
        _db_fail_once(db_conn, url, url, "channel", langs, auto_subs, sub_format, "채널 목록 조회 실패")
        return
    entries = _to_entries_list(info)
    if entries is None:
        log("[경고] 채널 목록이 아니라 단일 항목입니다. 개별 모드로 처리합니다")
        run_single(url, date_op, date_val, dur_min_sec, dur_max_sec, langs, auto_subs, sub_format, db_conn)
        return
    channel = info.get("channel") or info.get("uploader") or "채널"
    process_entries(entries, start_idx, end_idx, "channel", f"채널({channel})",
                    date_op, date_val, dur_min_sec, dur_max_sec, langs, auto_subs, sub_format, db_conn)


# =============== 실패 재시도 / 예약 (모드 4) ===============

def parse_selection(sel: str, total: int):
    """'all' / '1,3' / '2-5' / '1,3-5' → 1부터 시작 번호 리스트 (Enter=전체)."""
    s = sel.strip().lower()
    if s in ("", "all", "전체", "전부"):
        return list(range(1, total + 1))
    picked = set()
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            a, b = int(a.strip()), int(b.strip())
            if a > b:
                a, b = b, a
            picked.update(range(max(1, a), min(total, b) + 1))
        else:
            n = int(part)
            if 1 <= n <= total:
                picked.add(n)
    return sorted(picked)


def ask_schedule():
    """지금 / N분 후 / HH:MM 중 선택 → 목표 시각(datetime) 또는 None(지금)."""
    ans = input("언제 재시도? (Enter=지금 / 예: 30분후 / 21:30): ").strip()
    if not ans:
        return None
    m = re.match(r"^(\d+)\s*분\s*후?$", ans)
    if m:
        return datetime.now() + timedelta(minutes=int(m.group(1)))
    m = re.match(r"^(\d{1,2}):(\d{2})$", ans)
    if m:
        target = datetime.now().replace(hour=int(m.group(1)), minute=int(m.group(2)),
                                        second=0, microsecond=0)
        if target <= datetime.now():
            target += timedelta(days=1)  # 이미 지난 시각이면 내일
        return target
    log("예약 형식 오류 → 지금 실행합니다 (예: 30분후 / 21:30)")
    return None


def wait_until(target: datetime) -> bool:
    """예약 시각까지 10초씩 끊어 대기. Ctrl+C면 False(취소)를 돌려줌."""
    while True:
        remain = (target - datetime.now()).total_seconds()
        if remain <= 0:
            print()
            return True
        m, s = divmod(int(remain), 60)
        print(f"\r예약 대기 중... {m}분 {s}초 남음 (취소: Ctrl+C)   ", end="", flush=True)
        try:
            time.sleep(min(10, remain))
        except KeyboardInterrupt:
            print()
            log("[취소] 예약 대기 취소")
            return False


def recommend_retry(reason) -> tuple:
    """실패 사유별 재시도 가이드: (행동, 안내문) 반환.

    skip   - 없는 영상: 다시 해도 안 됨, 제외 권장
    wait   - 429: IP 식힘 필요, 30분 후 예약 권장
    cookie - 403: 쿠키 갱신 권장
    now    - 그 외: 즉시 재시도 가능
    """
    r = str(reason or "")
    if r.startswith("영상 없음"):
        return ("skip", "재시도 무의미(없는 영상) - 제외 권장")
    low = r.lower()
    if "429" in low or "too many" in low:
        return ("wait", "30분 후 예약 권장 (IP 식힘 필요)")
    if "403" in low or "forbidden" in low:
        return ("cookie", "쿠키 갱신 권장 (브라우저를 닫고 실행하면 자동 갱신됨)")
    return ("now", "즉시 재시도 가능")


def _execute_retry_targets(db_conn, targets):
    """재시도 실행 본체 (대화형/무질문 공용).

    쿠키 1회 갱신 → 건당 재시도(8~15초 간격) → 429 3연속 차단기.
    """
    export_cookies_from_chrome()
    log(f"[START] 실패 재시도: {len(targets)}개")
    consec_429 = 0
    for n, row in enumerate(targets, 1):
        url = row["url"]
        langs = [p.strip() for p in (row["langs"] or "ko,en").split(",") if p.strip()]
        auto_subs = bool(row["auto_subs"])
        sub_format = row["sub_format"] or "vtt/best"
        sub_mode = row["mode"] if row["mode"] in ("playlist", "channel", "single") else "single"
        log(f"[START] 재시도 {n}/{len(targets)} (통산 {row['attempt_no'] + 1}회차): {row['title']}")
        sub_filter = SubtitleFilter()  # 조건 없음
        try:
            result, saw_429 = download_subs_for_video(
                url, sub_mode, sub_filter, langs, auto_subs, sub_format,
                title_hint=row["title"], db_conn=db_conn)
        except KeyboardInterrupt:
            log("[중단] 재시도 중단 (남은 항목은 DB에 보관됨)")
            break
        log(f"[END] 재시도 {n}/{len(targets)}: {result}")
        consec_429 = consec_429 + 1 if saw_429 else 0
        if consec_429 >= MAX_CONSECUTIVE_429:
            log(f"[차단기] 429가 {MAX_CONSECUTIVE_429}회 연속 → 나머지 중단 (모드 4로 나중에 재개 가능)")
            break
        if n < len(targets):
            jitter = random.uniform(SLEEP_RETRY_MIN, SLEEP_RETRY_MAX)
            log(f"재시도 간 대기 {jitter:.1f}초 (안전)")
            try:
                time.sleep(jitter)
            except KeyboardInterrupt:
                log("[중단] 재시도 중단 (남은 항목은 DB에 보관됨)")
                break
    log("[END] 실패 재시도 완료")


# =============== 예약 영속성: Windows 작업 스케줄러 (P2-3) ===============
# 앱 안의 예약(wait_until)은 프로세스를 켜 둬야 합니다. 진짜 예약(PC만 켜두면 됨)은
# Windows 작업 스케줄러(schtasks)에 "--retry-failed" 무질문 실행을 등록합니다.

SCHED_PREFIX = "YTSubsRetry_"


def build_retry_command(selection):
    """스케줄러가 실행할 명령줄 문자열 (경로 공백 대비 따옴표 처리)."""
    cmd = f'"{sys.executable}" "{os.path.abspath(__file__)}" --retry-failed'
    s = (selection or "").strip()
    if s and s.lower() not in ("all", "전체", "전부"):
        cmd += f' --retry-select "{s}"'
    cmd += " --headless"
    return cmd


def build_schtasks_args(task_name, target, selection):
    """schtasks /create 인자 목록. 당일이면 /sd 생략(로케일 날짜 문제 회피)."""
    args = ["schtasks", "/create", "/tn", task_name,
            "/tr", build_retry_command(selection),
            "/sc", "once", "/st", target.strftime("%H:%M"), "/f"]
    if target.date() != datetime.now().date():
        args += ["/sd", target.strftime("%m/%d/%Y")]
    return args


def register_retry_task(target, selection):
    """예약을 작업 스케줄러에 등록. 반환: (성공여부, 안내문)."""
    task_name = SCHED_PREFIX + target.strftime("%Y%m%d_%H%M")
    try:
        proc = subprocess.run(build_schtasks_args(task_name, target, selection),
                              capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return False, "schtasks를 찾을 수 없음 (Windows 전용 기능)"
    except Exception as e:
        return False, f"스케줄러 등록 실패: {e}"
    if proc.returncode == 0:
        return True, (f"스케줄러 등록 완료: {task_name} "
                      f"({target.strftime('%Y-%m-%d %H:%M')} 실행, 이 창을 닫아도 됨)")
    out = (proc.stderr or proc.stdout or "").strip()[:300]
    return False, f"스케줄러 등록 실패: {out}"


def list_retry_tasks():
    """등록된 자막 재시도 예약 이름 목록 (실패 시 빈 목록)."""
    try:
        proc = subprocess.run(["schtasks", "/query", "/fo", "CSV", "/nh"],
                              capture_output=True, text=True, timeout=30)
    except Exception as e:
        log(f"[경고] 예약 조회 실패: {e}")
        return []
    if proc.returncode != 0:
        return []
    tasks = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        name = line.split('","')[0].strip().strip('"').lstrip("\\")
        if name.startswith(SCHED_PREFIX):
            tasks.append(name)
    return tasks


def delete_retry_task(task_name):
    """예약 1건 삭제. 반환: (성공여부, 안내문)."""
    try:
        proc = subprocess.run(["schtasks", "/delete", "/tn", task_name, "/f"],
                              capture_output=True, text=True, timeout=30)
    except Exception as e:
        return False, f"예약 삭제 실패: {e}"
    if proc.returncode == 0:
        return True, f"예약 삭제 완료: {task_name}"
    out = (proc.stderr or proc.stdout or "").strip()[:200]
    return False, f"예약 삭제 실패: {out}"


def run_schedule_manager():
    """모드 5: 등록된 예약 조회/삭제."""
    tasks = list_retry_tasks()
    if not tasks:
        log("[안내] 등록된 자막 재시도 예약이 없습니다")
        return
    print("\n======= 등록된 자막 재시도 예약 =======")
    for i, t in enumerate(tasks, 1):
        print(f"{i}. {t}")
    ans = input("삭제할 번호 (Enter=취소): ").strip()
    if not ans:
        return
    try:
        n = int(ans)
        target = tasks[n - 1]
    except (ValueError, IndexError):
        log("번호 오류 → 취소")
        return
    ok, msg = delete_retry_task(target)
    log(msg)


def ask_register_scheduler(target, selection):
    """앱 안 대기 대신 작업 스케줄러 등록을 제안. 등록 성공 시 True."""
    ans = input("작업 스케줄러에 등록할까요? (등록하면 이 창을 닫아도 됨) (y/N): ").strip().lower()
    if ans != "y":
        return False
    ok, msg = register_retry_task(target, selection)
    log(msg)
    if not ok:
        log("[안내] 등록 실패 → 앱 안에서 대기합니다. 명령을 직접 실행해도 됩니다:")
        log("  " + build_retry_command(selection))
    return ok


def run_retry(db_conn):
    """모드 4: DB 실패 목록 → 번호 선택 → 즉시/예약 재시도.

    안전 장치: 재시도 간 8~15초 대기, 영상당 최대 3회 시도,
    429가 3회 연속이면 나머지 중단(차단기). 중단된 항목은 DB에 그대로 남아
    다음 실행에서 이어서 재시도할 수 있습니다.
    재시도는 조건 필터 없이 실행합니다 (실패는 조건 문제가 아니므로).
    """
    failed = db_latest_failed(db_conn)
    if not failed:
        log("[안내] 재시도할 실패 기록이 없습니다")
        return
    print("\n======= 실패 목록 (URL별 가장 최근 실패) =======")
    marks = {"skip": "[제외권장]", "wait": "[예약권장]", "cookie": "[쿠키권장]", "now": "[재시도가능]"}
    recs = [recommend_retry(row["reason"]) for row in failed]
    for i, (row, (act, msg)) in enumerate(zip(failed, recs), 1):
        print(f"{i}. {marks[act]} {row['title'] or '(제목없음)'} | {row['url']}")
        print(f"   실패시각: {row['finished_at']} | 누적시도 {row['attempt_no']}회 | {str(row['reason'])[:80]}")
        print(f"   → 가이드: {msg}")
    n_skip = sum(1 for a, _ in recs if a == "skip")
    n_wait = sum(1 for a, _ in recs if a == "wait")
    if n_skip or n_wait:
        print(f"   요약: 제외권장 {n_skip}개, 예약권장 {n_wait}개")
    try:
        sel_raw = input("재시도할 번호 (예: all / 1,3 / 2-5, Enter=전체): ")
        picked = parse_selection(sel_raw, len(failed))
    except ValueError:
        log("선택 파싱 실패 → 취소 (숫자와 , - 만 사용)")
        return
    if not picked:
        log("선택 없음 → 취소")
        return
    targets = [failed[i - 1] for i in picked]

    # P2-2: 없는 영상은 골라도 소용없으니 제외 제안
    gone = [r for r in targets if recommend_retry(r["reason"])[0] == "skip"]
    if gone:
        ans = input(f"재시도해도 안 되는 영상 {len(gone)}개를 제외할까요? (Y/n, Enter=Y): ").strip().lower()
        if ans != "n":
            targets = [r for r in targets if recommend_retry(r["reason"])[0] != "skip"]
            log(f"{len(gone)}개 제외 → {len(targets)}개 재시도")
            if not targets:
                log("남은 항목 없음 → 취소")
                return

    # P2-2: 429가 섞여 있으면 30분 후 예약 제안
    target_time = None
    if any(recommend_retry(r["reason"])[0] == "wait" for r in targets):
        ans = input("429 실패가 있어 30분 후 예약을 권장합니다. 예약할까요? (y/N, Enter=지금실행): ").strip().lower()
        if ans == "y":
            target_time = datetime.now() + timedelta(minutes=30)
    if target_time is None:
        target_time = ask_schedule()
    if target_time is not None:
        log(f"예약: {target_time.strftime('%Y-%m-%d %H:%M')}에 재시도 시작")
        # P2-3: 스케줄러에 맡기면 이 창을 닫아도 됨. 등록 성공 시 앱 안 대기는 생략.
        if ask_register_scheduler(target_time, sel_raw):
            return
        if not wait_until(target_time):
            return  # 예약 취소

    _execute_retry_targets(db_conn, targets)


# =============== 종료 정리 ===============

def finalize():
    if FAIL_LIST:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        ensure_dirs()
        fail_path = os.path.join(DOWNLOAD_DIR, f"SUB_FAIL_LIST_{ts}.txt")
        with open(fail_path, "w", encoding="utf-8") as f:
            for item in FAIL_LIST:
                f.write(f"title={item['title']} | url={item['url']}\n")
        log(f"[END] 실패 리스트 저장 → {fail_path}")
    log(f"작업 기록 DB → {DB_PATH} (실패 목록은 모드 4에서 재시도 가능)")

    print("\n======= 자막 성공 =======")
    print("(없음)" if not SUCCESS_LIST else "\n".join(f"+ {t}" for t in SUCCESS_LIST))
    print("\n======= 이미 받아둠 (재요청 안 함) =======")
    print("(없음)" if not CACHED_LIST else "\n".join(f"= {t}" for t in CACHED_LIST))
    print("\n======= 자막 없음 (모든 언어 후보 소진) =======")
    if NO_SUB_LIST:
        for item in NO_SUB_LIST:
            print(f"- {item['title']} | {item['url']} ({item['reason']})")
    else:
        print("(없음)")
    print("\n======= 조건 불만족으로 스킵 =======")
    if SKIPPED_LIST:
        for item in SKIPPED_LIST:
            print(f"- {item['title']} ({item['reason']})")
    else:
        print("(없음)")
    print("\n======= 자막 실패 =======")
    if FAIL_LIST:
        for item in FAIL_LIST:
            print(f"- {item['title']} | {item['url']}")
    else:
        print("(없음)")

    try:
        import winsound
        winsound.MessageBeep()
    except Exception:
        pass
    if HEADLESS:
        return  # 스케줄러 실행 중 팝업 금지 (멈춤 방지)
    try:
        from ctypes import windll
        windll.user32.MessageBoxW(0, "자막 다운로드 작업이 종료되었습니다.", "subtitle downloader", 0)
    except Exception:
        pass


# =============== 입력 도우미 ===============

LAST_DIR_FILE = os.path.join(DOWNLOAD_DIR, ".last_subtitle_dir")


def _load_last_save_dir():
    """지난번에 쓴 저장 폴더 (없으면 None)."""
    try:
        with open(LAST_DIR_FILE, encoding="utf-8") as f:
            p = f.read().strip()
            return p or None
    except Exception:
        return None


def ask_save_location():
    """자막 저장 폴더 묻기. Enter=지난번(없으면 기본값). 없으면 만들고, 실패하면 기본값."""
    default = _load_last_save_dir() or SUBTITLE_DIR
    ans = input(f"자막 저장 폴더 (Enter={default}): ").strip().strip('"').strip("'")
    chosen = os.path.abspath(os.path.expanduser(ans)) if ans else default
    try:
        os.makedirs(chosen, exist_ok=True)
    except Exception as e:
        log(f"[경고] 폴더 생성 실패 → 기본값 사용: {e}")
        chosen = SUBTITLE_DIR
        os.makedirs(chosen, exist_ok=True)
    try:
        with open(LAST_DIR_FILE, "w", encoding="utf-8") as f:
            f.write(chosen)
    except Exception:
        pass
    if os.path.normcase(chosen) != os.path.normcase(SUBTITLE_DIR):
        log(f"저장 위치: {chosen}")
    return chosen


def ask_lang_config():
    """자막 언어/자동자막/형식 묻기."""
    langs_raw = input("자막 언어 (쉼표 구분, 예: ko,en / 전체는 all, Enter=ko,en): ").strip()
    if not langs_raw:
        langs = ["ko", "en"]
    elif langs_raw.lower() == "all":
        langs = ["all"]
    else:
        langs = [p.strip() for p in langs_raw.split(",") if p.strip()] or ["ko", "en"]
    auto_raw = input("자동생성 자막도 포함? (Y/n, Enter=Y): ").strip().lower()
    auto_subs = auto_raw != "n"
    fmt_raw = input("형식 (vtt/srt, Enter=vtt): ").strip().lower()
    if fmt_raw == "srt":
        sub_format = "srt/vtt/best"
    else:
        sub_format = "vtt/best"
    log(f"자막 설정: 언어={langs}, 자동자막={'포함' if auto_subs else '제외'}, 형식={sub_format}")
    return langs, auto_subs, sub_format


def ask_optional_filters(with_range=True):
    """날짜/길이/번호 조건 묻기 (전부 Enter면 조건 없음)."""
    date_op = date_val = None
    ans = input("업로드 날짜 조건을 쓸까요? (예: >= 2024-01-01, 아니면 Enter): ").strip()
    if ans:
        try:
            m = re.match(r"\s*(==|=|!=|<=|>=|<|>)\s*(.+)\s*$", ans)
            if not m:
                raise ValueError("연산자(==,!=,<,<=,>,>=)로 시작해야 합니다")
            date_op = DATE_OPS[m.group(1)]
            date_val = parse_ymd(m.group(2))
            log(f"날짜 조건: upload_date {date_op} {date_val}")
        except ValueError as ve:
            log(f"날짜 조건 파싱 실패 → 날짜 조건 없이 진행 ({ve})")
            date_op = date_val = None

    dur_min_sec = dur_max_sec = None
    ans = input("길이 조건을 쓸까요? (예: 5-30분, 최소만은 10-, 아니면 Enter): ").strip()
    if ans:
        try:
            parts = ans.replace("분", "").split("-")
            lo = parts[0].strip()
            hi = parts[1].strip() if len(parts) > 1 else ""
            if lo:
                dur_min_sec = int(float(lo) * 60)
            if hi:
                dur_max_sec = int(float(hi) * 60)
            log(f"길이 조건: {lo or '제한없음'}분 ~ {hi or '제한없음'}분")
        except ValueError:
            log("길이 조건 파싱 실패 → 길이 조건 없이 진행")
            dur_min_sec = dur_max_sec = None

    start_idx = end_idx = None
    if with_range:
        ans = input("번호 범위를 지정할까요? (예: 1-50, 아니면 Enter=전체): ").strip()
        if ans:
            try:
                parts = ans.split("-")
                start_idx = int(parts[0].strip())
                end_idx = int(parts[1].strip()) if len(parts) > 1 and parts[1].strip() else None
                if start_idx < 1:
                    raise ValueError("start < 1")
            except ValueError:
                log("범위 파싱 실패 → 전체 처리")
                start_idx = end_idx = None
    return date_op, date_val, dur_min_sec, dur_max_sec, start_idx, end_idx


# =============== 메인 ===============

def parse_cli(argv=None):
    """명령줄 인자. --retry-failed는 스케줄러용 무질문 실행."""
    p = argparse.ArgumentParser(description="YouTube 자막 배치 다운로더")
    p.add_argument("--retry-failed", nargs="?", const="all", default=None,
                   help='실패 목록 재시도 (무질문). 예: --retry-failed / --retry-failed "1,3-5"')
    p.add_argument("--retry-select", default=None, help="재시도 선택 (예: 1,3-5)")
    p.add_argument("--headless", action="store_true", help="알림 팝업 생략")
    return p.parse_known_args(argv)[0]


def run_headless_retry(selection_raw="all"):
    """--retry-failed 용: 질문 없이 실패 목록 재시도 (스케줄러가 호출).

    없는 영상은 자동 제외. 저장 위치는 지난번 값. 팝업 없음.
    """
    global SUBTITLE_DIR
    SUBTITLE_DIR = _load_last_save_dir() or SUBTITLE_DIR
    ensure_dirs()
    log(f"[START] 무질문 실패 재시도 (선택: {selection_raw or 'all'})")
    conn = db_init(DB_PATH)
    try:
        try:
            cleaned = db_cleanup_stale_running(conn)
        except Exception:
            cleaned = 0
        if cleaned:
            log(f"[정리] running 흔적 {cleaned}건 정리")
        failed = db_latest_failed(conn)
        if not failed:
            log("[안내] 재시도할 실패 기록이 없습니다")
            return
        try:
            picked = parse_selection(selection_raw or "all", len(failed))
        except ValueError:
            log("선택 파싱 실패 → 전체 재시도")
            picked = list(range(1, len(failed) + 1))
        targets = [failed[i - 1] for i in picked]
        gone = [r for r in targets if recommend_retry(r["reason"])[0] == "skip"]
        if gone:
            targets = [r for r in targets if recommend_retry(r["reason"])[0] != "skip"]
            log(f"없는 영상 {len(gone)}개 자동 제외 → {len(targets)}개 재시도")
        if not targets:
            log("재시도 대상 없음 → 종료")
            return
        _execute_retry_targets(conn, targets)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    finalize()
    log("[END] 무질문 재시도 완료")


def main():
    global SUBTITLE_DIR, HEADLESS
    args = parse_cli()
    HEADLESS = bool(args.headless or args.retry_failed is not None)
    if args.retry_failed is not None:
        run_headless_retry(args.retry_select or args.retry_failed)
        return
    SUBTITLE_DIR = ask_save_location()  # 저장 위치 먼저 확정 (이후 모든 경로가 여기를 따름)
    ensure_dirs()
    db_conn = db_init(DB_PATH)
    # P1-2: 지난 실행이 강제종료로 남긴 running 흔적 정리 (30분 지난 것만)
    try:
        cleaned = db_cleanup_stale_running(db_conn)
    except Exception as e:
        log(f"[경고] running 흔적 정리 실패: {e}")
        cleaned = 0
    if cleaned:
        log(f"[정리] 비정상 종료 흔적 {cleaned}건을 interrupted로 정리 "
            f"(모드 4 대상 아님 - 원본 모드로 다시 돌리면 미완료분만 처리됨)")
    try:
        _main_menu(db_conn)
    except KeyboardInterrupt:
        log("[중단] 사용자 중단 (Ctrl+C)")
    finally:
        try:
            db_conn.close()
        except Exception:
            pass

    finalize()
    log("[END] 전체 작업 완료")


def _main_menu(db_conn):
    log("[START] YouTube 자막 배치 다운로드 (자막 전용 모드)")
    try:
        counts = db_count_by_status(db_conn)
    except Exception:
        counts = {}
    failed_n = counts.get("failed", 0)
    print("=" * 55)
    print("  1) 채널 전체 자막 (날짜/길이/번호 조건 가능)")
    print("  2) 개별 영상 자막")
    print("  3) 재생목록 자막 (전체 or 조건)")
    print(f"  4) 실패 목록 재시도/예약 (현재 실패 {failed_n}개)")
    print("  5) 예약 관리 (스케줄러 조회/삭제)")
    print("=" * 55)
    mode = input("모드 선택 (1/2/3/4/5): ").strip()

    if mode == "4":
        run_retry(db_conn)
        return
    if mode == "5":
        run_schedule_manager()
        return

    langs, auto_subs, sub_format = ask_lang_config()

    if mode == "2":
        url = input("영상 URL: ").strip()
        if not url:
            log("[END] URL이 없어 종료")
            return
        date_op, date_val, dmin, dmax, _, _ = ask_optional_filters(with_range=False)
        run_single(url, date_op, date_val, dmin, dmax, langs, auto_subs, sub_format, db_conn)
    elif mode == "3":
        url = input("재생목록 URL: ").strip()
        if not url:
            log("[END] URL이 없어 종료")
            return
        date_op, date_val, dmin, dmax, s, e = ask_optional_filters(with_range=True)
        run_playlist(url, s, e, date_op, date_val, dmin, dmax, langs, auto_subs, sub_format, db_conn)
    elif mode == "1":
        url = input("채널 URL (@핸들/channel/c/user 모두 가능): ").strip()
        if not url:
            log("[END] URL이 없어 종료")
            return
        date_op, date_val, dmin, dmax, s, e = ask_optional_filters(with_range=True)
        run_channel(url, s, e, date_op, date_val, dmin, dmax, langs, auto_subs, sub_format, db_conn)
    else:
        log("[END] 잘못된 모드 번호")
        return


if __name__ == "__main__":
    main()
