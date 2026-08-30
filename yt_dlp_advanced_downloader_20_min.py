import os
import math
import time
import random
import sys
import shutil
import subprocess
from datetime import datetime
import http.cookiejar as cookielib
import browser_cookie3  # pip install browser-cookie3
from yt_dlp import YoutubeDL  # pip install yt-dlp

# =============== 환경 설정 ===============
# YTDLP_EXE: yt-dlp CLI를 직접 호출할 때만 사용. 현재는 라이브러리(YoutubeDL) 방식이므로 미사용.
YTDLP_EXE = r"C:\Users\rpt53\yt-dlp.exe"
DOWNLOAD_DIR = r"C:\Users\rpt53\Downloads"
COOKIE_FILENAME = "cookies.txt"
COOKIE_PATH = os.path.join(DOWNLOAD_DIR, COOKIE_FILENAME)
LOG_FILE = os.path.join(DOWNLOAD_DIR, "download.log")

# 결과 집계용
SUCCESS_LIST = []
FAIL_LIST = []  # dict: {title, url, index}

CHUNK_SECONDS = 20 * 60  # 20분 단위(1200초)

# --- 차단 방지 튜닝 값 (요청 간 딜레이) ---
SLEEP_BETWEEN_VIDEOS_MIN = 5.0
SLEEP_BETWEEN_VIDEOS_MAX = 12.0
SLEEP_BETWEEN_CHUNKS_MIN = 2.0
SLEEP_BETWEEN_CHUNKS_MAX = 5.0
SLEEP_ON_429_BASE = 60  # 429 시 대기
COOKIE_REFRESH_EVERY_N_CHUNKS = 5
COOKIE_REFRESH_EVERY_N_VIDEOS = 3

# =============== 유틸 ===============

def ensure_download_dir():
    """DOWNLOAD_DIR과 하위 로그 경로 보장 (P0: 폴더 없으면 실패)"""
    try:
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    except Exception as e:
        print(f"[경고] DOWNLOAD_DIR 생성 실패 {DOWNLOAD_DIR}: {e}")


def check_ffmpeg() -> bool:
    """download_sections는 ffmpeg 필수. 없으면 청크 기능이 무력화되므로 경고."""
    has = shutil.which("ffmpeg") is not None
    # yt-dlp.exe 번들에 ffmpeg가 내장된 경우도 있으므로 추가 체크
    if not has:
        try:
            subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
            has = True
        except Exception:
            has = False
    if not has:
        log("[경고] ffmpeg를 찾을 수 없습니다. 20분 분할(download_sections)은 ffmpeg 없이는 동작하지 않습니다. https://ffmpeg.org 에서 설치 후 PATH 등록하세요.", to_file=True)
    return has


def log(msg: str, to_file: bool = True):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    if to_file:
        try:
            ensure_download_dir()
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            print(f"[로그 실패] {e}")


def rotate_old_cookie():
    """기존 쿠키 백업. 호출 전 성공 저장이 보장된 뒤에만 호출해야 함 (P0 수정)."""
    if os.path.exists(COOKIE_PATH):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = os.path.join(DOWNLOAD_DIR, f"{ts}_cookies.txt")
        # 동일 초에 여러 번 호출될 때 충돌 방지
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
    """여러 브라우저/도메인 조합으로 쿠키 로드 시도. 차단 방지: 실패해도 프로그램 중단 안 함."""
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
            # CookieJar는 list()로 소진되지 않지만, 안전하게 리스트로 복사 후 길이 체크
            cookies_list = list(cj)
            count = len(cookies_list)
            if count == 0:
                continue
            # 이미 로드된 리스트를 다시 CookieJar로 감싸지 않고, 원본 cj 반환 전 리스트를 유지
            # 호출부에서 list(cj) 대신 cookies_list를 직접 순회하도록, 원본 cj 대신 임시 리스트 반환 호환을 위해
            # 여기서는 cookies_list를 반환해도 되지만, 기존 로직과 호환 위해 cj를 반환하되 count만 로그
            # 단, list()로 소진 문제 없으므로 두 번째 로드 불필요 - 첫 cj 그대로 반환
            log(f"쿠키 로드 성공: {name} ({count}개)")
            # browser_cookie3가 반환한 CookieJar를 그대로 쓰되, 호출부에서 list(cj) 대신 cookies_list를 쓰도록
            # 호환: cookies_list를 담은 간이 객체 반환 대신 cj를 반환하고, 호출부는 cj를 그대로 순회 (CookieJar는 재순회 가능)
            return cj
        except Exception as e:
            log(f"쿠키 로드 실패 ({name}): {e}", to_file=False)
            continue
    return None


