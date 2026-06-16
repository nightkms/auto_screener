# Auto Screener

**매시각 정각(KST)** 마다 코스피/코스닥에서 핫 종목 5개를 자동 선정해 큐에 넣고, 워커가 1종목씩 **Claude 멀티 서브에이전트**(5명 병렬)로 분석한 뒤, **강한 신호(STRONG) 종목**을 텔레그램으로 푸시합니다.

핵심 특징:
- **Claude Max 구독 한도 안에서 동작** — 별도 API 키 불요 (Claude Agent SDK 사용)
- 한 번 띄워두면 매시각 자동 선정·분석·알림. PC만 켜져 있으면 됨 (꺼졌다 켜지면 부팅 시 큐 자동 복구)
- 보고서·후보 이력·토큰 사용량은 SQLite + Markdown으로 저장

## 폴더 구조

```
auto_screener/
├── .env                    DART 키, 텔레그램 토큰
├── requirements.txt
├── config.py               설정 로더
├── selector.py             종목 선정 (네이버 모바일 API)
├── data_loader.py          DART 재무·공시 수집
├── agents.py               Claude 5개 서브에이전트 (Agent SDK 병렬)
├── synthesizer.py          종합 + 등급 판정 (STRONG/WATCH/INTEREST/SKIP)
├── notifier.py             텔레그램 푸시
├── storage.py              SQLite (실행·보고서·토큰)
├── pipeline.py             전체 오케스트레이션
├── dashboard.py            FastAPI 대시보드 (localhost:8765)
├── scheduler.py            상주 entry point (대시보드 + APScheduler)
├── prompts/                서브에이전트 시스템 프롬프트 5개 + synthesizer
├── templates/              대시보드 HTML (Jinja2)
└── data/screener.db        SQLite (자동 생성)
```

## 첫 설치 (이미 완료된 상태)

```powershell
cd path\to\auto_screener
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 비밀값 유출 방지 pre-commit 훅 활성화 (클론 후 1회)
git config core.hooksPath .githooks
```

### 비밀값 차단 훅

`.githooks/pre-commit` 가 커밋 직전 staged 내용을 스캔해, 비밀값·개인정보가 섞이면
커밋을 거부합니다 (`scripts/check_secrets.py`). 탐지 대상:

- 로컬 `.env` 의 실제 값이 코드에 그대로 들어간 경우 (하드블록)
- 토큰/프라이빗키/AWS·GitHub·Slack 키, 일반 `secret=...` 할당
- 이메일·Windows 사용자 경로 등 개인정보
- 금지 파일명(`.env`, `*.pem`, `*.key`, `id_rsa` …)

오탐이면 해당 라인 끝에 `# pragma: allowlist secret` 를 붙이면 통과합니다
(단 `.env` 실값/금지 파일명은 pragma 로도 통과 불가).

## 필요한 자격 증명

`.env` 파일에서:

| 키 | 용도 | 어디서 |
|---|---|---|
| `DART_API_KEY` | DART 재무·공시 | https://opendart.fss.or.kr |
| `TELEGRAM_BOT_TOKEN` | 봇 메시지 전송 | BotFather (`/newbot`) |
| `TELEGRAM_CHAT_ID` | 본인 채팅 ID | 봇과 대화 시작 후 `getUpdates` |

**Claude는 별도 API 키 불요** — Claude Code가 로그인된 환경이면 자동 사용.

