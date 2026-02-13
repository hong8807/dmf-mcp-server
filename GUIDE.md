# DMF Intelligence MCP 서버 구현 & PlayMCP 등록 가이드

> Huxeed 소싱팀용 — 의약품안전나라 DMF 데이터를 AI로 조회·분석·공유하는 MCP 서버

---

## 전체 흐름 요약

```
[STEP 1] 로컬 개발 & 테스트       → 내 PC에서 MCP 서버 동작 확인
[STEP 2] GitHub에 코드 업로드      → 배포 준비
[STEP 3] Render.com에 배포         → 인터넷에서 접근 가능한 URL 생성
[STEP 4] PlayMCP에 서버 등록       → 카카오 플랫폼에 등록
[STEP 5] 사용하기                  → AI 채팅으로 DMF 조회 → 카톡에 공유
```

---

## STEP 1. 로컬 개발 & 테스트 (내 PC)

### 1-1. 사전 준비

PC에 이미 Python이 설치되어 있으므로 (dmf_weekly_report.py 구동 환경), 추가 설치만 하면 됩니다.

```bash
# 프로젝트 폴더 생성
mkdir dmf-mcp-server
cd dmf-mcp-server

# 필요한 패키지 설치
pip install "mcp[cli]" fastmcp requests pandas openpyxl uvicorn
```

### 1-2. server.py 파일 배치

첨부된 `server.py` 파일을 `dmf-mcp-server` 폴더에 넣으세요.

### 1-3. 로컬 테스트 실행

```bash
# 방법 1: MCP Inspector로 테스트 (브라우저에서 도구 테스트 가능)
mcp dev server.py

# 방법 2: SSE 서버로 직접 실행
python server.py
```

**MCP Inspector 테스트 방법:**
1. `mcp dev server.py` 실행하면 브라우저가 열림
2. 좌측에 5개 도구(tool)가 표시됨
3. `get_weekly_dmf` 클릭 → `weeks_ago: 1` 입력 → Run
4. 의약품안전나라에서 데이터를 받아와 결과가 JSON으로 표시되면 성공!

### 1-4. Claude Desktop에서 로컬 테스트 (선택)

Claude Desktop 설정 파일에 추가:
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "dmf-intelligence": {
      "command": "python",
      "args": ["C:\\Users\\홍성재\\Desktop\\PYTHON\\dmf-mcp-server\\server.py"]
    }
  }
}
```

Claude Desktop 재시작 후 "이번 주 DMF 현황 보여줘"라고 입력하면 동작합니다.

---

## STEP 2. GitHub에 코드 업로드

PlayMCP에 등록하려면 서버가 인터넷에 공개되어야 합니다.
먼저 코드를 GitHub에 올립니다.

### 2-1. GitHub 저장소 생성

1. https://github.com 접속 → 로그인
2. 우측 상단 `+` → `New repository`
3. 설정:
   - Repository name: `dmf-mcp-server`
   - Public 선택 (PlayMCP 심사 시 코드 확인 필요)
   - `Add a README file` 체크
4. `Create repository` 클릭

### 2-2. 파일 업로드

GitHub 웹에서 직접 업로드하는 가장 간단한 방법:

1. 생성된 저장소 페이지에서 `Add file` → `Upload files`
2. 아래 3개 파일을 드래그앤드롭:
   - `server.py`
   - `requirements.txt`
   - `render.yaml`
3. `Commit changes` 클릭

---

## STEP 3. Render.com에 무료 배포

Render.com은 GitHub 연결만으로 자동 배포되는 클라우드 서비스입니다.
무료 플랜으로 충분합니다.

### 3-1. Render 계정 생성

1. https://render.com 접속
2. `Sign Up` → `GitHub` 계정으로 가입

### 3-2. 새 Web Service 생성

1. Dashboard → `New` → `Web Service`
2. `Build and deploy from a Git repository` 선택
3. GitHub 저장소 목록에서 `dmf-mcp-server` 선택
4. 설정:

| 항목 | 값 |
|------|-----|
| Name | `dmf-intelligence-mcp` |
| Region | `Singapore` (한국에서 가장 빠름) |
| Runtime | `Python 3` |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `python server.py` |
| Instance Type | `Free` |

5. Environment Variables 추가:

| Key | Value |
|-----|-------|
| `PORT` | `8000` |
| `MCP_TRANSPORT` | `sse` |

6. `Create Web Service` 클릭

### 3-3. 배포 완료 확인

- 배포에 3~5분 소요
- 완료되면 URL이 생성됨: `https://dmf-intelligence-mcp.onrender.com`
- 브라우저에서 `https://dmf-intelligence-mcp.onrender.com/sse` 접속 시
  SSE 연결이 되면 성공

