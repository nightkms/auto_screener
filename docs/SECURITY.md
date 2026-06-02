# 보안 / 비밀값 취급

## 비밀값은 전부 `.env`

자격증명(`DART_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`)은 `.env`에만 두고,
코드는 `config.py`가 환경변수로 읽는다. 하드코딩된 키는 없다.
`.env`는 `.gitignore`로 제외되며, 키 이름 템플릿은 `.env.example` 참고.

`.gitignore`가 제외하는 것: `.env`, `.venv/`, `__pycache__/`, `data/*.db`, `data/cache/`,
`data/screener.pid`, `logs/`, 그리고 로컬 전용 보조 스크립트(`_diag*.py`, `_get_pid.ps1`,
`_silence_console.py`).

## 커밋 전 비밀값 차단 훅

`.githooks/pre-commit`이 커밋 직전 staged 내용을 스캔해, 비밀값·개인정보가 섞이면
커밋을 거부한다(`scripts/check_secrets.py`).

활성화 (클론 후 1회):

```sh
git config core.hooksPath .githooks
```

탐지 대상:

- 로컬 `.env`의 **실제 값**이 코드에 그대로 들어간 경우 (하드블록)
- 토큰/프라이빗키/AWS·GitHub·Slack·Google 키, 일반 `secret=...` 할당
- 이메일·Windows 사용자 경로 등 개인정보
- 금지 파일명(`.env`, `*.pem`, `*.key`, `id_rsa` …)

오탐이면 해당 라인 끝에 `# pragma: allowlist secret`를 붙이면 통과한다
(단 `.env` 실값/금지 파일명은 pragma로도 통과 불가).

> 스캐너는 실제 비밀 문자열을 코드에 박지 않고, 로컬 `.env`에서 런타임에 읽어 대조한다.
> 따라서 스캐너 자체가 공개돼도 안전하다.

## 자격증명이 유출됐다면

`.env` 값이 실수로 커밋·push되면 즉시:

- 텔레그램 봇 토큰: BotFather에서 재발급
- DART 키: opendart.fss.or.kr에서 재생성

(git 히스토리에 한 번 들어가면 삭제해도 복구될 수 있으므로 재발급이 원칙.)