### 텔레그램 봇 만들기 (5분)
1. 텔레그램에서 `@BotFather` 검색 → `/newbot` → 봇 이름 → 토큰 발급
2. 새 봇과 대화 시작 → 아무 메시지 전송
3. `https://api.telegram.org/bot<TOKEN>/getUpdates` 접속 → `chat.id` 확인
4. `.env`의 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` 입력
5. 검증: `python notifier.py test` → "✅ AutoScreener 봇 연결 테스트 성공" 수신

## 실행

**상주 모드 (스케줄러 + 대시보드)**
```powershell
.\.venv\Scripts\Activate.ps1
python scheduler.py
```
- 대시보드: http://localhost:8765
- 매시각 정각(:00) 자동 실행 — failed 큐 복귀 → 핫 종목 5개 enqueue → 워커가 순차 분석

**1회만 즉시**
```powershell
python pipeline.py              # 표준 (top 5)
python pipeline.py --top 3      # 후보 3개
python pipeline.py --dry-run    # 알림 끄고 테스트
```

**개별 모듈 단독 테스트**
```powershell
python selector.py              # 핫 종목 top 5 출력
python data_loader.py 005930    # 삼성전자 DART 정보
python agents.py 260970         # 에스앤디 5 서브에이전트 분석
python synthesizer.py 260970    # 위 + 종합 등급
python notifier.py test         # 텔레그램 봇 핑
python notifier.py last         # 최근 실행 결과 다시 푸시
```

## 운영 모델

| 모듈 | Claude 모델 | 이유 |
|---|---|---|
| 5 서브에이전트 (밸류/산업/수급/카탈/리스크) | `claude-sonnet-4-6` | 속도·Max 한도 |
| 종합 (synthesizer) | `claude-opus-4-7` | 최종 판단 품질 |

회당 6개 LLM 호출 × 후보 5개 = **30 호출**. Sonnet 위주라 Max 플랜으로 동작하나, 매시각 도는 만큼 일일 한도 도달 시 다음 리셋까지 차단될 수 있음(부팅·정각마다 큐 자동 복구).

### Claude 인증 & 설정 디렉토리 격리

LLM 호출은 `claude_agent_sdk`가 띄우는 **`claude` CLI 서브프로세스**로 동작한다(별도 API 키 불필요, 로그인된 Claude Max/Pro 구독 한도 사용). 한 종목당 5개 서브에이전트 + 공시 요약/종합이 **동시에** 떠서 각자 `claude`를 실행한다.

이때 기본값인 홈의 `~/.claude.json`(계정·상태 파일)을 여러 프로세스가 동시에 read-modify-write 하면 **경합으로 깨질 수 있다** — 특히 같은 PC에서 대화형 Claude Code 세션을 같이 켜둔 경우. 이를 막기 위해 스크리너는 **프로젝트 전용 설정 디렉토리(`.claude_config/`)로 자동 격리**한다:

- 첫 실행 시 홈의 로그인/설정(`~/.claude.json`, `~/.claude/.credentials.json`, `~/.claude/settings.json`)을 `.claude_config/`로 **1회 자동 시드** → 별도 재로그인 불필요.
- `.claude_config/`는 OAuth 토큰을 담으므로 **`.gitignore`로 추적 제외**(절대 커밋 금지).
- 경로 변경/비활성화는 `.env`의 `SCREENER_CLAUDE_CONFIG_DIR`로:
  - 미설정(기본) → `.claude_config/`로 격리(권장)
  - `=off` (또는 `none`/`0`) → 격리 끄고 홈 `~/.claude.json` 그대로 사용
  - `=<경로>` → 지정 경로로 격리

> 격리 디렉토리에서 인증이 안 풀리면(예: 홈에 로그인이 없던 상태) 해당 디렉토리를 `CLAUDE_CONFIG_DIR`로 지정한 채 `claude` 로그인을 1회 하면 된다.

## 신호 등급

| 등급 | 기준 (synthesizer가 판정, fallback rule 있음) | 자동 실행 알림 |
|---|---|---|
| 🟢 STRONG | 평균 ★ ≥ 4.0 AND 리스크 ★ ≥ 3.5 AND 단기 트리거 존재 | 텔레그램 푸시 |
| 🟡 WATCH | 평균 ★ ≥ 3.5 AND 리스크 ★ ≥ 3.0 | 보고서 저장 + 주가 watch 등록 (푸시 X) |
| ⚪ INTEREST | 평균 ★ ≥ 3.0 | 보고서만 저장 |
| ⚫ SKIP | 그 외 | 인덱스 기록 |

> 알림 정책은 **소스별로 다름**: 자동 스크리닝(`auto_hourly`)은 **STRONG만** 푸시(스팸 방지). 사용자가 직접 지정한 종목(`manual`/`telegram`)은 등급과 무관하게 항상 푸시.

## Windows 부팅 시 자동 시작 (선택)

작업 스케줄러 등록 — 관리자 PowerShell:
```powershell
schtasks /create /tn "AutoScreener" `
  /tr "<repo>\auto_screener\.venv\Scripts\python.exe <repo>\auto_screener\scheduler.py" `
  /sc onstart /ru "%USERNAME%"
```
삭제: `schtasks /delete /tn "AutoScreener" /f`

## 데이터 흐름

```
[매시각 정각(:00) 트리거]  ── failed 큐 복귀 → 핫 종목 enqueue → 워커 순차 처리
        │
        ▼
selector.py   ◄── 네이버 모바일 API (시총 상위 200 → 핫 점수 → top 5)
        │
        ▼
data_loader.py ◄── DART OpenAPI (회사·재무·최근 90일 공시)
        │
        ▼
agents.py     ◄── Claude Sonnet 4.6 × 5 (병렬, WebSearch 허용)
        │
        ▼
synthesizer.py ◄── Claude Opus 4.7 (종합 + 등급)
        │
        ├──► storage.py (SQLite + analysis/auto/<주차>/*.md)
        └──► notifier.py (텔레그램, STRONG/WATCH만)
```

## 한계 / 알려진 이슈

- 네이버 모바일 API가 비공식이라 변경 가능 (selector.py 수정 여지)
- DART 단일계정 API에서 일부 종목 당기순이익 누락 → 향후 fnlttSinglAcntAll 전체 재무제표로 보강 예정
- Claude Max 일일·주간 한도 도달 시 다음 리셋까지 차단됨
- 비영업일(주말·공휴일) 데이터 빈 가능성 → pipeline 실패 처리되어 다음 정각에 재시도