def export_cookies_from_chrome() -> bool:
    """
    P0 수정: 기존 쿠키를 먼저 지우지 않고, 임시 파일에 저장 성공 후에만 rotate+replace.
    반환값: 성공 True / 실패 False (실패 시 기존 쿠키 유지)
    """
    log("[START] 쿠키 추출")
    # 1. 쿠키 로드 (브라우저 DB 잠김 등 예외는 여기서 잡힘)
    cj = _try_load_browser_cookies()
    if cj is None:
        log("[경고] 쿠키 추출 실패 → 기존 cookies.txt 유지 (브라우저가 열려있으면 닫고 재시도하세요)")
        return False

    # 2. 임시 파일에 먼저 저장
    tmp_path = COOKIE_PATH + ".tmp"
    try:
        ensure_download_dir()
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

    # 3. 성공 후에만 기존 백업 + 교체 (원자적 이동)
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


def sec_to_hms(sec: int) -> str:
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def sec_to_min_str(sec: int) -> str:
    mins = sec / 60
    return f"{mins:.1f}분"

# =============== yt-dlp 래퍼 ===============

def make_ydl(progress_title: str, outtmpl: str, download_sections=None):
    """
    차단 방지 안전 옵션 적용:
    - sleep_interval / max_sleep_interval / sleep_interval_requests 로 요청 간 딜레이
    - concurrent_fragments 1로 낮춰 동시 요청 폭주 방지 (기존 5 → 1)
    - extractor_retries / socket_timeout / extractor_args(player_client) 추가
    - http_headers로 일반 브라우저 UA 흉내
    P0 수정: outtmpl은 호출부에서 이미 DOWNLOAD_DIR을 포함한 절대 템플릿을 넘겨야 함
    """
    # 쿠키 파일이 없으면 옵션에서 제외 (yt-dlp가 경고 출력 방지)
    cookie_opt = COOKIE_PATH if os.path.exists(COOKIE_PATH) else None

    ydl_opts = {
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "continue_dl": True,
        "nooverwrites": True,
        "retries": 10,
        "fragment_retries": 10,
        "concurrent_fragments": 1,  # 안전: 5 → 1 (차단 위험 대폭 감소, 느려지지만 안정)
        "extractor_retries": 3,
        "socket_timeout": 30,
        # ---- 차단 방지 핵심: 요청 간 휴식 ----
        "sleep_interval": 1.5,
        "max_sleep_interval": 6,
        "sleep_interval_requests": 1,
        "sleep_interval_subtitles": 1,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
        },
        # youtube extractor가 429/잠깐 차단을 덜 받도록 player_client 다변화
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"],
            }
        },
        "geo_bypass": True,
        "ignoreerrors": False,  # 개별 영상 실패는 상위에서 catch
        "noprogress": False,
        "quiet": False,
        "no_warnings": False,
    }
    if cookie_opt:
        ydl_opts["cookiefile"] = cookie_opt

    # 진행률 로그는 10% 단위로만 출력해 스팸 방지
    last_pct = {"v": -10}

    def progress_hook(d):
        if d.get('status') == 'downloading':
            total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate')
            downloaded = d.get('downloaded_bytes')
            if total_bytes and downloaded is not None:
                pct = int(downloaded / total_bytes * 100)
                # 10% 단위로만 로그 (차단 방지와 무관하지만 로그 폭주 방지)
                if pct // 10 != last_pct["v"] // 10:
                    last_pct["v"] = pct
                    log(f"{progress_title} 진행률: {pct}%", to_file=False)
        elif d.get('status') == 'finished':
            log(f"[END] {progress_title} 구간 다운로드 완료")

    ydl_opts["progress_hooks"] = [progress_hook]

    if download_sections:
        ydl_opts["download_sections"] = download_sections

    return YoutubeDL(ydl_opts)

