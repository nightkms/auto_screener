# CLAUDE.md

이 파일은 Claude Code(및 개발자)가 **auto_screener 레포 안에서만** 작업할 때 필요한
맥락을 담는다. 이 폴더를 어디에 클론하든 여기 적힌 대로 동작하는 것이 목표다.

## 프로젝트 개요

한국 코스피/코스닥 종목을 **매시각 정각**에 자동 선정·분석하고, 강한 신호(STRONG)만
텔레그램으로 푸시하는 스크리너. 분석은 **Claude 멀티 서브에이전트**(5명 병렬)로 수행한다.

- LLM 호출은 `claude_agent_sdk`(Claude Code CLI 서브프로세스)를 통하며 **Claude Max 구독
  한도 안에서** 동작한다 — 별도 Anthropic API 키 불필요.
- 외부 데이터: DART OpenAPI(재무·공시), 네이버 모바일/검색 페이지(시세·핫 종목).
- 저장: SQLite(`data/screener.db`) + Markdown 보고서.

사용자 대상 사용법·설치는 [README.md](README.md), 설계 근거는 [docs/design/](docs/design/) 참고.

## 메모리 시스템 (자동 로드)

@memory/MEMORY.md

`memory/`는 포터블 컨텍스트다. 위 인덱스의 각 파일을 필요 시 Read 한다. 특히:

- `memory/feedback_*.md` ← 작업 방식·응답 스타일·정책 (특히 [push 정책](memory/feedback_push_policy.md): push는 사용자 명시 요청 시에만)
- `memory/howto_dart_document.md` ← DART 공시 원문(document API) 디코딩 노하우
- `memory/project_auto_screener_github.md` ← 이 repo 발행 규칙

주의: `memory/`는 개인 성향·피드백을 담으므로 **`.gitignore`로 제외**된다 (public repo 비노출).

## 빠른 시작

```sh
python -m venv .venv
# Windows: .\.venv\Scripts\Activate.ps1   |   macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # 키 채우기 (DART_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
git config core.hooksPath .githooks   # 비밀값 차단 훅 활성화 (클론 후 1회, 필수)

python scheduler.py           # 상주: 대시보드(127.0.0.1:8765) + 매시각 자동 실행
```

`.env`가 없거나 필수 키가 비면 `config._req`/`require_telegram`이 명확한 에러를 던진다.

## 아키텍처 / 데이터 흐름

```
[매시각 정각 트리거] (scheduler.py)
   reset_failed_to_pending → enqueue_event_picks(auto_event) → enqueue_hot_picks(auto_hourly)
        │                            │
        │                            └─ event_watch.py  최근 30일 분석 종목 중 오늘 급변한
        │                               종목을 dedup 무시하고 재투입 (priority=1로 먼저 처리)
        ▼
selector.py     검색상위 → 상한가·거래량 급증 → 부족분 시총 z-score → top 5  (0건 허용)
        │
        ▼
data_loader.py  DART OpenAPI: 회사·재무·최근 공시 + 네이버 시세
        │
        ▼
agents.py       Claude Sonnet × 5 병렬 (valuation/industry/price_flow/catalyst/risk)
        │       각 서브에이전트는 prompts/<name>.txt 시스템 프롬프트 + WebSearch 허용
        ▼
synthesizer.py  Claude Opus: 5개 보고 종합 + 등급(STRONG/WATCH/INTEREST/SKIP)
        │
        ├──► storage.py     SQLite + analysis/auto/<주차>/*.md
        └──► notifier.py    텔레그램 2단계 푸시(요약 → 상세)
                            (auto_hourly = STRONG 또는 1~2년 실적변화 이벤트, SKIP 제외)
                            (auto_event  = 등급 무관 전송 — 급변 원인 자체가 알림 가치)
```

큐 워커·가격 알림 워커는 `dashboard.py`에 있고 `scheduler.lifespan`이 백그라운드 태스크로 띄운다.

## 모듈 책임

