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
import time
import random
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

# --- 차단 방지 튜닝 값 (자막은 가벼우니 미디어 모드보다 짧게) ---
SLEEP_BETWEEN_VIDEOS_MIN = 2.0
SLEEP_BETWEEN_VIDEOS_MAX = 5.0
SLEEP_ON_429_BASE = 60  # 429 시 대기
COOKIE_REFRESH_EVERY_N_VIDEOS = 20  # 20개 영상마다 쿠키 갱신

# --- 재시도 안전값 (실패했 easing던 영상들이라 더 여유 있게) ---
SLEEP_RETRY_MIN = 8.0
SLEEP_RETRY_MAX = 15.0
MAX_CONSECUTIVE_429 = 3  # 429가 3회 연속이면 나머지 중단 (차단기)

# 없는 영상(삭제/비공개/찾을 수 없음) 신호 — 재시도해도 살아나지 않으므로 1회로 종료
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


def download_subs_for_video(url: str, mode: str, sub_filter: SubtitleFilter,
                            langs, auto_subs: bool, sub_format: str, noplaylist: bool = False,
                            title_hint=None, db_conn=None, max_attempts: int = 3):
    """영상 1개의 자막 다운로드.

    DB에 시도 시작/종료(시각·제목·URL·결과)를 기록하고, 성공 시 자막 파일
    상단에 완료시각·제목·URL 헤더를 기입합니다.
    반환: (result, saw_429) — result는 downloaded / skipped / failed / cached.
    cached는 DB에 같은 언어 완료 기록이 있고 파일도 그대로 있을 때
    (유튜브에 요청을 1번도 보내지 않음).
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
    for attempt in range(1, max_attempts + 1):
        try:
            # 필터 판정 기록을 위해 영상마다 새 검사기 상태로 시작
            sub_filter.last_kept = None
            sub_filter.last_reason = ""
            sub_filter.seen_title = None
            sub_filter.seen_id = None
            ydl = make_sub_ydl(outtmpl, langs, auto_subs, sub_format, match_filter=sub_filter)
            if noplaylist:
                ydl.params["noplaylist"] = True
            with ydl:
                ydl.download([url])
            # 예외 없이 끝남 → 필터가 스킵했는지 확인
            real_title = sub_filter.seen_title or started_title
            if sub_filter.last_kept is False:
                SKIPPED_LIST.append({"title": real_title, "reason": sub_filter.last_reason})
                _db_finish(db_conn, job_id, "skipped", sub_filter.last_reason, real_title, "")
                return "skipped", saw_429
            finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            video_id = sub_filter.seen_id or parse_video_id_from_url(url)
            stamped = add_header_to_subtitles(video_id, real_title, url, finished_at)
            log(f"[END] 자막 저장: {real_title}")
            SUCCESS_LIST.append(real_title)
            _db_finish(db_conn, job_id, "success", "", real_title, "\n".join(stamped))
            return "downloaded", saw_429
        except Exception as e:
            msg = str(e).lower()
            # 없는 영상은 재시도해도 살아나지 않음 → 1회로 종료, DB·실패목록에 사유 기록
            if any(p in msg for p in UNAVAILABLE_PATTERNS):
                real_title = sub_filter.seen_title or started_title
                reason = f"영상 없음(삭제/비공개/찾을 수 없음): {str(e)[:300]}"
                log(f"[영상 없음] 재시도 없이 실패 처리: {real_title}")
                FAIL_LIST.append({"title": real_title, "url": url})
                _db_finish(db_conn, job_id, "failed", reason, real_title, "")
                return "failed", False
            is_429 = "429" in msg or "too many requests" in msg
            is_403 = "403" in msg or "forbidden" in msg
            if is_429:
                saw_429 = True
                wait = SLEEP_ON_429_BASE + random.uniform(10, 30)
                log(f"[경고] 429 감지 (시도 {attempt}/{max_attempts}): {e} → {wait:.0f}초 대기")
                time.sleep(wait)
                export_cookies_from_chrome()
            elif is_403:
                log(f"[경고] 403 (시도 {attempt}/{max_attempts}): 쿠키 갱신 후 재시도")
                export_cookies_from_chrome()
                time.sleep(random.uniform(5, 10))
            else:
                log(f"[경고] 자막 실패 (시도 {attempt}/{max_attempts}): {e}")
                if attempt < max_attempts:
                    backoff = random.uniform(3, 6) * attempt
                    log(f"  → {backoff:.1f}초 후 재시도")
                    time.sleep(backoff)
            if attempt == max_attempts:
                real_title = sub_filter.seen_title or started_title
                log(f"[END] 자막 실패: {real_title} / {e}")
                FAIL_LIST.append({"title": real_title, "url": url})
                _db_finish(db_conn, job_id, "failed", str(e)[:500], real_title, "")
                return "failed", saw_429


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
    for i, row in enumerate(failed, 1):
        print(f"{i}. {row['title'] or '(제목없음)'} | {row['url']}")
        print(f"   실패시각: {row['finished_at']} | 누적시도 {row['attempt_no']}회 | {str(row['reason'])[:80]}")
    try:
        picked = parse_selection(
            input("재시도할 번호 (예: all / 1,3 / 2-5, Enter=전체): "), len(failed))
    except ValueError:
        log("선택 파싱 실패 → 취소 (숫자와 , - 만 사용)")
        return
    if not picked:
        log("선택 없음 → 취소")
        return
    targets = [failed[i - 1] for i in picked]

    target_time = ask_schedule()
    if target_time is not None:
        log(f"예약: {target_time.strftime('%Y-%m-%d %H:%M')}에 재시도 시작")
        if not wait_until(target_time):
            return  # 예약 취소

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

def main():
    global SUBTITLE_DIR
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
            f"(모드 4 대상 아님 — 원본 모드로 다시 돌리면 미완료분만 처리됨)")
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
    print("=" * 55)
    mode = input("모드 선택 (1/2/3/4): ").strip()

    if mode == "4":
        run_retry(db_conn)
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
