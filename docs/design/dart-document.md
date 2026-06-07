# DART 공시 본문(원문) 수집·요약

## 왜 본문까지 받는가

auto_screener는 원래 공시를 **제목(`report_nm`)만** 보고 키워드로 분류했다.
문제는 제목만으로는 호재/악재가 드러나지 않는 공시가 많다는 것이다.

대표 패턴: 대주주 지분 변동 공시의 제목은 보통 `"주식등의 대량보유상황보고서(일반)"`
처럼 중립적이라 **증여·담보·승계 같은 핵심이 한 글자도 안 보인다.** 본문에 가서야
`"주식담보계약 및 주식증여계약 체결"`(승계 + 자금조달) 같은 사실이 나온다.
제목만 보면 막연히 악재로 기울거나 그냥 지나치게 된다.

→ 제목만으로 판단이 안 서는 유형은 **원문 본문을 받아 LLM으로 2~4문장 요약**해서
분석 컨텍스트에 주입한다.

## DART document API 디코딩 노하우

- 엔드포인트: `GET https://opendart.fss.or.kr/api/document.xml?crtfc_key=<KEY>&rcept_no=<접수번호>`
- 응답은 **ZIP**이다 (content-type이 `application/x-msdownload`로 와도 내용은 ZIP).
  안에 `{rcept_no}.xml` 파일 1개, 인코딩은 **UTF-8**.
- 마크업은 DART 전용 스키마(`dart4.xsd`): `TABLE/TR/TD/TE/TU/P …` 태그.
  태그를 제거하면 원본 50~110KB가 보통 **4~9천 자**로 줄어(약 90% 감소) LLM 요약
  입력으로 적합하다. 셀/행 경계(`</TR>`, `</TD>` 등)만 공백·개행으로 남기고 나머지
  태그는 지운다 → `data_loader._clean_dart_xml`.
- **핵심필드는 `ACODE` 속성으로 직접 추출**할 수 있다(요약 보조 + LLM 실패 시 fallback):

  | ACODE | 의미 |
  |---|---|
  | `RPT_RSP_NM` | 보고자 |
  | `SUM_CHN_RWN` | 보고사유 |
  | `CHN_RSM` | 변경사유 |
  | `CHN_RSN` | 변동사유 |
  | `TRD_RVL` | 계약상대방 |
  | `TRD_KND` | 계약종류 |
  | `TRD_RMK` | 계약비고 |

  정규식 `ACODE="<code>"[^>]*>([^<]*)<` 로 뽑는다 → `data_loader._extract_dart_fields`.

## 함께 잡은 버그: 공시 유형이 전부 "기타"로 박히던 문제

`list.json`(공시 **목록** API)은 **각 row에 `pblntf_ty`(공시 유형)를 주지 않는다.**
기존 코드가 `r.get("pblntf_ty")`를 읽다 보니 모든 공시 type이 `"기타"`로 떨어졌다.

해결: 목록을 받을 때 어느 카테고리 필터(`None`/`B`/`C`/`D`/`I`)로 조회했는지(루프
변수)로 유형을 판정한다. `None`(전체) 조회분은 일단 `"기타"`로 두고, 이후 B/C/D/I
조회에서 같은 `rcept_no`를 다시 만나면 정확한 유형으로 보강한다.
참고로 대량보유·소유상황보고서는 **D(지분)**.

## 어떤 공시의 본문을 받는가 (`should_fetch_document`)

본문 fetch + LLM 요약은 비용이 있으므로 대상을 좁힌다:

- 유형이 **지분(D)** 또는 **주요사항(B)** — 제목만으론 호재/악재가 안 드러나는 대표군
- `signal_level`이 **fatal/warn** — 맥락 확인 가치가 있음
- 위 유형 판정이 누락돼도 **제목 패턴**으로 한 번 더 거른다
  (대량보유·소유상황·특정증권·최대주주·증자·전환사채·합병·분할·감자·자기주식 등)
- **정기보고서(사업/분기, A)는 제외** — 실적은 `fnlttSinglAcnt`로 이미 구조화
  수신하므로 방대한 본문을 다시 받을 필요가 없다

종목당 본문 fetch+요약은 `MAX_DOCS_PER_STOCK`(기본 8)건으로 상한.
공시는 signal/최신 우선 정렬돼 있으므로 중요한 것부터 채워진다.

## 증분 수집 + 캐시 (매번 전체를 다시 받지 않기)

목록은 가볍지만 본문 fetch와 LLM 요약은 비싸다. 그래서 **목록은 증분으로 받고,
요약 결과는 캐시**한다.

- `data_loader.collect_disclosures_cached(ticker, name, corp)`
  - `metadata.last_disclosure_scan_date` **그 날부터** 다시 조회한다(하루 겹침).
    오전에 조회한 뒤 같은 날 오후에 올라온 공시를 놓치지 않기 위함. 첫 방문이면
    `since=None` → 180일 전체.
  - DART 신규분 + jsonl 캐시(과거 전체)를 `rcept_no`로 머지하고 180일 이내로 컷.
    → 매번 전체를 다시 받지 않아도 분석 컨텍스트에는 180일 공시가 다 들어간다.
  - 캐시에 있던 `summary`(본문 요약)는 머지에서 **보존**한다.
- `agents.enrich_disclosures(ticker, name, disclosures)`
  - 캐시에 `summary`가 있는 `rcept_no`는 **LLM 재호출 없이 재사용**.
  - 미캐시 + `should_fetch_document` 대상만 본문 fetch + `config.CLAUDE_SUB_MODEL`로 요약.
  - 새로 요약한 것만 jsonl에 append(`rcept_no`당 사실상 1회성).
  - `analyze_stock`이 **5개 서브에이전트 가동 전에** 호출 → 각 서브에이전트 프롬프트에
    "제목 + 본문요약"이 함께 들어간다(중복 fetch 방지).
- 캐시 위치: `analysis/by_ticker/<ticker>_<name>/dart_disclosures.jsonl`
  (각 row에 `summary` 필드).

## 요약 프롬프트 원칙

본문 요약은 분석 산출물 규칙을 그대로 따른다:

- 공시일 **시점의 사실만** 적는다. 추측·전망·매수/매도 의견 금지.
- 누가/무엇을/얼마나(주식수·금액·지분율)/왜(사유)를 **구체 수치로**.
- 증여·담보·계약 건은 **상대방과 목적**(승계·자금조달 등)을 드러낸다.
- 본문에 없는 내용은 지어내지 않는다.

관련: [이벤트 시간/시제 룰](event-time-rule.md), [출처 추적 원칙](source-tracking.md)