| 파일 | 역할 |
|---|---|
| `scheduler.py` | 상주 entry point. FastAPI(uvicorn) + APScheduler hourly 잡 + 부팅 복구 |
| `dashboard.py` | FastAPI 라우트, 큐 워커, 가격감시 워커, 보고서 Q&A 엔드포인트 |
| `config.py` | `.env` 로더·검증. 모든 설정·경로의 단일 출처 |
| `selector.py` | hot pick 선정 (3단계, [정책](docs/design/selector.md)) |
| `event_watch.py` | 이벤트 재분석 트리거. 최근 분석 종목 중 오늘 급변한 종목을 30일 dedup 무시하고 재투입 |
| `data_loader.py` | DART/네이버 수집, 분석 컨텍스트 패킹 |
| `agents.py` | 5개 서브에이전트 병렬 실행 (`MAX_TURNS_PER_AGENT`, sub별 timeout) |
| `synthesizer.py` | 종합·등급 판정 (fallback rule 포함) |
| `pipeline.py` | 전체 오케스트레이션 (`run_once`, `enqueue_hot_picks`) |
| `notifier.py` | 텔레그램 전송 (소스별 알림 정책, 연속 실패/복구 알림) |
| `report_chat.py` | 대시보드 보고서 Q&A (Sonnet + WebSearch/WebFetch) |
| `ticker_archive.py` | 종목별 직전 요약(prior_summary) 누적 |
| `storage.py` | SQLite 스키마·쿼리·복구 함수 |

## 실행 & 디버그

```sh
python scheduler.py             # 상주(대시보드+스케줄러)
python pipeline.py              # 1회 즉시 (top 5)
python pipeline.py --dry-run    # 알림 끄고 테스트
python selector.py              # 핫 종목 top 5만 출력
python event_watch.py           # 오늘 급변한 '이미 분석한 종목' 감지만 (큐 미투입)
python event_watch.py --enqueue # 위 + 큐 투입
python data_loader.py 005930    # 특정 종목 DART 컨텍스트
python agents.py 260970         # 5 서브에이전트만
python synthesizer.py 260970    # 위 + 종합 등급
python notifier.py test         # 텔레그램 봇 핑
python report_chat.py <id> "질문"  # 보고서 Q&A 단독 테스트
```

Windows 상주 백그라운드 기동은 `start_screener.bat`(→ `start_helper.vbs` + `_get_pid.ps1`),
종료는 `stop_screener.bat`. macOS/Linux에선 `python scheduler.py`를 직접 띄운다.

keepalive로 **못 막는** 만료도 있다: Claude Code는 토큰 유효기간과 별개로 주기적(체감 ~1주)으로
대화형 재로그인을 요구한다. 이때 `credentials.json`의 만료 시각은 멀쩡해서 scheduler의 만료
점검(`_check_credentials_and_alert`)에 안 걸리고, 서브에이전트만 `error result: success`로 전부
즉시 실패한다 → 사람이 `claude`에서 `/login` 할 때까지 자동 복구 불가(2026-08-22 사고).
그래서 `dashboard.queue_worker`가 원인과 무관하게 **연속 실패 스트릭**을 세고
(`FAIL_ALERT_COUNT`건 AND `FAIL_ALERT_HOURS`시간 지속) 텔레그램으로 알린다 — 사람을 부르는 알림이다.

## 개발 규칙

### LLM / 모델
- 모델은 `config.CLAUDE_SUB_MODEL`(Sonnet), `config.CLAUDE_SYNTH_MODEL`(Opus)로만 참조. 하드코딩 금지.
- 서브에이전트 턴 상한은 `agents.MAX_TURNS_PER_AGENT`, 보고서 Q&A는 `report_chat.MAX_TURNS`.
  검색이 많은 작업이 한도에서 끊기면 상한을 올리고, 끊겨도 부분 결과를 살리는 폴백을 둔다.

### 프롬프트 (`prompts/`)
- 분석 룰을 바꿀 때는 코드가 아니라 해당 `prompts/*.txt`를 수정한다.
- 모든 이벤트는 **시점·상태·가격반영** 3필드 + 시간 윈도우 룰을 지킨다.
  키워드만으로 옛 사건과 자동 매칭 금지 → [event-time-rule](docs/design/event-time-rule.md).
- 수집 자료는 출처를 남긴다 → [source-tracking](docs/design/source-tracking.md).

### selector
- 검색상위 우선 → 상한가·거래량 급증 보강 → 부족분만 시총 z-score(="일반") 보강, dedup 유지,
  **0건은 정상**. 윈도우(30일)를 임의로 줄이지 말 것 → [selector](docs/design/selector.md).

### event_watch (이벤트 재분석)
- selector와 **반대 방향**이다: selector는 안 본 종목을 찾고(dedup 적용), event_watch는
  이미 본 종목 중 오늘 급변한 것을 찾는다(dedup 무시).
