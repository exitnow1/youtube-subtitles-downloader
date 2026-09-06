# 설명서: p5-1 작업 + 머지, 숏폼 지원 (P5-1)

- 브랜치: `p5-1-shorts-type` → `main`에 머지 / 머지 커밋: `75d89c3` / 일시: 2026-09-06 12:26 (KST)
- 저장소: youtube-subtitles-downloader
- 한 줄 요약: 롱폼만 받던 채널·재생목록에 숏폼 수집·분류·선택을 추가. 기본값은 롱폼(기존 동작 유지). 충돌 없이 자동 머지.

---

## 1. 앱 전체 구조 (지도)

```
채널 모드: 종류 선택(Enter=롱폼 / shorts / both)
  → long: /videos 탭만 (예전과 동일)
  → shorts: /shorts 탭만
  → both: 두 탭 수집 후 합침 → 종류별 하위 폴더에 저장
재생목록: 그대로 가져와서 종류 판별 후 필터 (탭 정보 없으니 URL·길이로 추정)
개별: 필터 없이 종류만 기록
```

## 2. 사용 흐름 (이번 커밋 기준)

```
채널 URL: https://www.youtube.com/@x
종류 (Enter=롱폼만 / shorts=숏폼만 / both=둘 다): both
...
[안내] videos 탭: 120개
[안내] shorts 탭: 45개
[START] 채널(CH) 1/165: 제목1 [long]
```

저장: `subtitles/채널명/videos/제목 [ID].vtt`, `subtitles/채널명/shorts/제목 [ID].vtt`.
자막 상단에 `Video-ID:` 행이 별도로 들어갑니다 (URL 외에 ID 단독 기록).

## 3. 동작 원리 (쉽게)

- **수집**: 채널은 탭이 곧 정답입니다. `/videos`에서 온 건 롱폼,
  `/shorts`에서 온 건 숏폼으로 찍고 목록에 `_tab` 표시를 붙입니다.
- **판정 순서** (`classify_type`): 수집 탭 → URL에 `/shorts/` 포함 →
  길이 추정(설정 `shorts_max_seconds` 이하, 기본 60초) → 나머지 롱폼.
  길이 추정은 보조 수단이라 한계가 문서화되어 있습니다.
- **필터**: `both`가 아니면 다른 종류는 요청 전에 제외하고 개수를 로그에 남깁니다.
- **DB·헤더**: `video_type`·`video_id` 컬럼 추가(옛 DB 자동 마이그레이션).
  시작 시 목록 기준, 종료 시 실측 ID로 확정 기록.
- **기존 파일과 공존**: 채널 폴더 구조가 `uploader/`에서
  `uploader/videos/`로 바뀌지만, 예전 파일은 DB 완료 기록으로 인식돼
  다시 받지 않습니다.

## 4. 전체 그림에서 뭘 추가·수정했나

| 구분 | 위치 | 내용 |
|---|---|---|
| 추가 | `classify_type()` | 탭 > URL > 길이 순 판별 |
| 수정 | `normalize_channel_url(url, tab)` | 탭 인자 추가 (videos/shorts) |
| 수정 | `run_channel()` | 탭별 수집·합병·태깅. `video_type` 인자 |
| 수정 | `process_entries()` | 종류 필터 + 제외 개수 로그. `[long/shorts]` 표시 |
| 수정 | `build_sub_outtmpl()` | 채널 종류별 하위 폴더 |
| 수정 | `add_header_to_subtitles()` | `Video-ID:` 행 추가 |
| 수정 | `subtitle_db.py` | `video_type`·`video_id` 컬럼 + 마이그레이션, 기록 함수 인자 |
| 수정 | `run_single/run_playlist/재시도` | 종류 전달 (개별은 기록용) |
| 추가 | `ask_video_type()` | 종류 질문 (기본 long) |
| 추가 | 설정 | `shorts_max_seconds` (기본 60) |
| 수정 | `download_subs_for_video()` | `langs=None` 방어 (직접 호출 대비, 기존 잠재 버그) |
| 머지 | `75d89c3` | `p5-1-shorts-type` → main, 충돌 없이 자동 머지 |

## 5. 검증

- 모의 테스트 7종 PASS: 판별 8케이스 / URL 정규화 / 저장 경로 /
  DB 컬럼·마이그레이션 / 헤더 Video-ID / 탭 수집(long 1회·both 2회+태그) /
  종류 필터·길이 추정 / 개별 기록
- 테스트 중 테스트 기대값 오류 2건 수정 (헤더 줄 수, 탭 우선 설계 반영)
- 머지 후 회귀 4종 PASS: P3-1·제목언어·P2-2 (+P1-1·저장위치는 기존 확인분)
- `py_compile` 정상, Gitleaks 통과, `314f91b..75d89c3 main -> main` 푸시 확인
