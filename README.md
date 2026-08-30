# YouTube Downloader (yt-dlp 20분 분할)

유튜브 영상/재생목록을 20분 단위로 자동 분할 다운로드하는 `yt-dlp` 기반 Python 도구. 차단 방지(Sleep/Jitter, 429 백오프, 쿠키 원자적 교체) 및 차세대 안정성 패치 적용.

## 기능

- 단일 영상 / 재생목록 모두 지원
- 20분(1200초) 단위 `download_sections` 분할 — 파일명 `_part001of005` 로 고유화, `nooverwrites` 충돌 해결
- `DOWNLOAD_DIR` 절대경로 저장 (`C:\Users\rpt53\Downloads` 기본, `ensure_download_dir()`로 자동 생성)
- 재생목록 `extract_flat=True`로 목록만 빠르게 조회, `entries` 제너레이터 대응
- 쿠키: `browser_cookie3`로 Chrome/Edge/Firefox 폴백, 임시파일 원자적 교체(`.tmp` → `os.replace`), 실패 시 기존 유지
- 차단 방지: `concurrent_fragments=1`, `sleep_interval 1.5~6초`, 영상/청크 간 랜덤 대기, 429 시 60~90초 백오프 + 쿠키 갱신
- 입력: 한 줄에 하나씩, 빈 줄로 종료, `url1 url2` 공백 분리도 지원

## 요구사항

- Python 3.10+
- `pip install yt-dlp browser_cookie3`
- `ffmpeg` (PATH 등록, `ffmpeg -version` 확인) — 20분 분할 필수

## 사용법

```bash
python yt_dlp_advanced_downloader_20_min.py
# URL 입력 (빈 줄로 종료) > https://www.youtube.com/watch?v=...
# URL 입력 (빈 줄로 종료) > https://www.youtube.com/playlist?list=...
# (빈 줄)
# 재생목록 범위를 지정할까요? (예: 3-10, 아니면 Enter):
```

로그: `DOWNLOAD_DIR/download.log`, 실패 목록: `FAIL_LIST_<timestamp>.txt`

## 설정

파일 상단 상수에서 경로/튜닝 값 변경:

```python
DOWNLOAD_DIR = r"C:\Users\rpt53\Downloads"
CHUNK_SECONDS = 20 * 60
SLEEP_BETWEEN_VIDEOS_MIN/MAX = 5.0/12.0
SLEEP_BETWEEN_CHUNKS_MIN/MAX = 2.0/5.0
SLEEP_ON_429_BASE = 60
```

## P0 패치 이력

1. `outtmpl` 절대경로화
2. 청크 파일명 고유화
3. `input()` 다중줄 입력 루프
4. `entries` 제너레이터 + `extract_flat`
5. 쿠키 백업 순서 원자화

## 라이선스

MIT