- 트리거는 **당일성 지표만** — 당일 등락률 +15%(급등), 52주 신고가. 거래량 배수는 쓰지 않는다.
  급락은 기본 off(`EVENT_PLUNGE=0`) — 토큰은 급등과 같이 쓰는데 그날 되짚을 실익이 적다(2026-09-06).
  '분석일 대비 누적 이탈'·'52주 신저가'는 실측 후 제외(전자는 당일성 없음, 후자는 하락장에
  하루 40~86건 동반 발생). 임계는 `config.EVENT_*`로만 조정하고 코드에 하드코딩 금지.
- **쿨다운·하루 상한 없음**(사용자 룰). 중복 제거는 두 가지뿐:
  ① `event_trigger` UNIQUE(ticker, event_date, kind) — 같은 날 같은 종류 1회
  ② 오늘 이미 분석된 종목은 오늘 이벤트로 재소환하지 않음 — 없으면 selector가 상한가로
     뽑아 분석한 종목을 event_watch가 즉시 되잡는 자기참조 루프가 생긴다.
  둘 다 날짜가 바뀌면 풀린다 → **이틀 연속 급등이면 이틀 다 분석한다.**
- 큐 `priority=1`로 정기 핫픽을 앞지른다. "오늘 왜 튀었나"는 그날 답이 나와야 의미가 있다.

### 공시 수집
- 제목만으론 호재/악재가 안 드러나는 공시(지분·주요사항 등)는 본문 원문을 받아 요약해
  컨텍스트에 주입한다. 목록 증분 수집 + 요약 캐시로 비용을 줄인다.
  document API(ZIP/XML)·ACODE 필드·대상 판정 → [dart-document](docs/design/dart-document.md).

### 분석 산출물 원칙
- 한국어. 재무 수치는 백만원 단위 표.
- **매수/매도/관망 권유 금지** — 객관 데이터와 변동 원인만.
- 분기 손익 비교 시 효율법인세율·일회성 항목 등 회계적 noise는 명시 구분.
- 종목 코드는 추측 말고 확인.

### 런타임 가정
- 24/7 아닌 호스트 전제 → 고정 시각 cron 금지, hourly + 부팅 복구.
  hang 진단 시 sleep/wake를 먼저 의심 → [runtime-assumptions](docs/design/runtime-assumptions.md).

## 보안 (반드시 준수)

- 자격증명은 **전부 `.env`** 에만. 코드는 `config`/`os.getenv`로 읽고 키를 하드코딩하지 않는다.
- `.githooks/pre-commit`(+`scripts/check_secrets.py`)이 커밋 직전 비밀값·개인정보를 차단한다.
  클론 후 `git config core.hooksPath .githooks`로 1회 활성화. 상세 → [SECURITY.md](docs/SECURITY.md).
- 커밋 전 `git status`로 `.env`·`data/`·`logs/`가 staged에 없는지 항상 확인.

## 코드 컨벤션

- 주석·로그·문서는 한국어. 기존 파일의 톤·밀도를 따른다.
- 새 설정은 `config.py`에 `_req`(필수)/`_opt`(기본값) 헬퍼로 추가.
- 경로는 `config.DATA_DIR`/`ANALYSIS_DIR` 등 중앙 상수를 쓰고 절대경로 하드코딩 금지.
- 외부 호출(DART/네이버/텔레그램/SDK)은 timeout과 예외 처리를 둔다.

## 알려진 함정

- `scheduler.py`는 최상단에서 `import _silence_console`(Windows 콘솔 억제, 비Windows는 no-op).
  이 파일은 추적 대상이며 **삭제·ignore 금지** — 없으면 import 에러.
- 네이버 검색/시세는 **비공식** 엔드포인트라 변경될 수 있다(`selector.py`/`data_loader.py` 조정 여지).
- DART 단일계정 API는 일부 종목 당기순이익이 누락될 수 있다.
- Windows에서 `pythonw` + VBS redirect 조합이 sleep/wake 후 로그를 0바이트로 남기는 경우가 있다.
- `data/`·`logs/`·`.venv/`·`.env`는 gitignore 대상. 클론 직후엔 비어 있고 첫 실행 시 생성된다.
- `*.bat`/`*.vbs`/`*.ps1`이 **LF(Unix) 줄바꿈**으로 저장되면 `cmd.exe`가 `.bat` 라인을 잘못 끊어
  `'ta'`/`'er.pid'`/`'D' is not recognized` 류 에러로 기동이 실패한다(`start_screener.bat` 먹통).
  `.gitattributes`가 `eol=crlf`로 강제하지만, 에디터가 LF로 덮어쓰면 재발 → 증상 보이면 줄바꿈부터 확인.
