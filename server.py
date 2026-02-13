"""
DMF Intelligence Server (MCP + 카카오톡 채널 챗봇)
===================================================
의약품안전나라 DMF 데이터를 조회·분석하는 통합 서버

[1] MCP 서버: Claude Desktop / PlayMCP에서 사용
[2] 카카오 웹훅 API: 카카오 i 오픈빌더 Skill 서버

배포: Render.com → 하나의 서버로 두 기능 모두 제공
"""

import os
import json
import tempfile
import logging
import re
from datetime import datetime, timedelta
from collections import Counter
from typing import Optional
from contextlib import asynccontextmanager

import requests
import pandas as pd
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

# MCP (조건부 임포트 — MCP 없이도 카카오 웹훅만으로 동작 가능)
try:
    from mcp.server.fastmcp import FastMCP
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False


# ═══════════════════════════════════════════
# 로깅 설정
# ═══════════════════════════════════════════
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dmf-server")


# ═══════════════════════════════════════════
# 데이터 캐싱 (카카오 5초 타임아웃 대응)
# ═══════════════════════════════════════════

import threading

_cache = {
    "df": None,           # 캐싱된 DataFrame
    "last_updated": None, # 마지막 업데이트 시각
    "loading": False      # 로딩 중 여부
}
CACHE_TTL = timedelta(hours=24)  # 하루 1회 갱신


def _download_dmf_excel() -> str:
    """의약품안전나라에서 DMF 엑셀 다운로드 → 임시 파일 경로 반환"""
    url = "https://nedrug.mfds.go.kr/pbp/CCBAC03/getExcel"
    logger.info("📥 DMF 엑셀 다운로드 중...")
    response = requests.get(url, timeout=120)
    response.raise_for_status()

    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    tmp.write(response.content)
    tmp.close()
    logger.info(f"✅ 다운로드 완료: {tmp.name}")
    return tmp.name


def _get_cached_data() -> pd.DataFrame:
    """캐싱된 데이터 반환. 없거나 만료되면 새로 다운로드."""
    now = datetime.now()

    # 캐시가 유효하면 바로 반환
    if (_cache["df"] is not None and
        _cache["last_updated"] is not None and
        now - _cache["last_updated"] < CACHE_TTL):
        logger.info("⚡ 캐시 데이터 사용")
        return _cache["df"]

    # 캐시 갱신
    logger.info("🔄 캐시 갱신 중...")
    excel_path = _download_dmf_excel()
    try:
        df = _load_and_prepare(excel_path)
        _cache["df"] = df
        _cache["last_updated"] = now
        logger.info(f"✅ 캐시 갱신 완료 ({len(df)}건)")
        return df
    finally:
        os.unlink(excel_path)


def _preload_cache():
    """서버 시작 시 백그라운드로 캐시 미리 로드"""
    try:
        _cache["loading"] = True
        _get_cached_data()
    except Exception as e:
        logger.error(f"❌ 캐시 프리로드 실패: {e}")
    finally:
        _cache["loading"] = False


def _load_and_prepare(excel_path: str) -> pd.DataFrame:
    """엑셀 로드 + 기본 전처리"""
    df = pd.read_excel(excel_path)

    # NaN 처리 (빈 칸을 빈 문자열로 변환)
    text_cols = ['성분명', '신청인', '제조소명', '제조국가', '등록번호',
                 '취소/취하구분', '연계심사문서번호']
    for col in text_cols:
        if col in df.columns:
            df[col] = df[col].fillna('')

    df['최초등록일자'] = pd.to_datetime(df['최초등록일자'], errors='coerce')

    df['is_허여'] = df['등록번호'].astype(str).str.contains(r'\(', na=False)
    df['등록유형'] = df['is_허여'].map({True: '허여(변경)', False: '최초등록'})

    df['base_dmf'] = df['등록번호'].astype(str).apply(
        lambda x: x.split('(', 1)[0] if '(' in x else x
    )
    has_linked = (df['연계심사문서번호'].astype(str).str.strip() != '')
    linked_bases = set(df.loc[has_linked, 'base_dmf'])
    df['has_연계심사'] = df['base_dmf'].isin(linked_bases)

    active = df[df['취소/취하구분'] == '정상'].copy()
    return active


