# DMF Intelligence MCP Server

의약품안전나라(nedrug.mfds.go.kr)의 DMF(Drug Master File) 등록 데이터를 AI로 조회·분석하는 MCP 서버입니다.

## 제공 도구

| 도구 | 설명 |
|------|------|
| `get_weekly_dmf` | 주간 DMF 등록 현황 조회 |
| `get_monthly_dmf_summary` | 월간 DMF 등록 분석 (국가별, 신청인별, 경쟁 성분) |
| `search_dmf_by_ingredient` | 특정 성분 DMF 등록 이력 검색 |
| `search_dmf_by_country` | 특정 국가 DMF 등록 현황 검색 |
| `get_dmf_chat_summary` | 메신저 공유용 간결한 요약 생성 |

## 사용 예시

AI 대화에서:
- "이번 주 신규 DMF 등록 현황 알려줘"
- "인도 제조사 DMF 현황 보여줘"
- "amoxicillin DMF 등록 내역 검색해줘"
- "카톡에 공유할 수 있게 DMF 주간 요약 만들어줘"

## 설치 및 실행

```bash
pip install -r requirements.txt
python server.py
```

## 데이터 출처

의약품안전나라 DMF 등심사결과 공개 (https://nedrug.mfds.go.kr)