# =============== 메타 데이터 ===============

def fetch_info(url: str, extract_flat: bool = False):
    """
    P0 수정: extract_flat 옵션 추가로 플레이리스트 목록 조회 시 네트워크 폭주 방지.
    extract_flat=True면 각 영상의 상세 메타(duration 등)를 따로 조회하지 않고 목록만 빠르게 가져옴.
    일반 영상 duration 조회 시에는 extract_flat=False로 호출.
    차단 방지: sleep/retry 옵션 포함, context manager 사용.
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "extract_flat": extract_flat,
        "skip_download": True,
        "extractor_retries": 3,
        "socket_timeout": 30,
        "sleep_interval": 1.5,
        "max_sleep_interval": 5,
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
        info = ydl.extract_info(url, download=False)
        return info

# =============== 다운로드 코어 ===============

def build_chunk_outtmpl(is_playlist: bool, chunk_idx: int, total_chunks: int) -> str:
    """
    P0 수정: DOWNLOAD_DIR 포함 + 청크별 고유 파일명 생성 (nooverwrites 충돌 해결)
    예) Downloads/플레이리스트명/001_제목_part001of005.mp4
        Downloads/제목_part001of005.mp4
    yt-dlp의 %(title).150s 등은 그대로 템플릿으로 두고, _partXXX 만 파이썬에서 치환.
    """
    ensure_download_dir()
    suffix = f"_part{chunk_idx:03d}of{total_chunks:03d}.%(ext)s"
    if is_playlist:
        # 플레이리스트면 하위 폴더에 저장. %(playlist_title)s가 비어있을 경우 대비해 fallback은 yt-dlp가 처리
        template = f"%(playlist_title)s/%(playlist_index)03d_%(title).150s{suffix}"
    else:
        template = f"%(title).150s{suffix}"
    # 절대 경로로 변환 (P0)
    return os.path.join(DOWNLOAD_DIR, template)


def build_whole_outtmpl(is_playlist: bool) -> str:
    ensure_download_dir()
    if is_playlist:
        template = "%(playlist_title)s/%(playlist_index)03d_%(title).150s.%(ext)s"
    else:
        template = "%(title).150s.%(ext)s"
    return os.path.join(DOWNLOAD_DIR, template)


def download_chunk(url: str, start_hms: str, end_hms: str, is_playlist: bool, title_for_log: str, chunk_idx: int, total_chunks: int):
    outtmpl = build_chunk_outtmpl(is_playlist, chunk_idx, total_chunks)
    sections = [f"*{start_hms}-{end_hms}"] if end_hms else [f"*{start_hms}-"]
    # ffmpeg 없으면 download_sections가 무시될 수 있으므로 사전 체크 로그는 download_video_in_chunks에서 1회만
    ydl = make_ydl(progress_title=title_for_log, outtmpl=outtmpl, download_sections=sections)
    with ydl:
        ydl.download([url])


def download_video_in_chunks(url: str, is_playlist: bool, video_title: str):
    log(f"[START] 영상 처리: {video_title}")
    # duration 확인용 상세 조회 (flat=False)
    info = fetch_info(url, extract_flat=False)
    if info is None:
        raise RuntimeError("영상 정보를 가져오지 못했습니다 (비공개/삭제/차단 가능)")

    # 플레이리스트 entry가 그대로 넘어온 경우 duration이 최상위에 없을 수 있음 → entry 자체가 video info
    duration = info.get("duration")

    if not duration:
        # 길이를 모르면 전체 다운로드
        log("[START] 전체 다운로드 (길이 미상 - 분할 생략)")
        outtmpl = build_whole_outtmpl(is_playlist)
        ydl = make_ydl(progress_title=video_title, outtmpl=outtmpl)
        with ydl:
            ydl.download([url])
        log(f"[END] 전체 다운로드: {video_title}")
        SUCCESS_LIST.append(video_title)
        # 차단 방지: 영상 간 대기
        _sleep_between_videos()
        return

    log(f"영상 길이: {sec_to_min_str(duration)}")

    # ffmpeg 체크 (청크 기능 필수)
    has_ffmpeg = check_ffmpeg()
    if not has_ffmpeg:
        log("[경고] ffmpeg 없음 → 분할 없이 전체 다운로드로 폴백")
        outtmpl = build_whole_outtmpl(is_playlist)
        ydl = make_ydl(progress_title=video_title, outtmpl=outtmpl)
        with ydl:
            ydl.download([url])
        log(f"[END] 전체 다운로드(폴백): {video_title}")
        SUCCESS_LIST.append(video_title)
        _sleep_between_videos()
        return

    # n개 20분 조각으로 나누기
    num_chunks = math.ceil(duration / CHUNK_SECONDS)
    log(f"20분 단위 조각 수: {num_chunks}개")

    for idx in range(num_chunks):
        start_sec = idx * CHUNK_SECONDS
        end_sec = min((idx + 1) * CHUNK_SECONDS, duration)
        start_hms = sec_to_hms(start_sec)
        end_hms = sec_to_hms(end_sec)

        log(f"[START] 조각 {idx+1}/{num_chunks}: {start_hms} ~ {end_hms}")
        # --- 차단 방지: 조각별 재시도 래퍼 (429/403 감지) ---
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                download_chunk(url, start_hms, end_hms, is_playlist, f"{video_title} (chunk {idx+1})", chunk_idx=idx+1, total_chunks=num_chunks)
                break  # 성공
            except Exception as e:
                msg = str(e).lower()
                is_429 = "429" in msg or "too many requests" in msg
                is_403 = "403" in msg or "forbidden" in msg
                is_429_like = is_429 or "http error 429" in msg or "unable to download" in msg and "429" in msg

                if is_429_like:
                    wait = SLEEP_ON_429_BASE + random.uniform(10, 30)
                    log(f"[경고] 429/차단 감지 (시도 {attempt}/{max_attempts}): {e} → {wait:.0f}초 대기 후 재시도")
                    time.sleep(wait)
                    # 429면 쿠키 갱신이 도움 될 수 있음
                    export_cookies_from_chrome()
                elif is_403:
                    log(f"[경고] 403 Forbidden (시도 {attempt}/{max_attempts}): {e} → 쿠키 갱신 후 재시도")
                    export_cookies_from_chrome()
                    time.sleep(random.uniform(5, 10))
                else:
                    log(f"[경고] 조각 실패 (시도 {attempt}/{max_attempts}): {e}")
                    if attempt < max_attempts:
                        backoff = random.uniform(3, 6) * attempt
                        log(f"  → {backoff:.1f}초 후 재시도")
                        time.sleep(backoff)

                if attempt == max_attempts:
                    raise  # 상위 process_url에서 FAIL_LIST로 처리

        log(f"[END] 조각 {idx+1}/{num_chunks}: {start_hms} ~ {end_hms}")

        # P0 수정: 매 조각 쿠키 리프레시 제거 → 5 조각마다만, 또는 마지막에 1회
        # 차단 방지: 청크 간 짧은 랜덤 대기 (봇 탐지 회피)
        if idx < num_chunks - 1:
            jitter = random.uniform(SLEEP_BETWEEN_CHUNKS_MIN, SLEEP_BETWEEN_CHUNKS_MAX)
            log(f"청크 간 대기 {jitter:.1f}초 (차단 방지)")
            time.sleep(jitter)
            if (idx + 1) % COOKIE_REFRESH_EVERY_N_CHUNKS == 0:
                log(f"[START] 주기적 쿠키 재추출 ({COOKIE_REFRESH_EVERY_N_CHUNKS}청크마다)")
                export_cookies_from_chrome()
                log("[END] 주기적 쿠키 재추출")

    log(f"[END] 영상 처리 완료: {video_title}")
    SUCCESS_LIST.append(video_title)
    _sleep_between_videos()


def _sleep_between_videos():
    """차단 방지: 영상 처리 사이 랜덤 대기"""
    jitter = random.uniform(SLEEP_BETWEEN_VIDEOS_MIN, SLEEP_BETWEEN_VIDEOS_MAX)
    log(f"영상 간 대기 {jitter:.1f}초 (차단 방지)")
    time.sleep(jitter)


def _extract_video_url(entry: dict) -> str | None:
    """flat/non-flat entry 모두에서 재생 가능한 URL 추출 (P0: generator/flat 대응)"""
    if not entry:
        return None
    # flat 모드면 id만 있는 경우가 많음
    url = entry.get("webpage_url") or entry.get("url")
    if url and isinstance(url, str) and url.startswith("http"):
        return url
    # url이 비디오 ID 형태일 수 있음
    vid_id = entry.get("id")
    if vid_id and isinstance(vid_id, str):
        # 유튜브 ID는 보통 11자
        if len(vid_id) == 11 and "/" not in vid_id:
            return f"https://www.youtube.com/watch?v={vid_id}"
        if vid_id.startswith("http"):
            return vid_id
        # url이 ID일 경우
        if url and isinstance(url, str) and len(url) == 11:
            return f"https://www.youtube.com/watch?v={url}"
        # fallback: id를 URL로
        if url and isinstance(url, str):
            return url
        return f"https://www.youtube.com/watch?v={vid_id}"
    return url

# =============== URL 처리 ===============

def process_url(url: str, start_idx: int = None, end_idx: int = None):
    # 시작 시 1회만 쿠키 시도 (P0: 실패해도 중단 안 함)
    export_cookies_from_chrome()
    # P0 수정: 플레이리스트 목록은 flat으로 빠르게 가져와서 차단/속도 개선
    # flat=True로 목록만 가져오고, 개별 영상은 download_video_in_chunks 내부에서 상세 조회
    info = fetch_info(url, extract_flat=True)

    if info is None:
        log(f"[END] 정보를 가져오지 못함: {url}")
        FAIL_LIST.append({"title": url, "url": url, "index": None})
        return

    # entries가 있으면 재생목록 (P0: list/generator 모두 대응)
    entries = info.get("entries")
    if entries is not None:
        # generator/list 모두 안전하게 리스트화
        try:
            if not isinstance(entries, list):
                # 제너레이터일 수 있으므로 list()로 소모
                entries = list(entries)
        except Exception as e:
            log(f"[경고] entries 변환 실패: {e} → 빈 목록 처리")
            entries = []

        # None 필터링
        entries = [e for e in entries if e]

        total = len(entries)
        if total == 0:
            log("[경고] 재생목록이 비어있거나 모두 비공개/삭제됨 → 단일 영상으로 폴백 시도")
            vid_title = info.get("title") or "single_video"
            try:
                download_video_in_chunks(url, is_playlist=False, video_title=vid_title)
            except Exception as e:
                log(f"[END] 다운로드 실패(단일 폴백): {vid_title} / 오류: {e}")
                FAIL_LIST.append({"title": vid_title, "url": url, "index": None})
            return

        log(f"[START] 재생목록 처리: 총 {total}개")
        # 범위 보정
        s = start_idx if start_idx is not None else 1
        e = end_idx if end_idx is not None else total
        s = max(1, s)
        e = min(total, e)
        if s > e:
            log(f"[경고] 범위 오류 s={s} > e={e} → 전체 처리로 폴백")
            s, e = 1, total
        log(f"재생목록 범위: {s} ~ {e}")

        for idx, entry in enumerate(entries, start=1):
            if idx < s or idx > e:
                continue
            vid_url = _extract_video_url(entry)
            vid_title = entry.get("title") or f"playlist_item_{idx}"
            if not vid_url:
                log(f"[경고] URL 추출 실패 idx={idx}: {entry}")
                FAIL_LIST.append({"title": vid_title, "url": str(entry), "index": idx})
                continue
            try:
                log(f"[START] 재생목록 영상 {idx}/{total}: {vid_title}")
                download_video_in_chunks(vid_url, is_playlist=True, video_title=vid_title)
                log(f"[END] 재생목록 영상 {idx}/{total}: {vid_title}")
                # 주기적 쿠키 리프레시 (영상 단위)
                if idx % COOKIE_REFRESH_EVERY_N_VIDEOS == 0:
                    log(f"[주기] {COOKIE_REFRESH_EVERY_N_VIDEOS}개 영상마다 쿠키 갱신")
                    export_cookies_from_chrome()
            except Exception as ex:
                log(f"[END] 다운로드 실패: {vid_title} / 오류: {ex}")
                FAIL_LIST.append({"title": vid_title, "url": vid_url, "index": idx})
                # 실패 후에도 차단 방지 대기
                wait = random.uniform(5, 10)
                log(f"실패 후 대기 {wait:.1f}초")
                time.sleep(wait)
        log(f"[END] 재생목록 처리 완료")
    else:
        vid_title = info.get("title") or "single_video"
        try:
            download_video_in_chunks(url, is_playlist=False, video_title=vid_title)
        except Exception as e:
            log(f"[END] 다운로드 실패(단일): {vid_title} / 오류: {e}")
            FAIL_LIST.append({"title": vid_title, "url": url, "index": None})

# =============== 종료 정리 ===============

def finalize():
    if FAIL_LIST:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        ensure_download_dir()
        fail_path = os.path.join(DOWNLOAD_DIR, f"FAIL_LIST_{ts}.txt")
        with open(fail_path, "w", encoding="utf-8") as f:
            for item in FAIL_LIST:
                line = f"title={item['title']} | url={item['url']} | index={item['index']}"
                f.write(line + "\n")
        log(f"[END] 실패 리스트 저장 → {fail_path}")

    print("\n======= 다운로드 실패한 영상 =======")
    if FAIL_LIST:
        for item in FAIL_LIST:
            print(f"- {item['title']} | {item['url']} | index={item['index']}")
    else:
        print("(없음)")

    print("\n======= 다운로드 성공한 영상 =======")
    if SUCCESS_LIST:
        for t in SUCCESS_LIST:
            print(f"+ {t}")
    else:
        print("(없음)")

    # 윈도우 알림
    try:
        import winsound
        winsound.MessageBeep()
    except Exception:
        pass
    try:
        from ctypes import windll
        windll.user32.MessageBoxW(0, "모든 다운로드 작업이 종료되었습니다.", "yt-dlp downloader", 0)
    except Exception:
        pass

# =============== 메인 ===============

def main():
    ensure_download_dir()
    log("[START] yt-dlp 20분 자동다운로드 (차단 방지 모드)")
    check_ffmpeg()

    # P0 수정: input() 한 줄 문제 해결 → 빈 줄까지 여러 줄 입력
    print("YouTube URL/Playlist URL을 한 줄에 하나씩 입력하세요.")
    print("붙여넣기 후 빈 줄(엔터만)으로 종료합니다.")
    urls = []
    # 프롬프트 개선: 첫 입력 전 안내
    while True:
        try:
            prompt = "URL 입력 (빈 줄로 종료) > " if urls else "URL 입력 > "
            line = input(prompt)
        except EOFError:
            break
        # 빈 줄 처리
        if not line.strip():
            if urls:
                break
            else:
                # 첫 줄이 빈 줄이면 종료
                break
        # 공백 분리된 여러 URL을 한 줄에 붙여넣은 경우도 지원
        # 예: "url1 url2 url3" → 3개로 분리
        parts = line.strip().split()
        for p in parts:
            p = p.strip()
            if p:
                urls.append(p)

    if not urls:
        log("[END] URL이 없어 종료")
        return

    # 입력된 URL 로그
    log(f"입력된 URL {len(urls)}개")
    for i, u in enumerate(urls, 1):
        log(f"  {i}. {u}")

    range_ans = input("재생목록 범위를 지정할까요? (예: 3-10, 아니면 Enter): ").strip()
    start_idx = None
    end_idx = None
    if range_ans:
        try:
            parts = range_ans.split("-")
            start_idx = int(parts[0].strip())
            end_idx = int(parts[1].strip()) if len(parts) > 1 and parts[1].strip() else None
            if start_idx < 1:
                raise ValueError("start <1")
        except ValueError:
            log("범위 파싱 실패 → 전체 처리")
            start_idx, end_idx = None, None

    for url in urls:
        try:
            process_url(url, start_idx=start_idx, end_idx=end_idx)
        except KeyboardInterrupt:
            log("[중단] 사용자 중단 (Ctrl+C)")
            break
        except Exception as e:
            log(f"[경고] process_url 예외: {url} / {e}")
            FAIL_LIST.append({"title": url, "url": url, "index": None})

    finalize()
    log("[END] 전체 작업 완료")


if __name__ == "__main__":
    main()