⚠️ **무료 플랜 주의사항:**
- 15분 동안 요청이 없으면 서버가 Sleep 상태로 전환
- 다시 요청 시 Cold Start로 30초~1분 대기 필요
- 월 750시간 무료 (1대 서버 24시간 돌리면 약 31일)

---

## STEP 4. PlayMCP에 서버 등록

### 4-1. PlayMCP 접속 & 로그인

1. https://playmcp.kakao.com 접속
2. 카카오 계정으로 로그인

### 4-2. MCP 서버 등록

1. `MCP 서버 등록` 또는 `내 MCP` 메뉴로 이동
2. 양식 작성:

| 항목 | 내용 |
|------|------|
| 서버 이름 | `DMF Intelligence` |
| 설명 | `의약품안전나라 DMF(Drug Master File) 등록 현황을 실시간 조회·분석합니다. 주간/월간 현황, 성분별·국가별 검색, 채팅 공유용 요약을 제공합니다.` |
| 서버 URL | `https://dmf-intelligence-mcp.onrender.com/sse` |
| 카테고리 | `데이터 분석` 또는 `비즈니스` |

3. 제공 도구(Tools) 설명:

| 도구명 | 설명 |
|--------|------|
| `get_weekly_dmf` | 주간 DMF 등록 현황 조회 |
| `get_monthly_dmf_summary` | 월간 DMF 등록 분석 (국가별, 신청인별, 경쟁 성분) |
| `search_dmf_by_ingredient` | 특정 성분 DMF 등록 이력 검색 |
| `search_dmf_by_country` | 특정 국가 DMF 등록 현황 검색 |
| `get_dmf_chat_summary` | 카카오톡 공유용 간결한 요약 메시지 생성 |

4. `등록 신청` 클릭

### 4-3. 심사 대기

- 카카오 내부 심사를 거쳐 승인됨 (보통 수일 소요)
- 승인 후 `전체 공개` 또는 `나에게만 공개` 선택 가능
- 개인 업무용이면 `나에게만 공개`로 충분

---

## STEP 5. 사용하기

### 방법 A: PlayMCP AI 채팅에서 직접 사용

1. https://playmcp.kakao.com 접속
2. AI 채팅 화면에서 `DMF Intelligence` MCP를 추가
3. 대화 예시:
   - "이번 주 신규 DMF 등록 현황 알려줘"
   - "인도 제조사 DMF 현황 보여줘"
   - "amoxicillin DMF 등록 내역 검색해줘"
   - "카톡에 공유할 수 있게 DMF 주간 요약 만들어줘"

### 방법 B: PlayMCP 도구함 → ChatGPT / Claude에서 사용

1. PlayMCP에서 `도구함`에 DMF Intelligence 추가
2. ChatGPT: 개발자 모드 → MCP 서버 URL 등록
3. Claude: 설정 → 커스텀 커넥터에 PlayMCP 도구함 연결
4. 이후 평소처럼 대화하면 됨

### 방법 C: Claude Desktop에서 직접 연결

```json
{
  "mcpServers": {
    "dmf-intelligence": {
      "transport": "sse",
      "url": "https://dmf-intelligence-mcp.onrender.com/sse"
    }
  }
}
```

---

## 카카오톡 단체방 공유 방법

⚠️ **현실적 제약:** 카카오톡 일반 단체방에는 API로 직접 메시지를 보낼 수 없습니다.
PlayMCP의 카카오톡 MCP도 "나와의 채팅방"까지만 지원합니다.

### 실용적인 워크플로우

```
매일 아침 (또는 매주 월요일):

1. PlayMCP AI 채팅 열기
2. "DMF 주간 요약 카톡용으로 만들어줘" 입력
3. AI가 get_dmf_chat_summary 호출 → 아래 같은 메시지 생성:

   📋 DMF 주간 현황 (01/06~01/10)
   ==============================
   총 12건 (최초 8 / 허여 4)
   연계심사 3건

   🔵최초 Sorafenib Tosylate
     대웅제약 | 인도 ✅
   🔵최초 Tofacitinib Citrate
     한국유나이티드 | 인도
   🟡허여 Amoxicillin Trihydrate
     종근당 | 중국
   ...

4. 이 텍스트를 복사 → 카카오톡 단체방에 붙여넣기
```

### 더 자동화하고 싶다면?

기존 `dmf_weekly_report.py`에 아래 코드를 추가하면,
메일 발송과 동시에 같은 요약을 텍스트 파일로 저장할 수 있습니다:

