# 설명서: p5-9 작업 + 머지, 전체 CLI (P5-9)

- 브랜치: `p5-9-full-cli` → `main`에 머지 / 머지 커밋: `dfcb905` / 일시: 2026-09-12 19:28 (KST)
- 저장소: youtube-subtitles-downloader
- 한 줄 요약: 대화형 질문 전부를 명령줄 플래그로. 스킬(P6-1)의 전제 작업. 충돌 없이 자동 머지.

---

## 1. 앱 전체 구조 (지도)

```
대화형: 메뉴 → 질문 → 실행 (그대로)
무질문: 플래그 → 파서 → 같은 실행 함수 (신규)
  --channel/--playlist/--single/--list/--missing (+ 기존 --retry/--scan/--dashboard)
  매핑: --langs/--no-auto/--format/--encoding/--out/--type
        --date/--dur/--range/--keyword/--status/--order/--select/--yes/--json
```

## 2. 사용 흐름 (이번 커밋 기준)

```powershell
python yt_dlp_subtitle_downloader.py --channel "URL" --type both --date ">= 2024-01-01" --json
python yt_dlp_subtitle_downloader.py --single "URL1" "URL2" --langs ko --out "D:\subs"
python yt_dlp_subtitle_downloader.py --list "URL" --status missing --json
python yt_dlp_subtitle_downloader.py --missing "URL" --order desc --select "1-10" --yes --json
```

플래그 값은 대화형 입력과 같은 문자열 (`>= 2024-01-01`, `5-30`, `1-50`).
`--json`이면 마지막에 `SUMMARY_JSON: {...}` 1줄 추가 출력.

## 3. 동작 원리 (쉽게)

- **문제**: 스킬·스케줄러가 부르려면 질문 없이 실행돼야 하는데,
  모드 1~3·8·9는 사람 손을 탔습니다.
- **해결**: 질문 파싱 부분만 순수 함수로 뽑아냈습니다
  (`parse_langs/date/dur/range/...`). 대화형 질문과 CLI 플래그가
  같은 함수를 쓰니 동작이 절대 안 어긋납니다.
- **실행 공유**: `--missing`은 대화형과 같은 실행 본체
  (`_run_missing_targets`)를 씁니다. `--yes` 없으면 시작 안 함(안전).
- **`--json`**: 사람이 보는 표는 그대로 두고, 기계용 요약 1줄을
  맨 뒤에 덧붙입니다. `finalize()`가 요약 dict를 돌려주게 바꿨습니다.

## 4. 전체 그림에서 뭘 추가·수정했나

| 구분 | 위치 | 내용 |
|---|---|---|
| 추가 | 파서 7종 | `parse_langs/auto/sub_format/video_type/date/dur/range` |
| 수정 | `ask_*` 4종 | 파서 재사용으로 축소 (동작 동일) |
| 추가 | 플래그 19종 | 모드·공통·목록·미수신·출력 플래그 |
| 추가 | `run_headless_flow/list/missing/batch` | 모드별 무질문 실행기 |
| 수정 | `finalize()` | 요약 dict 반환. `_print_json_summary()` + `JSON_MODE` |
| 수정 | `main()` | 신규 플래그 분기. `--missing`은 `--yes` 필수 |
| 머지 | `dfcb905` | `p5-9-full-cli` → main, 충돌 없이 자동 머지 |

## 5. 검증

- 모의 테스트 7종 PASS: 파서 15케이스 / 채널 종단(필터 전달·--out) /
  재생목록·개별 묶음 / 목록+JSON 파싱 / --yes 게이트 / 대시보드 무JSON /
  오입력 폴백
- 테스트 중 테스트 버그 3건 수정 (튜플 인덱스·설정 덮어씀·호출 규격).
  코드 버그 0건.
- 머지 후 회귀 5종 PASS: P5-8·P5-6·P5-7·제목언어 (+P5-5·P5-4·P5-3·P5-2·P3-1은 기존 확인분)
- `py_compile` 정상, Gitleaks 통과, `f4becd4..dfcb905 main -> main` 푸시 확인
