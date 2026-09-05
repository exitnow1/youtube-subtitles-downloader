# 설명서: p3-1 작업 + 머지, 설정 파일 (P3-1)

- 브랜치: `p3-1-config-file` → `main`에 머지 / 머지 커밋: `ec484cd` / 일시: 2026-09-05 22:15 (KST)
- 저장소: youtube-subtitles-downloader
- 한 줄 요약: 코드 속 숫자(저장 위치·대기 시간 등)를 `subtitle_config.json`으로 빼서 메모장으로 바꾸게 함. 충돌 없이 자동 머지.

---

## 1. 앱 전체 구조 (지도)

```
main() 시작
  → (신규) 설정 파일 읽기 → 전역에 반영 (없으면 기본값으로 자동 생성)
  → 이후 전부(경로·대기·차단기·폴백언어)가 설정값을 따름
  → 기존 메뉴·다운로드 흐름 그대로
```

## 2. 사용 흐름 (이번 커밋 기준)

사용자 입력 변화 없음. 첫 실행 때 `subtitle_config.json`이 자동 생성됩니다.

```json
{
  "download_dir": "C:\\Users\\rpt53\\Downloads",
  "sleep_between_videos_min": 2.0,
  "sleep_between_videos_max": 5.0,
  "sleep_on_429_base": 60,
  "cookie_refresh_every_n_videos": 20,
  "sleep_retry_min": 8.0,
  "sleep_retry_max": 15.0,
  "max_consecutive_429": 3,
  "stale_running_minutes": 30,
  "fallback_langs": ["en"]
}
```

바꾸고 싶으면 메모장으로 고치고 다시 실행. 우선순위: 실행 중 직접 입력(저장 폴더) > 설정 파일 > 코드 기본값.

## 3. 동작 원리 (쉽게)

- **문제**: 저장 위치·대기 시간 같은 조절값이 코드에 박혀 있어 바꾸려면
  파이썬 파일을 열어야 했습니다. 초보자에게는 높은 벽입니다.
- **해결**: 조절값을 JSON 파일로 빼냈습니다. 시작할 때 파일을 읽어
  전역 변수에 덮어씁니다. 파생 경로(쿠키·로그·DB 위치)도 함께 다시
  계산해서, `download_dir` 한 줄로 전부 이사 갑니다.
- **안전망 3개**: 파일이 없으면 기본값으로 자동 생성 / 깨진 JSON이면
  경고 후 기본값으로 실행(죽지 않음) / 이상한 값은 해당 키만 기본값,
  모르는 키는 무시(미래 버전 호환).
- **git 정리**: 개인 설정 파일(`subtitle_config.json`)은 커밋 제외,
  기본 본보기(`subtitle_config.example.json`)만 커밋. stale 정리 기준도
  설정값(`stale_running_minutes`)을 따르게 통일.

## 4. 전체 그림에서 뭘 추가·수정했나

| 구분 | 위치 | 내용 |
|---|---|---|
| 추가 | `DEFAULT_CONFIG / load_config() / apply_config() / _cfg_num()` | 기본값·읽기·반영·숫자 검증. `CONFIG_PATH`는 스크립트 옆 고정 |
| 수정 | `main()` 첫 줄 | `apply_config(load_config())` + 설정 경로 로그. 이후 per-run 저장위치 선택은 그대로(실행 중 입력 우선) |
| 수정 | stale 정리 2곳 | `STALE_RUNNING_MINUTES` 설정값 사용 |
| 추가 | `subtitle_config.example.json` (커밋) | 기본값 본보기 |
| 수정 | `.gitignore` | 실제 `subtitle_config.json` 제외 |
| 머지 | `ec484cd` | `p3-1-config-file` → main, 충돌 없이 자동 머지 |

## 5. 검증

- 모의 테스트 6종 PASS: 자동생성 / 부분 병합·미지키 무시 / 깨진 JSON·리스트형 /
  전역 반영·파생 경로 / 이상값 per-key 폴백 / example 일치
- 머지 후 회귀 4종 PASS: P2-4·P2-3·제목언어 (+P2-2·없는영상·P1-1·저장위치는 p2-3 머지 시 확인)
- `py_compile` 정상, Gitleaks 통과, `856772e..ec484cd main -> main` 푸시 확인