```python
# dmf_weekly_report.py의 run_weekly_report() 함수에 추가
def generate_chat_summary(data):
    """카톡 공유용 요약 텍스트 생성"""
    lines = [f"📋 DMF 주간 현황 ({data['last_week_monday'].strftime('%m/%d')}~{data['last_week_friday'].strftime('%m/%d')})"]
    lines.append("=" * 30)
    lines.append(f"총 {data['week_total']}건 (최초 {data['week_initial']} / 허여 {data['week_change']})")
    lines.append(f"연계심사 {data['week_linked_yes']}건")
    lines.append("")

    for d in data['week_details']:
        emoji = "🔵최초" if d['type'] == '최초' else "🟡허여"
        linked = " ✅" if d['linked'] == 'O' else ""
        lines.append(f"{emoji} {d['ingredient']}")
        lines.append(f"  {d['applicant']} | {d['country']}{linked}")

    lines.append("")
    lines.append("출처: 의약품안전나라 DMF 심사결과")
    return "\n".join(lines)
```

---

## 제공 도구(Tools) 상세

### 1. get_weekly_dmf
| 항목 | 내용 |
|------|------|
| 기능 | 주간 DMF 등록 현황 조회 |
| 입력 | `weeks_ago` (기본값 1 = 지난주) |
| 출력 | 총건수, 최초/허여, 성분별 상세 내역 |
| 사용 예 | "지난주 DMF 등록 현황 보여줘" |

### 2. get_monthly_dmf_summary
| 항목 | 내용 |
|------|------|
| 기능 | 월간 DMF 분석 (국가별, 신청인별, 경쟁 성분) |
| 입력 | `months_ago` (기본값 1 = 전월) |
| 출력 | 월간 통계, 전월 대비 변동률, TOP5 신청인, 경쟁 성분 |
| 사용 예 | "지난달 DMF 트렌드 분석해줘" |

### 3. search_dmf_by_ingredient
| 항목 | 내용 |
|------|------|
| 기능 | 특정 성분 DMF 검색 |
| 입력 | `ingredient` (성분명, 부분 일치) |
| 출력 | 등록 이력, 신청인 수, 국가 분포 |
| 사용 예 | "tofacitinib DMF 등록 현황 알려줘" |

### 4. search_dmf_by_country
| 항목 | 내용 |
|------|------|
| 기능 | 특정 국가 DMF 검색 |
| 입력 | `country` (국가명, 부분 일치) |
| 출력 | 전체 건수, 최근 3개월 신규, 주요 성분/제조소 |
| 사용 예 | "인도 DMF 현황 보여줘" |

### 5. get_dmf_chat_summary
| 항목 | 내용 |
|------|------|
| 기능 | 카카오톡/메신저용 간결한 요약 생성 |
| 입력 | 없음 (자동으로 지난주 기준) |
| 출력 | 복사-붙여넣기 가능한 텍스트 |
| 사용 예 | "카톡에 공유할 DMF 요약 만들어줘" |

---

## 프로젝트 파일 구조

```
dmf-mcp-server/
├── server.py           # MCP 서버 (메인 코드)
├── requirements.txt    # Python 패키지 목록
├── render.yaml         # Render.com 배포 설정
├── Dockerfile          # Docker 배포용 (선택)
└── README.md           # 프로젝트 설명 (GitHub용)
```

---

## 문제 해결 (Troubleshooting)

### "의약품안전나라 접속이 안 됩니다"
→ Render.com 무료 서버가 해외 IP라 차단될 수 있음
→ 해결: 한국 클라우드(카카오클라우드, NCP)로 이전하거나,
  프록시 설정 필요. 먼저 브라우저에서 URL 직접 접속 테스트:
  https://nedrug.mfds.go.kr/pbp/CCBAC03/getExcel

### "서버 응답이 느립니다"
→ 무료 플랜 Cold Start (30초~1분) + 엑셀 다운로드 시간
→ 첫 요청은 느리고, 연속 요청은 빠름

### "PlayMCP 심사가 통과되지 않습니다"
→ 서버 URL이 실제로 접근 가능한지 확인
→ 도구 설명(description)이 명확한지 확인
→ PlayMCP 디스코드 채널에서 문의 가능

---

## 향후 확장 아이디어

1. **데이터 캐싱**: 매번 다운로드 대신 일 1회 캐싱 → 응답 속도 개선
2. **알림 자동화**: n8n/Make.com 연동으로 매일 아침 자동 실행
3. **카카오워크 연동**: 회사에서 카카오워크 도입 시 웹훅으로 단체방 자동 전송 가능
4. **블루오션 분석**: DMF 미등록 성분 중 수요가 있는 성분 자동 탐지
