# 출처 추적 원칙

검색·수집된 모든 자료는 **내부적으로 출처(URL / 공시 rcept_no / DB 출처 등)를 남겨야 한다.**
보고서 본문에 노출하지 않더라도 사후 추적이 가능해야 한다. 출처 없는 단정은 금지.

## 현재 한계

현 파이프라인은 종합된 최종 마크다운만 디스크에 저장하고, 다음은 휘발된다.

- 5개 서브에이전트(valuation/industry/price_flow/catalyst/risk) 각각의 본문 텍스트
- 서브에이전트가 호출한 WebSearch/WebFetch 결과 (SDK가 자동 처리, `agents.py`는 TextBlock만 수집)
- `data_loader.load_context()`가 받아온 DART 공시 원본 리스트(rcept_no 포함)
- 주목 공시의 signal_level/signal_category 라벨링 근거

→ 보고서 한 문장의 출처를 사후 검증할 방법이 없다.

## 개선 제안 (TODO)

- 각 서브에이전트 본문 텍스트를 영구 저장
  (DB 별도 컬럼 또는 `analysis/auto/<주차>/sub/<종목>_<sub_name>.md`)
- `data_loader`가 받은 raw DART 공시 리스트를 run_id+ticker별 JSON 스냅샷으로 저장
- 서브에이전트의 도구 콜·결과(ToolUseBlock/ToolResultBlock)도 수집

본문에 출처를 인라인으로 전부 노출할 필요는 없지만, 별도 부록·로그·DB 레코드에 반드시 남긴다.

관련: [이벤트 시간/시제 룰](event-time-rule.md)
