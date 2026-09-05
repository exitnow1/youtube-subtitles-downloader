"""YouTube 자막 배치 다운로더 (yt-dlp 기반, 자막 전용 모드).

세 가지 모드:
  1) 채널 전체  : 채널 링크를 주면 영상 목록을 가져와 자막을 일괄 다운로드
                  (업로드 날짜 6종 연산자 + 길이 + 번호 범위로 조건 지정 가능)
  2) 개별 영상  : 영상 URL 1개의 자막 다운로드
  3) 재생목록   : 재생목록 내 영상 자막 다운로드 (전체 or 조건 지정)

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
from datetime import datetime
import http.cookiejar as cookielib
import browser_cookie3  # pip install browser-cookie3
from yt_dlp import YoutubeDL  # pip install yt-dlp

# =============== 환경 설정 ===============
DOWNLOAD_DIR = r"C:\Users\rpt53\Downloads"
SUBTITLE_DIR = os.path.join(DOWNLOAD_DIR, "subtitles")
COOKIE_FILENAME = "cookies.txt"
COOKIE_PATH = os.path.join(DOWNLOAD_DIR, COOKIE_FILENAME)
LOG_FILE = os.path.join(DOWNLOAD_DIR, "subtitle_download.log")

# 결과 집계용
SUCCESS_LIST = []  # 다운로드 시도된 영상 제목
SKIPPED_LIST = []  # 조건에 안 맞아 건너뛴 영상: dict {title, reason}
FAIL_LIST = []     # 실패: dict {title, url}

# --- 차단 방지 튜닝 값 (자막은 가벼우니 미디어 모드보다 짧게) ---
SLEEP_BETWEEN_VIDEOS_MIN = 2.0
SLEEP_BETWEEN_VIDEOS_MAX = 5.0
SLEEP_ON_429_BASE = 60  # 429 시 대기
COOKIE_REFRESH_EVERY_N_VIDEOS = 20  # 20개 영상마다 쿠키 갱신

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

    def _skip(self, title, reason):
        self.last_kept = False
        self.last_reason = reason
        log(f"  스킵: {title} ({reason})", to_file=False)
        return reason  # 문자열을 돌려주면 yt-dlp가 이 영상을 건너뜀

    def __call__(self, info, incomplete=False):
        if incomplete:
            return None
        title = info.get("title") or info.get("id") or "?"

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


def download_subs_for_video(url: str, mode: str, sub_filter: SubtitleFilter,
                            langs, auto_subs: bool, sub_format: str, noplaylist: bool = False):
    """영상 1개의 자막 다운로드. 결과에 따라 SUCCESS/SKIPPED/FAIL에 기록."""
    outtmpl = build_sub_outtmpl(mode)
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            # 필터 판정 기록을 위해 영상마다 새 검사기 상태로 시작
            sub_filter.last_kept = None
            sub_filter.last_reason = ""
            ydl = make_sub_ydl(outtmpl, langs, auto_subs, sub_format, match_filter=sub_filter)
            if noplaylist:
                ydl.params["noplaylist"] = True
            with ydl:
                ydl.download([url])
            # 예외 없이 끝남 → 필터가 스킵했는지 확인
            if sub_filter.last_kept is False:
                SKIPPED_LIST.append({"title": url, "reason": sub_filter.last_reason})
                return "skipped"
            log(f"[END] 자막 저장: {url}")
            SUCCESS_LIST.append(url)
            return "downloaded"
        except Exception as e:
            msg = str(e).lower()
            is_429 = "429" in msg or "too many requests" in msg
            is_403 = "403" in msg or "forbidden" in msg
            if is_429:
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
                log(f"[END] 자막 실패: {url} / {e}")
                FAIL_LIST.append({"title": url, "url": url})
                return "failed"


def _sleep_between_videos():
    jitter = random.uniform(SLEEP_BETWEEN_VIDEOS_MIN, SLEEP_BETWEEN_VIDEOS_MAX)
    log(f"영상 간 대기 {jitter:.1f}초 (차단 방지)")
    time.sleep(jitter)


def process_entries(entries, start_idx, end_idx, mode: str, label: str,
                    date_op=None, date_val=None, dur_min_sec=None, dur_max_sec=None,
                    langs=None, auto_subs=True, sub_format="vtt/best"):
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
    for idx, entry in enumerate(entries, start=1):
        if idx < s or idx > e:
            continue
        vid_url = _extract_video_url(entry)
        vid_title = (entry.get("title") if isinstance(entry, dict) else None) or f"item_{idx}"
        if not vid_url:
            log(f"[경고] URL 추출 실패 idx={idx}")
            FAIL_LIST.append({"title": vid_title, "url": str(entry)})
            continue
        done += 1
        log(f"[START] {label} {idx}/{total}: {vid_title}")
        sub_filter = SubtitleFilter(date_op, date_val, dur_min_sec, dur_max_sec)
        download_subs_for_video(vid_url, mode, sub_filter, langs, auto_subs, sub_format)
        log(f"[END] {label} {idx}/{total}: {vid_title}")
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
               langs=None, auto_subs=True, sub_format="vtt/best"):
    """모드 2: 개별 영상 1개의 자막."""
    export_cookies_from_chrome()
    log(f"[START] 개별 영상 자막: {url}")
    sub_filter = SubtitleFilter(date_op, date_val, dur_min_sec, dur_max_sec)
    # URL에 &list= 가 섞여 있어도 영상 1개만 처리 (재생목록 전체 받는 사고 방지)
    download_subs_for_video(url, "single", sub_filter, langs, auto_subs, sub_format, noplaylist=True)
    _sleep_between_videos()


def run_playlist(url: str, start_idx=None, end_idx=None, date_op=None, date_val=None,
                 dur_min_sec=None, dur_max_sec=None, langs=None, auto_subs=True, sub_format="vtt/best"):
    """모드 3: 재생목록 내 영상 자막 (전체 or 조건)."""
    export_cookies_from_chrome()
    info = fetch_flat_list(url)
    if info is None:
        log(f"[END] 정보를 가져오지 못함: {url}")
        FAIL_LIST.append({"title": url, "url": url})
        return
    entries = _to_entries_list(info)
    if entries is None:  # 재생목록이 아니라 단일 영상이면 그대로 처리
        log("[안내] 재생목록이 아니라 단일 영상으로 처리합니다")
        run_single(url, date_op, date_val, dur_min_sec, dur_max_sec, langs, auto_subs, sub_format)
        return
    title = info.get("title") or "재생목록"
    process_entries(entries, start_idx, end_idx, "playlist", f"재생목록({title})",
                    date_op, date_val, dur_min_sec, dur_max_sec, langs, auto_subs, sub_format)


def run_channel(url: str, start_idx=None, end_idx=None, date_op=None, date_val=None,
                dur_min_sec=None, dur_max_sec=None, langs=None, auto_subs=True, sub_format="vtt/best"):
    """모드 1: 채널 전체 영상 자막 (전체 or 날짜/길이/번호 조건)."""
    url = normalize_channel_url(url)
    log(f"채널 URL 정규화 → {url}")
    export_cookies_from_chrome()
    log("[안내] 채널 목록을 가져옵니다 (영상 많은 채널은 시간이 걸릴 수 있음)")
    info = fetch_flat_list(url)
    if info is None:
        log(f"[END] 정보를 가져오지 못함: {url}")
        FAIL_LIST.append({"title": url, "url": url})
        return
    entries = _to_entries_list(info)
    if entries is None:
        log("[경고] 채널 목록이 아니라 단일 항목입니다. 개별 모드로 처리합니다")
        run_single(url, date_op, date_val, dur_min_sec, dur_max_sec, langs, auto_subs, sub_format)
        return
    channel = info.get("channel") or info.get("uploader") or "채널"
    process_entries(entries, start_idx, end_idx, "channel", f"채널({channel})",
                    date_op, date_val, dur_min_sec, dur_max_sec, langs, auto_subs, sub_format)


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

    print("\n======= 자막 성공 =======")
    print("(없음)" if not SUCCESS_LIST else "\n".join(f"+ {t}" for t in SUCCESS_LIST))
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
    ensure_dirs()
    log("[START] YouTube 자막 배치 다운로드 (자막 전용 모드)")
    print("=" * 55)
    print("  1) 채널 전체 자막 (날짜/길이/번호 조건 가능)")
    print("  2) 개별 영상 자막")
    print("  3) 재생목록 자막 (전체 or 조건)")
    print("=" * 55)
    mode = input("모드 선택 (1/2/3): ").strip()

    langs, auto_subs, sub_format = ask_lang_config()

    try:
        if mode == "2":
            url = input("영상 URL: ").strip()
            if not url:
                log("[END] URL이 없어 종료")
                return
            date_op, date_val, dmin, dmax, _, _ = ask_optional_filters(with_range=False)
            run_single(url, date_op, date_val, dmin, dmax, langs, auto_subs, sub_format)
        elif mode == "3":
            url = input("재생목록 URL: ").strip()
            if not url:
                log("[END] URL이 없어 종료")
                return
            date_op, date_val, dmin, dmax, s, e = ask_optional_filters(with_range=True)
            run_playlist(url, s, e, date_op, date_val, dmin, dmax, langs, auto_subs, sub_format)
        elif mode == "1":
            url = input("채널 URL (@핸들/channel/c/user 모두 가능): ").strip()
            if not url:
                log("[END] URL이 없어 종료")
                return
            date_op, date_val, dmin, dmax, s, e = ask_optional_filters(with_range=True)
            run_channel(url, s, e, date_op, date_val, dmin, dmax, langs, auto_subs, sub_format)
        else:
            log("[END] 잘못된 모드 번호")
            return
    except KeyboardInterrupt:
        log("[중단] 사용자 중단 (Ctrl+C)")

    finalize()
    log("[END] 전체 작업 완료")


if __name__ == "__main__":
    main()
