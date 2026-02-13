"""
DMF Intelligence MCP Server
============================
의약품안전나라 DMF 데이터를 조회·분석하는 MCP 서버
PlayMCP 및 Claude/ChatGPT에서 사용 가능

사용 예시 (AI 대화):
  "이번 주 신규 DMF 등록 현황 알려줘"
  "인도 제조사 DMF만 보여줘"
  "최근 한 달 DMF 트렌드 분석해줘"
"""

import os
import json
import tempfile
import logging
from datetime import datetime, timedelta
from collections import Counter
from typing import Optional

import requests
import pandas as pd
from mcp.server.fastmcp import FastMCP

# ═══════════════════════════════════════════
# MCP 서버 초기화
# ═══════════════════════════════════════════

mcp = FastMCP(
    "dmf-intelligence",
    instructions="""DMF(Drug Master File) 등록 현황을 조회·분석하는 도구입니다.
    의약품안전나라(nedrug.mfds.go.kr)의 공개 데이터를 기반으로
    신규 DMF 등록, 국가별/성분별 분석, 경쟁 동향 등을 제공합니다.
    한국 제약 원료(API) 시장의 소싱 인텔리전스에 활용됩니다."""
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dmf-mcp")

# ═══════════════════════════════════════════
# 내부 함수: 데이터 다운로드 및 분석
# ═══════════════════════════════════════════

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


def _load_and_prepare(excel_path: str) -> pd.DataFrame:
    """엑셀 로드 + 기본 전처리"""
    df = pd.read_excel(excel_path)
    df['최초등록일자'] = pd.to_datetime(df['최초등록일자'], errors='coerce')

    # 등록유형 분류
    df['is_허여'] = df['등록번호'].astype(str).str.contains(r'\(', na=False)
    df['등록유형'] = df['is_허여'].map({True: '허여(변경)', False: '최초등록'})

    # 연계심사 여부
    df['base_dmf'] = df['등록번호'].astype(str).apply(
        lambda x: x.split('(', 1)[0] if '(' in x else x
    )
    has_linked = df['연계심사문서번호'].notna() & (df['연계심사문서번호'].astype(str).str.strip() != '')
    linked_bases = set(df.loc[has_linked, 'base_dmf'])
    df['has_연계심사'] = df['base_dmf'].isin(linked_bases)

    # 정상 상태만
    active = df[df['취소/취하구분'] == '정상'].copy()
    return active


# ═══════════════════════════════════════════
# MCP Tools (AI가 호출하는 도구들)
# ═══════════════════════════════════════════

@mcp.tool()
def get_weekly_dmf(weeks_ago: int = 1) -> str:
    """
    최근 주간 DMF 등록 현황을 조회합니다.

    Args:
        weeks_ago: 몇 주 전 데이터를 조회할지 (기본값 1 = 지난주)

    Returns:
        주간 DMF 등록 요약 (건수, 최초/허여, 성분별 상세)
    """
    try:
        excel_path = _download_dmf_excel()
        active = _load_and_prepare(excel_path)

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
            return json.dumps({
                "기간": week_label,
                "메시지": "해당 주간 신규 DMF 등록 내역이 없습니다."
            }, ensure_ascii=False)

        # 상세 내역
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

        result = {
            "기간": week_label,
            "총건수": len(week_df),
            "최초등록": int((~week_df['is_허여']).sum()),
            "허여_변경": int(week_df['is_허여'].sum()),
            "연계심사_있음": int(week_df['has_연계심사'].sum()),
            "상세내역": details
        }

        os.unlink(excel_path)
        return json.dumps(result, ensure_ascii=False, indent=2)

    except Exception as e:
        logger.error(f"주간 DMF 조회 실패: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool()
def get_monthly_dmf_summary(months_ago: int = 1) -> str:
    """
    월간 DMF 등록 현황 요약을 조회합니다.
    전월 대비 변동률, 국가별 분포, 주요 신청인, 경쟁 성분을 분석합니다.

    Args:
        months_ago: 몇 개월 전 데이터를 조회할지 (기본값 1 = 전월)

    Returns:
        월간 DMF 분석 요약
    """
    try:
        excel_path = _download_dmf_excel()
        active = _load_and_prepare(excel_path)

        today = datetime.today()
        # 대상 월 계산
        target_end = today.replace(day=1) - timedelta(days=1)
        for _ in range(months_ago - 1):
            target_end = target_end.replace(day=1) - timedelta(days=1)
        target_start = target_end.replace(day=1)

        month_label = target_start.strftime('%Y년 %m월')

        mask = (active['최초등록일자'] >= pd.Timestamp(target_start)) & \
               (active['최초등록일자'] <= pd.Timestamp(target_end))
        month_df = active[mask]

        # 전전월 (비교용)
        prev_end = target_start - timedelta(days=1)
        prev_start = prev_end.replace(day=1)
        prev_mask = (active['최초등록일자'] >= pd.Timestamp(prev_start)) & \
                    (active['최초등록일자'] <= pd.Timestamp(prev_end))
        prev_count = int(active[prev_mask].shape[0])

        # 변동률
        if prev_count > 0:
            change_pct = (len(month_df) - prev_count) / prev_count * 100
            change_str = f"+{change_pct:.1f}%" if change_pct >= 0 else f"{change_pct:.1f}%"
        else:
            change_str = "N/A"

        # 국가별
        countries = []
        for c in month_df['제조국가'].dropna():
            for cc in str(c).split('@'):
                countries.append(cc.strip())
        country_counts = Counter(countries).most_common(10)
        total_c = sum(dict(country_counts).values())
        country_list = [
            {"국가": c, "건수": n, "비율": f"{n/total_c*100:.1f}%"}
            for c, n in country_counts
        ]

        # 주요 신청인
        top_applicants = month_df.groupby('신청인').agg(
            건수=('등록번호', 'count')
        ).sort_values('건수', ascending=False).head(5)
        applicant_list = [
            {"신청인": name, "건수": int(row['건수'])}
            for name, row in top_applicants.iterrows()
        ]

        # 경쟁 성분 (동일 성분 다수 신청인)
        competition = month_df.groupby('성분명').agg(
            신청인수=('신청인', 'nunique'),
            신청인목록=('신청인', lambda x: ', '.join(x.unique())),
        ).query('신청인수 >= 2').sort_values('신청인수', ascending=False)
        competition_list = [
            {"성분명": name, "신청인수": int(row['신청인수']), "신청인": row['신청인목록']}
            for name, row in competition.head(10).iterrows()
        ]

        result = {
            "기간": month_label,
            "총건수": len(month_df),
            "최초등록": int((~month_df['is_허여']).sum()),
            "허여_변경": int(month_df['is_허여'].sum()),
            "전월대비_변동": change_str,
            "전월_건수": prev_count,
            "국가별_분포": country_list,
            "주요_신청인_TOP5": applicant_list,
            "경쟁_성분": competition_list
        }

        os.unlink(excel_path)
        return json.dumps(result, ensure_ascii=False, indent=2)

    except Exception as e:
        logger.error(f"월간 DMF 조회 실패: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool()
def search_dmf_by_ingredient(ingredient: str) -> str:
    """
    특정 성분명으로 DMF 등록 현황을 검색합니다.

    Args:
        ingredient: 검색할 성분명 (부분 일치, 예: "amoxicillin", "소라페닙")

    Returns:
        해당 성분의 전체 DMF 등록 이력
    """
    try:
        excel_path = _download_dmf_excel()
        active = _load_and_prepare(excel_path)

        mask = active['성분명'].astype(str).str.contains(ingredient, case=False, na=False)
        found = active[mask].sort_values('최초등록일자', ascending=False)

        if len(found) == 0:
            return json.dumps({
                "검색어": ingredient,
                "메시지": f"'{ingredient}' 관련 DMF 등록 내역을 찾을 수 없습니다."
            }, ensure_ascii=False)

        entries = []
        for _, row in found.iterrows():
            entries.append({
                "등록번호": str(row.get('등록번호', '')),
                "등록일": row['최초등록일자'].strftime('%Y-%m-%d') if pd.notna(row['최초등록일자']) else '',
                "등록유형": row['등록유형'],
                "성분명": str(row.get('성분명', '')),
                "신청인": str(row.get('신청인', '')),
                "제조소": str(row.get('제조소명', '')),
                "국가": str(row.get('제조국가', '')).replace('@', '/'),
                "연계심사": 'O' if row['has_연계심사'] else 'X',
                "상태": str(row.get('취소/취하구분', ''))
            })

        result = {
            "검색어": ingredient,
            "총_등록건수": len(found),
            "신청인_수": int(found['신청인'].nunique()),
            "제조국가_수": len(set(
                c.strip() for cs in found['제조국가'].dropna()
                for c in str(cs).split('@')
            )),
            "등록내역": entries[:30]  # 최대 30건
        }

        os.unlink(excel_path)
        return json.dumps(result, ensure_ascii=False, indent=2)

    except Exception as e:
        logger.error(f"성분 검색 실패: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool()
def search_dmf_by_country(country: str) -> str:
    """
    특정 국가의 DMF 등록 현황을 검색합니다.

    Args:
        country: 검색할 국가명 (부분 일치, 예: "인도", "중국", "India")

    Returns:
        해당 국가 제조사의 DMF 등록 현황 요약
    """
    try:
        excel_path = _download_dmf_excel()
        active = _load_and_prepare(excel_path)

        mask = active['제조국가'].astype(str).str.contains(country, case=False, na=False)
        found = active[mask].sort_values('최초등록일자', ascending=False)

        if len(found) == 0:
            return json.dumps({
                "검색_국가": country,
                "메시지": f"'{country}' 관련 DMF 등록 내역을 찾을 수 없습니다."
            }, ensure_ascii=False)

        # 최근 3개월 신규
        three_months_ago = datetime.today() - timedelta(days=90)
        recent = found[found['최초등록일자'] >= pd.Timestamp(three_months_ago)]

        # 주요 성분
        top_ingredients = found['성분명'].value_counts().head(10)
        ingredient_list = [
            {"성분명": name, "건수": int(cnt)}
            for name, cnt in top_ingredients.items()
        ]

        # 주요 제조소
        top_mfrs = found['제조소명'].value_counts().head(10)
        mfr_list = [
            {"제조소": name, "건수": int(cnt)}
            for name, cnt in top_mfrs.items()
        ]

        result = {
            "검색_국가": country,
            "전체_등록건수": len(found),
            "최근3개월_신규": len(recent),
            "주요_성분_TOP10": ingredient_list,
            "주요_제조소_TOP10": mfr_list
        }

        os.unlink(excel_path)
        return json.dumps(result, ensure_ascii=False, indent=2)

    except Exception as e:
        logger.error(f"국가 검색 실패: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool()
def get_dmf_chat_summary() -> str:
    """
    카카오톡/메신저에 바로 공유할 수 있는 간결한 DMF 요약 메시지를 생성합니다.
    전주 신규 등록 DMF를 한눈에 볼 수 있는 텍스트 형태로 반환합니다.

    Returns:
        복사해서 채팅방에 붙여넣기 가능한 요약 메시지
    """
    try:
        excel_path = _download_dmf_excel()
        active = _load_and_prepare(excel_path)

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
        lines.append(f"{'='*30}")

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

        os.unlink(excel_path)
        return "\n".join(lines)

    except Exception as e:
        logger.error(f"채팅 요약 생성 실패: {e}")
        return f"❌ 요약 생성 실패: {e}"


# ═══════════════════════════════════════════
# 서버 실행
# ═══════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    transport = os.environ.get("MCP_TRANSPORT", "sse")

    print(f"🚀 DMF Intelligence MCP Server 시작")
    print(f"   Transport: {transport}")
    print(f"   Port: {port}")

    mcp.run(transport=transport, port=port)