# ─── 분석 함수들 (JSON dict 반환) ───

def analyze_weekly_dmf(weeks_ago: int = 1) -> dict:
    """주간 DMF 등록 현황 분석"""
    try:
        active = _get_cached_data()

        today = datetime.today()
        days_since_monday = today.weekday()
        this_monday = today - timedelta(days=days_since_monday)
        target_monday = this_monday - timedelta(weeks=weeks_ago)
        target_friday = target_monday + timedelta(days=4)

        mask = (active['최초등록일자'] >= pd.Timestamp(target_monday)) & \
               (active['최초등록일자'] <= pd.Timestamp(target_friday))
        week_df = active[mask].sort_values('최초등록일자', ascending=False)

        week_label = f"{target_monday.strftime('%m/%d')}~{target_friday.strftime('%m/%d')}"

        if len(week_df) == 0:
            return {"기간": week_label, "메시지": "해당 주간 신규 DMF 등록 없음", "총건수": 0}

        details = []
        for _, row in week_df.iterrows():
            details.append({
                "등록일": row['최초등록일자'].strftime('%m/%d'),
                "등록유형": '허여' if row['is_허여'] else '최초',
                "성분명": str(row.get('성분명', '')),
                "신청인": str(row.get('신청인', '')),
                "제조소": str(row.get('제조소명', ''))[:25],
                "국가": str(row.get('제조국가', '')).replace('@', '/'),
                "연계심사": 'O' if row['has_연계심사'] else 'X'
            })

        return {
            "기간": week_label,
            "총건수": len(week_df),
            "최초등록": int((~week_df['is_허여']).sum()),
            "허여_변경": int(week_df['is_허여'].sum()),
            "연계심사_있음": int(week_df['has_연계심사'].sum()),
            "상세내역": details
        }
    except Exception as e:
        logger.error(f"주간 분석 실패: {e}")
        raise


def analyze_monthly_dmf(months_ago: int = 1) -> dict:
    """월간 DMF 등록 현황 분석"""
    try:
        active = _get_cached_data()

        today = datetime.today()
        target_end = today.replace(day=1) - timedelta(days=1)
        for _ in range(months_ago - 1):
            target_end = target_end.replace(day=1) - timedelta(days=1)
        target_start = target_end.replace(day=1)

        month_label = target_start.strftime('%Y년 %m월')

        mask = (active['최초등록일자'] >= pd.Timestamp(target_start)) & \
               (active['최초등록일자'] <= pd.Timestamp(target_end))
        month_df = active[mask]

        prev_end = target_start - timedelta(days=1)
        prev_start = prev_end.replace(day=1)
        prev_mask = (active['최초등록일자'] >= pd.Timestamp(prev_start)) & \
                    (active['최초등록일자'] <= pd.Timestamp(prev_end))
        prev_count = int(active[prev_mask].shape[0])

        if prev_count > 0:
            change_pct = (len(month_df) - prev_count) / prev_count * 100
            change_str = f"+{change_pct:.1f}%" if change_pct >= 0 else f"{change_pct:.1f}%"
        else:
            change_str = "N/A"

        countries = []
        for c in month_df['제조국가'].dropna():
            for cc in str(c).split('@'):
                countries.append(cc.strip())
        country_counts = Counter(countries).most_common(10)
        total_c = sum(dict(country_counts).values()) if country_counts else 1
        country_list = [
            {"국가": c, "건수": n, "비율": f"{n/total_c*100:.1f}%"}
            for c, n in country_counts
        ]

        top_applicants = month_df.groupby('신청인').agg(
            건수=('등록번호', 'count')
        ).sort_values('건수', ascending=False).head(5)
        applicant_list = [
            {"신청인": name, "건수": int(row['건수'])}
            for name, row in top_applicants.iterrows()
        ]

        return {
            "기간": month_label,
            "총건수": len(month_df),
            "최초등록": int((~month_df['is_허여']).sum()),
            "허여_변경": int(month_df['is_허여'].sum()),
            "전월대비_변동": change_str,
            "전월_건수": prev_count,
            "국가별_분포": country_list,
            "주요_신청인_TOP5": applicant_list
        }
    except Exception as e:
        logger.error(f"월간 분석 실패: {e}")
        raise


