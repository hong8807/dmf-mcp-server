# DMF Intelligence Server

의약품안전나라 DMF(Drug Master File) 등록 현황을 실시간으로 조회·분석하는 통합 서버입니다.

## 주요 기능

| 기능 | 설명 |
|------|------|
| 주간 DMF 현황 | 신규 등록 DMF 건수, 최초/허여, 연계심사 분석 |
| 월간 DMF 리포트 | 전월 대비 변동률, 국가별 분포, 주요 신청인 |
| 성분명 검색 | 특정 성분의 전체 DMF 등록 이력 |
| 국가별 검색 | 특정 국가 제조사의 DMF 현황 |
| 채팅 공유용 요약 | 카카오톡에 바로 붙여넣기 가능한 요약 |

## 서버 모드

### 1. 카카오톡 채널 챗봇 (기본)
카카오 i 오픈빌더 Skill 웹훅으로 동작합니다.
동료들이 카카오톡 채널에서 직접 DMF 현황을 조회할 수 있습니다.

```
환경변수: SERVER_MODE=kakao
```

**웹훅 엔드포인트:**
| 엔드포인트 | 용도 |
|-----------|------|
| POST /kakao/skill | 통합 스킬 (발화 자동 분석) |
| POST /kakao/weekly | 주간 현황 전용 |
| POST /kakao/monthly | 월간 리포트 전용 |
| POST /kakao/summary | 채팅 요약 전용 |
| POST /kakao/ingredient | 성분 검색 전용 |
| POST /kakao/country | 국가 검색 전용 |

**사용 예시 (카카오톡 채널 채팅):**
- `주간` → 주간 DMF 등록 현황
- `월간` → 월간 DMF 리포트
- `인도` → 인도 DMF 현황
- `amoxicillin` → 성분명 검색
- `요약` → 채팅 공유용 요약

### 2. MCP 서버
Claude Desktop / PlayMCP에서 AI 도구로 사용합니다.

```
환경변수: SERVER_MODE=mcp
```

**MCP 도구:**
- `get_weekly_dmf` — 주간 DMF 현황
- `get_monthly_dmf_summary` — 월간 DMF 요약
- `search_dmf_by_ingredient` — 성분명 검색
- `search_dmf_by_country` — 국가별 검색
- `get_dmf_chat_summary` — 채팅 공유용 요약

## 배포

Render.com (Free tier)으로 배포합니다.

```
Runtime: Docker
Region: Singapore
환경변수: SERVER_MODE=kakao, PORT=8000
```

## 데이터 출처

의약품안전나라 (nedrug.mfds.go.kr) DMF 심사결과 공개 데이터

## 기술 스택

- Python 3.11
- FastAPI (카카오 웹훅)
- FastMCP (MCP 서버)
- Pandas (데이터 분석)