def search_ingredient(ingredient: str) -> dict:
    """성분명으로 DMF 검색"""
    try:
        active = _get_cached_data()

        mask = active['성분명'].astype(str).str.contains(ingredient, case=False, na=False)
        found = active[mask].sort_values('최초등록일자', ascending=False)

        if len(found) == 0:
            return {"검색어": ingredient, "메시지": f"'{ingredient}' 관련 DMF 등록 없음", "총건수": 0}

        entries = []
        for _, row in found.head(20).iterrows():
            entries.append({
                "등록번호": str(row.get('등록번호', '')),
                "등록일": row['최초등록일자'].strftime('%Y-%m-%d') if pd.notna(row['최초등록일자']) else '',
                "등록유형": row['등록유형'],
                "성분명": str(row.get('성분명', '')),
                "신청인": str(row.get('신청인', '')),
                "제조소": str(row.get('제조소명', '')),
                "국가": str(row.get('제조국가', '')).replace('@', '/'),
                "연계심사": 'O' if row['has_연계심사'] else 'X'
            })

        return {
            "검색어": ingredient,
            "총_등록건수": len(found),
            "신청인_수": int(found['신청인'].nunique()),
            "등록내역": entries
        }
    except Exception as e:
        logger.error(f"성분 검색 실패: {e}")
        raise


def search_country(country: str) -> dict:
    """국가별 DMF 검색"""
    try:
        active = _get_cached_data()

        mask = active['제조국가'].astype(str).str.contains(country, case=False, na=False)
        found = active[mask].sort_values('최초등록일자', ascending=False)

        if len(found) == 0:
            return {"검색_국가": country, "메시지": f"'{country}' 관련 DMF 없음", "총건수": 0}

        three_months_ago = datetime.today() - timedelta(days=90)
        recent = found[found['최초등록일자'] >= pd.Timestamp(three_months_ago)]

        top_ingredients = found['성분명'].value_counts().head(10)
        ingredient_list = [
            {"성분명": name, "건수": int(cnt)}
            for name, cnt in top_ingredients.items()
        ]

        top_mfrs = found['제조소명'].value_counts().head(10)
        mfr_list = [
            {"제조소": name, "건수": int(cnt)}
            for name, cnt in top_mfrs.items()
        ]

        return {
            "검색_국가": country,
            "전체_등록건수": len(found),
            "최근3개월_신규": len(recent),
            "주요_성분_TOP10": ingredient_list,
            "주요_제조소_TOP10": mfr_list
        }
    except Exception as e:
        logger.error(f"국가 검색 실패: {e}")
        raise


def generate_chat_summary() -> str:
    """카카오톡 공유용 간결한 요약 메시지"""
    try:
        active = _get_cached_data()

        today = datetime.today()
        days_since_monday = today.weekday()
        this_monday = today - timedelta(days=days_since_monday)
        last_monday = this_monday - timedelta(days=7)
        last_friday = last_monday + timedelta(days=4)

        mask = (active['최초등록일자'] >= pd.Timestamp(last_monday)) & \
               (active['최초등록일자'] <= pd.Timestamp(last_friday))
        week_df = active[mask].sort_values('최초등록일자', ascending=False)

        week_label = f"{last_monday.strftime('%m/%d')}~{last_friday.strftime('%m/%d')}"

        lines = []
        lines.append(f"📋 DMF 주간 현황 ({week_label})")
        lines.append(f"{'='*28}")

        if len(week_df) == 0:
            lines.append("해당 주간 신규 DMF 등록 없음")
        else:
            initial = int((~week_df['is_허여']).sum())
            change = int(week_df['is_허여'].sum())
            linked = int(week_df['has_연계심사'].sum())

            lines.append(f"총 {len(week_df)}건 (최초 {initial} / 허여 {change})")
            lines.append(f"연계심사 {linked}건")
            lines.append("")

            for _, row in week_df.iterrows():
                reg_type = "🔵최초" if not row['is_허여'] else "🟡허여"
                linked_mark = "✅" if row['has_연계심사'] else ""
                country = str(row.get('제조국가', '')).replace('@', '/').strip()
                ingredient = str(row.get('성분명', ''))
                applicant = str(row.get('신청인', ''))

                lines.append(f"{reg_type} {ingredient}")
                lines.append(f"  {applicant} | {country} {linked_mark}")

            lines.append("")
            lines.append("출처: 의약품안전나라 DMF 심사결과")

        return "\n".join(lines)
    except Exception as e:
        logger.error(f"요약 생성 실패: {e}")
        raise


# ═══════════════════════════════════════════
# [1] MCP 서버 설정
# ═══════════════════════════════════════════

if MCP_AVAILABLE:
    mcp = FastMCP(
        "dmf-intelligence",
        instructions="""DMF(Drug Master File) 등록 현황을 조회·분석하는 도구입니다.
        의약품안전나라(nedrug.mfds.go.kr)의 공개 데이터를 기반으로
        신규 DMF 등록, 국가별/성분별 분석, 경쟁 동향 등을 제공합니다."""
    )

    @mcp.tool()
    def get_weekly_dmf(weeks_ago: int = 1) -> str:
        """최근 주간 DMF 등록 현황을 조회합니다."""
        try:
            return json.dumps(analyze_weekly_dmf(weeks_ago), ensure_ascii=False, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    @mcp.tool()
    def get_monthly_dmf_summary(months_ago: int = 1) -> str:
        """월간 DMF 등록 현황 요약을 조회합니다."""
        try:
            return json.dumps(analyze_monthly_dmf(months_ago), ensure_ascii=False, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    @mcp.tool()
    def search_dmf_by_ingredient(ingredient: str) -> str:
        """특정 성분명으로 DMF 등록 현황을 검색합니다."""
        try:
            return json.dumps(search_ingredient(ingredient), ensure_ascii=False, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    @mcp.tool()
    def search_dmf_by_country(country: str) -> str:
        """특정 국가의 DMF 등록 현황을 검색합니다."""
        try:
            return json.dumps(search_country(country), ensure_ascii=False, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    @mcp.tool()
    def get_dmf_chat_summary() -> str:
        """카카오톡 공유용 간결한 DMF 요약 메시지를 생성합니다."""
        try:
            return generate_chat_summary()
        except Exception as e:
            return f"❌ 요약 생성 실패: {e}"


# ═══════════════════════════════════════════
# [2] 카카오 i 오픈빌더 Skill 웹훅 API
# ═══════════════════════════════════════════

@asynccontextmanager
async def lifespan(app):
    """서버 시작 시 캐시 프리로드"""
    thread = threading.Thread(target=_preload_cache, daemon=True)
    thread.start()
    logger.info("🚀 백그라운드 캐시 프리로드 시작")
    yield

app = FastAPI(title="DMF Intelligence Server", version="2.0", lifespan=lifespan)


def kakao_simple_text(text: str) -> dict:
    """카카오 오픈빌더 simpleText 응답 생성"""
    return {
        "version": "2.0",
        "template": {
            "outputs": [
                {"simpleText": {"text": text}}
            ]
        }
    }


def kakao_text_with_buttons(text: str, buttons: list) -> dict:
    """카카오 오픈빌더 텍스트 + 버튼 응답 생성"""
    return {
        "version": "2.0",
        "template": {
            "outputs": [
                {
                    "basicCard": {
                        "description": text,
                        "buttons": buttons
                    }
                }
            ]
        }
    }


def kakao_quick_replies(text: str, replies: list) -> dict:
    """카카오 오픈빌더 텍스트 + 바로가기 응답 생성"""
    return {
        "version": "2.0",
        "template": {
            "outputs": [
                {"simpleText": {"text": text}}
            ],
            "quickReplies": replies
        }
    }


def format_weekly_for_kakao(data: dict) -> str:
    """주간 분석 결과를 카카오톡 메시지 형태로 포맷"""
    if data.get("총건수", 0) == 0:
        return f"📋 DMF 주간 현황 ({data['기간']})\n\n{data.get('메시지', '등록 없음')}"

    lines = [
        f"📋 DMF 주간 현황 ({data['기간']})",
        f"{'─'*24}",
        f"총 {data['총건수']}건 (최초 {data['최초등록']} / 허여 {data['허여_변경']})",
        f"연계심사 {data['연계심사_있음']}건",
        ""
    ]

    for item in data.get("상세내역", [])[:15]:  # 카카오톡 글자수 제한 고려
        reg_icon = "🔵" if item['등록유형'] == '최초' else "🟡"
        linked = " ✅" if item['연계심사'] == 'O' else ""
        lines.append(f"{reg_icon} {item['성분명']}")
        lines.append(f"  {item['신청인']} | {item['국가']}{linked}")

    if len(data.get("상세내역", [])) > 15:
        lines.append(f"\n... 외 {len(data['상세내역']) - 15}건")

    lines.append("\n출처: 의약품안전나라")
    return "\n".join(lines)


def format_monthly_for_kakao(data: dict) -> str:
    """월간 분석 결과를 카카오톡 메시지 형태로 포맷"""
    lines = [
        f"📊 DMF 월간 리포트 ({data['기간']})",
        f"{'─'*24}",
        f"총 {data['총건수']}건 (전월 {data['전월_건수']}건, {data['전월대비_변동']})",
        f"  최초등록 {data['최초등록']}건 / 허여 {data['허여_변경']}건",
        ""
    ]

    if data.get("국가별_분포"):
        lines.append("🌍 국가별 분포:")
        for item in data["국가별_분포"][:5]:
            lines.append(f"  {item['국가']}: {item['건수']}건 ({item['비율']})")

    if data.get("주요_신청인_TOP5"):
        lines.append("\n👤 주요 신청인:")
        for item in data["주요_신청인_TOP5"]:
            lines.append(f"  {item['신청인']}: {item['건수']}건")

    lines.append("\n출처: 의약품안전나라")
    return "\n".join(lines)


def format_ingredient_for_kakao(data: dict) -> str:
    """성분 검색 결과를 카카오톡 메시지 형태로 포맷"""
    if data.get("총건수", data.get("총_등록건수", 0)) == 0:
        return f"🔍 '{data['검색어']}' 검색 결과\n\n{data.get('메시지', '등록 없음')}"

    total = data.get("총_등록건수", 0)
    lines = [
        f"🔍 '{data['검색어']}' DMF 현황",
        f"{'─'*24}",
        f"총 {total}건 (신청인 {data.get('신청인_수', '?')}개사)",
        ""
    ]

    for item in data.get("등록내역", [])[:10]:
        reg_icon = "🔵" if item.get('등록유형', '') == '최초등록' else "🟡"
        linked = " ✅" if item.get('연계심사') == 'O' else ""
        lines.append(f"{reg_icon} {item['등록일']} | {item['신청인']}")
        lines.append(f"  {item['제조소'][:20]} ({item['국가']}){linked}")

    if total > 10:
        lines.append(f"\n... 외 {total - 10}건")

    lines.append("\n출처: 의약품안전나라")
    return "\n".join(lines)


def format_country_for_kakao(data: dict) -> str:
    """국가 검색 결과를 카카오톡 메시지 형태로 포맷"""
    if data.get("전체_등록건수", 0) == 0:
        return f"🌍 '{data['검색_국가']}' 검색 결과\n\n{data.get('메시지', '등록 없음')}"

    lines = [
        f"🌍 {data['검색_국가']} DMF 현황",
        f"{'─'*24}",
        f"전체 {data['전체_등록건수']}건 (최근3개월 {data['최근3개월_신규']}건)",
        ""
    ]

    if data.get("주요_성분_TOP10"):
        lines.append("💊 주요 성분:")
        for item in data["주요_성분_TOP10"][:7]:
            lines.append(f"  {item['성분명']}: {item['건수']}건")

    if data.get("주요_제조소_TOP10"):
        lines.append("\n🏭 주요 제조소:")
        for item in data["주요_제조소_TOP10"][:5]:
            lines.append(f"  {item['제조소'][:25]}: {item['건수']}건")

    lines.append("\n출처: 의약품안전나라")
    return "\n".join(lines)


def parse_user_intent(utterance: str) -> tuple:
    """
    사용자 발화를 분석하여 의도와 파라미터 추출

    Returns:
        (intent, params) 튜플
        intent: 'weekly' | 'monthly' | 'ingredient' | 'country' | 'summary' | 'help'
    """
    text = utterance.strip().lower()

    # 주간
    if any(kw in text for kw in ['주간', '이번주', '이번 주', '금주', '지난주', '지난 주', '주별']):
        return ('weekly', {})

    # 월간
    if any(kw in text for kw in ['월간', '이번달', '이번 달', '전월', '지난달', '지난 달', '월별']):
        return ('monthly', {})

    # 요약 / 채팅 공유
    if any(kw in text for kw in ['요약', '공유', '정리', '카톡', '챗']):
        return ('summary', {})

    # 국가 검색 (패턴: "인도 DMF", "중국 현황" 등)
    country_keywords = ['인도', '중국', '일본', '미국', '독일', '이탈리아', '스페인',
                        '프랑스', '영국', '캐나다', '브라질', '대만', '한국',
                        'india', 'china', 'japan', 'usa', 'germany', 'italy', 'spain']
    for kw in country_keywords:
        if kw in text:
            return ('country', {'country': kw})

    # 국가 패턴: "~나라 DMF", "~국가 현황"
    country_match = re.search(r'(\S+)\s*(나라|국가|제조소|제조사)', text)
    if country_match:
        return ('country', {'country': country_match.group(1)})

    # 도움말
    if any(kw in text for kw in ['도움', '사용법', '안내', '메뉴', '뭘 할 수', '기능', '명령']):
        return ('help', {})

    # 기본: 성분명으로 간주 (나머지 텍스트)
    # 너무 짧거나 일반적인 인사는 help로
    if len(text) <= 1 or text in ['안녕', '하이', 'hi', 'hello', '시작']:
        return ('help', {})

    return ('ingredient', {'ingredient': utterance.strip()})


# ─── 카카오 웹훅 엔드포인트들 ───

@app.get("/")
async def health_check():
    """서버 상태 확인"""
    return {
        "status": "running",
        "service": "DMF Intelligence Server",
        "cache": "loaded" if _cache["df"] is not None else "empty",
        "last_updated": str(_cache["last_updated"]) if _cache["last_updated"] else None,
        "endpoints": {
            "kakao_webhook": "/kakao/skill",
            "mcp_sse": "/sse" if MCP_AVAILABLE else "not available"
        }
    }


@app.get("/refresh")
async def refresh_cache():
    """캐시 강제 갱신 (Cron Job용) — 매일 아침 7시 호출"""
    try:
        _cache["df"] = None
        _cache["last_updated"] = None
        _get_cached_data()
        return {
            "status": "refreshed",
            "records": len(_cache["df"]),
            "updated_at": str(_cache["last_updated"])
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/kakao/skill")
async def kakao_skill_handler(request: Request):
    """
    카카오 i 오픈빌더 통합 Skill 엔드포인트
    
    사용자 발화를 자동 분석하여 적절한 DMF 정보를 반환합니다.
    오픈빌더의 '폴백 블록'에 연결하면, 모든 입력을 여기서 처리합니다.
    """
    try:
        body = await request.json()
        utterance = body.get("userRequest", {}).get("utterance", "")
        params = body.get("action", {}).get("params", {})

        logger.info(f"📨 카카오 요청: '{utterance}' | params: {params}")

        # 캐시가 아직 준비 안 됐으면 즉시 안내
        if _cache["df"] is None and _cache["loading"]:
            return JSONResponse(kakao_simple_text(
                "🔄 서버가 준비 중입니다.\n10초 후 다시 시도해주세요!"
            ))

        intent, extracted = parse_user_intent(utterance)

        if intent == 'weekly':
            data = analyze_weekly_dmf()
            text = format_weekly_for_kakao(data)
            return JSONResponse(kakao_quick_replies(text, [
                {"messageText": "월간", "action": "message", "label": "📊 월간 리포트"},
                {"messageText": "요약", "action": "message", "label": "📋 채팅 공유용"},
                {"messageText": "도움", "action": "message", "label": "❓ 사용법"}
            ]))

        elif intent == 'monthly':
            data = analyze_monthly_dmf()
            text = format_monthly_for_kakao(data)
            return JSONResponse(kakao_quick_replies(text, [
                {"messageText": "주간", "action": "message", "label": "📋 주간 현황"},
                {"messageText": "인도", "action": "message", "label": "🇮🇳 인도 DMF"},
                {"messageText": "도움", "action": "message", "label": "❓ 사용법"}
            ]))

        elif intent == 'summary':
            text = generate_chat_summary()
            return JSONResponse(kakao_simple_text(text))

        elif intent == 'country':
            country = extracted.get('country', params.get('country', ''))
            if not country:
                return JSONResponse(kakao_simple_text("어느 국가의 DMF를 검색할까요?\n\n예: 인도, 중국, 일본, 미국"))
            data = search_country(country)
            text = format_country_for_kakao(data)
            return JSONResponse(kakao_quick_replies(text, [
                {"messageText": "주간", "action": "message", "label": "📋 주간 현황"},
                {"messageText": "월간", "action": "message", "label": "📊 월간 리포트"},
                {"messageText": "도움", "action": "message", "label": "❓ 사용법"}
            ]))

        elif intent == 'ingredient':
            ingredient = extracted.get('ingredient', params.get('ingredient', ''))
            if not ingredient:
                return JSONResponse(kakao_simple_text("검색할 성분명을 입력해주세요.\n\n예: amoxicillin, tofacitinib, 소라페닙"))
            data = search_ingredient(ingredient)
            text = format_ingredient_for_kakao(data)
            return JSONResponse(kakao_quick_replies(text, [
                {"messageText": "주간", "action": "message", "label": "📋 주간 현황"},
                {"messageText": "월간", "action": "message", "label": "📊 월간 리포트"},
                {"messageText": "도움", "action": "message", "label": "❓ 사용법"}
            ]))

        else:  # help
            help_text = (
                "💊 DMF Intelligence 챗봇\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "의약품안전나라 DMF 데이터를\n"
                "실시간으로 조회·분석합니다.\n\n"
                "📋 사용법:\n"
                "• '주간' → 주간 DMF 등록 현황\n"
                "• '월간' → 월간 DMF 리포트\n"
                "• '요약' → 채팅 공유용 요약\n"
                "• '인도' → 인도 DMF 현황\n"
                "• 'amoxicillin' → 성분명 검색\n\n"
                "성분명을 직접 입력하시면\n"
                "해당 성분의 DMF 현황을 검색합니다."
            )
            return JSONResponse(kakao_quick_replies(help_text, [
                {"messageText": "주간", "action": "message", "label": "📋 주간 현황"},
                {"messageText": "월간", "action": "message", "label": "📊 월간 리포트"},
                {"messageText": "인도", "action": "message", "label": "🇮🇳 인도 DMF"},
                {"messageText": "중국", "action": "message", "label": "🇨🇳 중국 DMF"}
            ]))

    except Exception as e:
        logger.error(f"❌ 카카오 스킬 처리 실패: {e}")
        return JSONResponse(kakao_simple_text(
            f"⚠️ 처리 중 오류가 발생했습니다.\n잠시 후 다시 시도해주세요.\n\n(오류: {str(e)[:100]})"
        ))


# 개별 스킬 엔드포인트 (오픈빌더에서 블록별로 연결할 때 사용)
@app.post("/kakao/weekly")
async def kakao_weekly(request: Request):
    """주간 DMF 현황 전용 스킬"""
    try:
        data = analyze_weekly_dmf()
        text = format_weekly_for_kakao(data)
        return JSONResponse(kakao_simple_text(text))
    except Exception as e:
        return JSONResponse(kakao_simple_text(f"⚠️ 조회 실패: {str(e)[:100]}"))


@app.post("/kakao/monthly")
async def kakao_monthly(request: Request):
    """월간 DMF 리포트 전용 스킬"""
    try:
        data = analyze_monthly_dmf()
        text = format_monthly_for_kakao(data)
        return JSONResponse(kakao_simple_text(text))
    except Exception as e:
        return JSONResponse(kakao_simple_text(f"⚠️ 조회 실패: {str(e)[:100]}"))


@app.post("/kakao/summary")
async def kakao_summary(request: Request):
    """채팅 공유용 요약 전용 스킬"""
    try:
        text = generate_chat_summary()
        return JSONResponse(kakao_simple_text(text))
    except Exception as e:
        return JSONResponse(kakao_simple_text(f"⚠️ 요약 실패: {str(e)[:100]}"))


@app.post("/kakao/ingredient")
async def kakao_ingredient(request: Request):
    """성분명 검색 전용 스킬 (파라미터: ingredient)"""
    try:
        body = await request.json()
        utterance = body.get("userRequest", {}).get("utterance", "")
        ingredient = body.get("action", {}).get("params", {}).get("ingredient", utterance)

        if not ingredient:
            return JSONResponse(kakao_simple_text("검색할 성분명을 입력해주세요."))

        data = search_ingredient(ingredient)
        text = format_ingredient_for_kakao(data)
        return JSONResponse(kakao_simple_text(text))
    except Exception as e:
        return JSONResponse(kakao_simple_text(f"⚠️ 검색 실패: {str(e)[:100]}"))


@app.post("/kakao/country")
async def kakao_country(request: Request):
    """국가 검색 전용 스킬 (파라미터: country)"""
    try:
        body = await request.json()
        utterance = body.get("userRequest", {}).get("utterance", "")
        country = body.get("action", {}).get("params", {}).get("country", utterance)

        if not country:
            return JSONResponse(kakao_simple_text("검색할 국가명을 입력해주세요."))

        data = search_country(country)
        text = format_country_for_kakao(data)
        return JSONResponse(kakao_simple_text(text))
    except Exception as e:
        return JSONResponse(kakao_simple_text(f"⚠️ 검색 실패: {str(e)[:100]}"))


# ═══════════════════════════════════════════
# 서버 실행 (MCP + 카카오 동시 지원)
# ═══════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    mode = os.environ.get("SERVER_MODE", "kakao")  # "kakao" | "mcp" | "both"

    if mode == "mcp" and MCP_AVAILABLE:
        # MCP 전용 모드 (Claude Desktop / PlayMCP)
        print(f"🚀 DMF MCP Server (SSE) 시작 — Port {port}")
        mcp.run(transport="sse", port=port)

    elif mode == "both" and MCP_AVAILABLE:
        # 두 서버 동시 실행 (별도 포트)
        import threading
        mcp_port = int(os.environ.get("MCP_PORT", 8001))

        def run_mcp():
            print(f"🚀 MCP Server 시작 — Port {mcp_port}")
            mcp.run(transport="sse", port=mcp_port)

        mcp_thread = threading.Thread(target=run_mcp, daemon=True)
        mcp_thread.start()

        print(f"🚀 카카오 웹훅 Server 시작 — Port {port}")
        uvicorn.run(app, host="0.0.0.0", port=port)

    else:
        # 카카오 웹훅 전용 모드 (기본)
        print(f"🚀 DMF 카카오 챗봇 Server 시작 — Port {port}")
        print(f"   웹훅 URL: https://YOUR-APP.onrender.com/kakao/skill")
        uvicorn.run(app, host="0.0.0.0", port=port)
