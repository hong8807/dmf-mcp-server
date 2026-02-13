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
    "loading": False,     # 로딩 중 여부
    "digest": None        # Gemini용 데이터 요약
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
        _cache["digest"] = _build_data_digest(df)
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


def _build_data_digest(df: pd.DataFrame) -> str:
    """Gemini 컨텍스트용 DMF 데이터 통계 요약 생성 (캐시 갱신 시 1회 호출)"""
    today = datetime.today()
    lines = []

    lines.append(f"[DMF 데이터 요약] 기준일: {today.strftime('%Y-%m-%d')}")
    lines.append(f"총 정상 DMF 등록건수: {len(df)}건")

    # 최초등록 vs 허여
    initial = int((~df['is_허여']).sum())
    change = int(df['is_허여'].sum())
    linked = int(df['has_연계심사'].sum())
    lines.append(f"최초등록: {initial}건 / 허여(변경): {change}건 / 연계심사: {linked}건")

    # 상위 성분 TOP 20
    top_ing = df['성분명'].value_counts().head(20)
    lines.append("\n[상위 성분 TOP 20]")
    for name, cnt in top_ing.items():
        lines.append(f"  {name}: {cnt}건")

    # 국가별 분포
    country_dist = Counter()
    for c in df['제조국가'].dropna():
        for cc in str(c).split('@'):
            country_dist[cc.strip()] += 1
    lines.append("\n[국가별 분포]")
    for country, cnt in country_dist.most_common(20):
        pct = cnt / len(df) * 100
        lines.append(f"  {country}: {cnt}건 ({pct:.1f}%)")

    # 상위 신청인 TOP 20
    top_app = df['신청인'].value_counts().head(20)
    lines.append("\n[상위 신청인 TOP 20]")
    for name, cnt in top_app.items():
        if name:
            lines.append(f"  {name}: {cnt}건")

    # 상위 제조소 TOP 20
    top_mfr = df['제조소명'].value_counts().head(20)
    lines.append("\n[상위 제조소 TOP 20]")
    for name, cnt in top_mfr.items():
        if name:
            lines.append(f"  {name}: {cnt}건")

    # 최근 12개월 월별 등록 추이
    lines.append("\n[월별 등록 추이 (최근 12개월)]")
    for i in range(12, 0, -1):
        m_end = today.replace(day=1) - timedelta(days=1)
        for _ in range(i - 1):
            m_end = m_end.replace(day=1) - timedelta(days=1)
        m_start = m_end.replace(day=1)
        mask = (df['최초등록일자'] >= pd.Timestamp(m_start)) & \
               (df['최초등록일자'] <= pd.Timestamp(m_end))
        cnt = int(mask.sum())
        if cnt > 0:
            lines.append(f"  {m_start.strftime('%Y-%m')}: {cnt}건")

    # 최근 7일 등록
    week_ago = today - timedelta(days=7)
    recent_mask = df['최초등록일자'] >= pd.Timestamp(week_ago)
    recent_cnt = int(recent_mask.sum())
    lines.append(f"\n[최근 7일 신규등록]: {recent_cnt}건")

    return "\n".join(lines)


def compare_countries(country_a: str, country_b: str) -> dict:
    """두 국가 DMF 등록 현황 비교"""
    try:
        active = _get_cached_data()
        today = datetime.today()
        three_months_ago = today - timedelta(days=90)

        results = {}
        for country in [country_a, country_b]:
            mask = active['제조국가'].astype(str).str.contains(country, case=False, na=False)
            found = active[mask]
            recent = found[found['최초등록일자'] >= pd.Timestamp(three_months_ago)]

            top_ing = found['성분명'].value_counts().head(5)
            top_mfr = found['제조소명'].value_counts().head(5)

            results[country] = {
                "전체_등록건수": len(found),
                "최근3개월_신규": len(recent),
                "최초등록": int((~found['is_허여']).sum()),
                "허여_변경": int(found['is_허여'].sum()),
                "연계심사": int(found['has_연계심사'].sum()),
                "주요_성분": [{"성분명": n, "건수": int(c)} for n, c in top_ing.items()],
                "주요_제조소": [{"제조소": n, "건수": int(c)} for n, c in top_mfr.items()]
            }

        return {
            "비교_국가": [country_a, country_b],
            country_a: results[country_a],
            country_b: results[country_b]
        }
    except Exception as e:
        logger.error(f"국가 비교 실패: {e}")
        raise


def get_top_rankings(category: str, top_n: int = 10, period_months: int = None) -> dict:
    """카테고리별 상위 랭킹 조회"""
    try:
        active = _get_cached_data()

        # 기간 필터
        if period_months:
            cutoff = datetime.today() - timedelta(days=period_months * 30)
            filtered = active[active['최초등록일자'] >= pd.Timestamp(cutoff)]
            period_label = f"최근 {period_months}개월"
        else:
            filtered = active
            period_label = "전체"

        col_map = {
            'ingredient': '성분명',
            'country': '제조국가',
            'applicant': '신청인',
            'manufacturer': '제조소명'
        }

        col = col_map.get(category)
        if not col:
            return {"error": f"지원하지 않는 카테고리: {category}. 가능: ingredient, country, applicant, manufacturer"}

        if category == 'country':
            counts = Counter()
            for c in filtered[col].dropna():
                for cc in str(c).split('@'):
                    cc = cc.strip()
                    if cc:
                        counts[cc] += 1
        else:
            counts = Counter()
            for val in filtered[col].dropna():
                val = str(val).strip()
                if val:
                    counts[val] += 1

        rankings = [
            {"순위": i + 1, "이름": name, "건수": cnt}
            for i, (name, cnt) in enumerate(counts.most_common(top_n))
        ]

        return {
            "카테고리": category,
            "기간": period_label,
            "총_대상수": len(counts),
            "상위_목록": rankings
        }
    except Exception as e:
        logger.error(f"랭킹 조회 실패: {e}")
        raise


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


def search_ingredient(ingredient: str, linked_filter: str = None) -> dict:
    """
    성분명으로 DMF 검색
    
    Args:
        ingredient: 검색 키워드 (부분 매칭)
        linked_filter: 'linked' = 연계심사 있는 것만, 'unlinked' = 없는 것만, None = 전체
    """
    try:
        active = _get_cached_data()

        mask = active['성분명'].astype(str).str.contains(ingredient, case=False, na=False)
        found = active[mask].sort_values('최초등록일자', ascending=False)

        if len(found) == 0:
            return {"검색어": ingredient, "메시지": f"'{ingredient}' 관련 DMF 등록 없음", "총건수": 0}

        found_copy = found.copy()
        found_copy['base_dmf'] = found_copy['등록번호'].astype(str).apply(
            lambda x: x.split('(')[0] if '(' in x else x
        )

        # 성분명별로 그룹핑 (동일 키워드라도 다른 성분은 분리)
        ingredient_groups = []
        total_mfr_count = 0
        total_linked_count = 0

        for ing_name, ing_group in found_copy.groupby('성분명'):
            # 이 성분의 제조원별 분석
            manufacturers = []
            for base, group in ing_group.groupby('base_dmf'):
                first_row = group[~group['is_허여']]
                if len(first_row) == 0:
                    first_row = group.iloc[:1]
                first_row = first_row.iloc[0]

                heo_count = int(group['is_허여'].sum())
                is_linked = bool(first_row['has_연계심사'])
                status = '정상' if (group['취소/취하구분'] == '정상').any() else '취소/취하'

                mfr_data = {
                    "base_dmf": base,
                    "제조소": str(first_row.get('제조소명', '')),
                    "국가": str(first_row.get('제조국가', '')).replace('@', '/'),
                    "신청인": str(first_row.get('신청인', '')),
                    "등록일": first_row['최초등록일자'].strftime('%Y-%m-%d') if pd.notna(first_row['최초등록일자']) else '',
                    "허여_수": heo_count,
                    "연계심사": is_linked,
                    "상태": status
                }

                # 필터 적용
                if linked_filter == 'linked' and not is_linked:
                    continue
                if linked_filter == 'unlinked' and is_linked:
                    continue

                manufacturers.append(mfr_data)

            if not manufacturers:
                continue

            linked_count = sum(1 for m in manufacturers if m['연계심사'])
            total_mfr_count += len(manufacturers)
            total_linked_count += linked_count

            # 국가별 분포
            country_dist = Counter()
            for m in manufacturers:
                main_country = m['국가'].split('/')[0]
                country_dist[main_country] += 1

            ingredient_groups.append({
                "성분명": str(ing_name),
                "제조원수": len(manufacturers),
                "연계심사_수": linked_count,
                "국가별_분포": [{"국가": k, "수": v} for k, v in country_dist.most_common()],
                "제조원_목록": manufacturers
            })

        if not ingredient_groups:
            filter_msg = "연계심사 등록된" if linked_filter == 'linked' else "연계심사 미등록"
            return {"검색어": ingredient, "메시지": f"'{ingredient}' 중 {filter_msg} 제조원이 없습니다.", "총건수": 0}

        return {
            "검색어": ingredient,
            "필터": linked_filter,
            "성분_종류수": len(ingredient_groups),
            "총_제조원수": total_mfr_count,
            "총_연계심사수": total_linked_count,
            "성분별_현황": ingredient_groups
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


def search_applicant(applicant: str, month: int = None) -> dict:
    """신청인별 DMF 검색"""
    try:
        active = _get_cached_data()

        mask = active['신청인'].astype(str).str.contains(applicant, case=False, na=False)
        found = active[mask].sort_values('최초등록일자', ascending=False)

        if len(found) == 0:
            return {"검색_신청인": applicant, "메시지": f"'{applicant}' 관련 DMF 등록 없음", "총건수": 0}

        # 월 필터
        if month:
            year = datetime.today().year
            found_month = found[
                (found['최초등록일자'].dt.month == month) &
                (found['최초등록일자'].dt.year == year)
            ]
            month_label = f"{year}년 {month}월"
        else:
            found_month = found
            month_label = "전체"

        # 성분별 현황
        ingredient_list = []
        for name, group in found_month.groupby('성분명'):
            group_copy = group.copy()
            group_copy['base_dmf'] = group_copy['등록번호'].astype(str).apply(
                lambda x: x.split('(')[0] if '(' in x else x
            )
            mfr_count = group_copy['base_dmf'].nunique()

            # 제조소 목록
            mfrs = []
            for base, bg in group_copy.groupby('base_dmf'):
                first = bg[~bg['is_허여']]
                if len(first) == 0:
                    first = bg.iloc[:1]
                first = first.iloc[0]
                mfrs.append({
                    "제조소": str(first.get('제조소명', '')),
                    "국가": str(first.get('제조국가', '')).replace('@', '/'),
                    "등록일": first['최초등록일자'].strftime('%Y-%m-%d') if pd.notna(first['최초등록일자']) else ''
                })

            ingredient_list.append({
                "성분명": str(name),
                "등록건수": len(group),
                "제조원수": mfr_count,
                "제조원": mfrs
            })

        # 제조국가 분포
        country_dist = Counter()
        for _, row in found_month.iterrows():
            main_country = str(row['제조국가']).split('@')[0]
            country_dist[main_country] += 1
        country_list = [{"국가": k, "건수": v} for k, v in country_dist.most_common()]

        return {
            "검색_신청인": applicant,
            "기간": month_label,
            "총_등록건수": len(found_month),
            "취급_성분수": len(ingredient_list),
            "국가별_분포": country_list,
            "성분별_현황": sorted(ingredient_list, key=lambda x: x['등록건수'], reverse=True)
        }
    except Exception as e:
        logger.error(f"신청인 검색 실패: {e}")
        raise


def search_manufacturer(keyword: str) -> dict:
    """제조소명으로 DMF 검색"""
    try:
        active = _get_cached_data()

        mask = active['제조소명'].astype(str).str.contains(keyword, case=False, na=False)
        found = active[mask].sort_values('최초등록일자', ascending=False)

        if len(found) == 0:
            return {"검색_제조소": keyword, "메시지": f"'{keyword}' 관련 제조소 없음", "총건수": 0}

        found_copy = found.copy()
        found_copy['base_dmf'] = found_copy['등록번호'].astype(str).apply(
            lambda x: x.split('(')[0] if '(' in x else x
        )

        # 성분별 현황
        ingredient_list = []
        for name, group in found_copy.groupby('성분명'):
            mfr_count = group['base_dmf'].nunique()
            linked_count = group[group['has_연계심사']]['base_dmf'].nunique()
            applicants = group['신청인'].unique().tolist()

            ingredient_list.append({
                "성분명": str(name),
                "제조원수": mfr_count,
                "연계심사_수": linked_count,
                "신청인": [a for a in applicants if a][:3]
            })

        # 국가 정보
        country_dist = Counter()
        for _, row in found_copy.drop_duplicates('base_dmf').iterrows():
            main_country = str(row['제조국가']).split('@')[0]
            country_dist[main_country] += 1

        return {
            "검색_제조소": keyword,
            "총_등록건수": len(found),
            "취급_성분수": len(ingredient_list),
            "국가별_분포": [{"국가": k, "건수": v} for k, v in country_dist.most_common()],
            "성분별_현황": sorted(ingredient_list, key=lambda x: x['제조원수'], reverse=True)
        }
    except Exception as e:
        logger.error(f"제조소 검색 실패: {e}")
        raise


def search_universal(keyword: str, month: int = None) -> tuple:
    """
    통합 검색: 성분명 → 신청인 → 제조소명 순서로 검색
    Returns: (search_type, data) 튜플
        search_type: 'ingredient' | 'applicant' | 'manufacturer' | 'none'
    """
    active = _get_cached_data()

    # 1순위: 성분명
    if active['성분명'].astype(str).str.contains(keyword, case=False, na=False).any():
        return ('ingredient', None)  # 기존 search_ingredient 사용

    # 2순위: 신청인
    if active['신청인'].astype(str).str.contains(keyword, case=False, na=False).any():
        return ('applicant', search_applicant(keyword, month))

    # 3순위: 제조소명
    if active['제조소명'].astype(str).str.contains(keyword, case=False, na=False).any():
        return ('manufacturer', search_manufacturer(keyword))

    return ('none', None)


def search_date_range(start_date, end_date) -> dict:
    """기간별 DMF 등록 현황 검색"""
    try:
        active = _get_cached_data()

        mask = (active['최초등록일자'] >= pd.Timestamp(start_date)) & \
               (active['최초등록일자'] <= pd.Timestamp(end_date))
        found = active[mask].sort_values('최초등록일자', ascending=False)

        period_label = f"{start_date.strftime('%m/%d')}~{end_date.strftime('%m/%d')}"

        if len(found) == 0:
            return {"기간": period_label, "메시지": f"{period_label} 기간 신규 DMF 등록 없음", "총건수": 0}

        initial = int((~found['is_허여']).sum())
        change = int(found['is_허여'].sum())
        linked = int(found['has_연계심사'].sum())

        # 국가별 분포
        country_dist = Counter()
        for _, row in found.iterrows():
            main_country = str(row['제조국가']).split('@')[0]
            country_dist[main_country] += 1
        country_list = [{"국가": k, "건수": v} for k, v in country_dist.most_common()]

        # 성분별 목록
        ingredient_list = []
        for name, group in found.groupby('성분명'):
            ingredient_list.append({
                "성분명": str(name),
                "건수": len(group),
                "신청인": group['신청인'].iloc[0] if len(group) > 0 else '',
                "제조소": group['제조소명'].iloc[0][:20] if len(group) > 0 else '',
                "국가": str(group['제조국가'].iloc[0]).split('@')[0] if len(group) > 0 else ''
            })
        ingredient_list.sort(key=lambda x: x['건수'], reverse=True)

        return {
            "기간": period_label,
            "총건수": len(found),
            "최초등록": initial,
            "허여": change,
            "연계심사": linked,
            "국가별_분포": country_list,
            "성분별_현황": ingredient_list
        }
    except Exception as e:
        logger.error(f"기간 검색 실패: {e}")
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
    """성분 검색 결과를 카카오톡 메시지 형태로 포맷 (성분명별 그룹핑)"""
    if data.get("총건수", 0) == 0 and data.get("총_제조원수", 0) == 0:
        return f"🔍 '{data['검색어']}' 검색 결과\n\n{data.get('메시지', '등록 없음')}"

    linked_filter = data.get("필터")
    filter_label = ""
    if linked_filter == 'linked':
        filter_label = " [연계심사 ✅]"
    elif linked_filter == 'unlinked':
        filter_label = " [미연계]"

    lines = [
        f"🔍 '{data['검색어']}' DMF 현황{filter_label}",
        f"{'─'*24}",
        f"📋 성분 {data['성분_종류수']}종 | 제조원 {data['총_제조원수']}개사 | 연계 {data['총_연계심사수']}개",
    ]

    # 성분별 상세
    for ig in data.get("성분별_현황", []):
        lines.append(f"\n{'━'*24}")
        lines.append(f"💊 {ig['성분명']}")

        # 국가별 분포
        dist = ig.get("국가별_분포", [])
        if dist:
            dist_str = " | ".join([f"{c['국가']} {c['수']}" for c in dist[:4]])
            lines.append(f"   🌍 {dist_str}")

        lines.append(f"   제조원 {ig['제조원수']}개 (연계 {ig['연계심사_수']}개)")

        # 제조원 목록 (전체 표시, 1줄로 압축)
        for m in ig.get("제조원_목록", []):
            linked_mark = "✅" if m['연계심사'] else "⬜"
            status_mark = "❌" if m['상태'] != '정상' else ""
            heo = f"+{m['허여_수']}허여" if m['허여_수'] > 0 else ""
            country = m['국가'].split('/')[0]  # 첫 번째 국가만
            lines.append(f"  {linked_mark} {m['제조소'][:20]} ({country})")
            lines.append(f"     {m['신청인'][:12]} {heo}{status_mark}")

    lines.append(f"\n{'─'*24}")
    lines.append("출처: 의약품안전나라")
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


def format_applicant_for_kakao(data: dict) -> str:
    """신청인 검색 결과를 카카오톡 메시지 형태로 포맷"""
    if data.get("총_등록건수", 0) == 0:
        return f"👤 '{data['검색_신청인']}' 검색 결과\n\n{data.get('메시지', '등록 없음')}"

    lines = [
        f"👤 '{data['검색_신청인']}' DMF 현황",
        f"   ({data['기간']})",
        f"{'─'*24}",
        f"📋 총 {data['총_등록건수']}건 | 취급 성분 {data['취급_성분수']}종",
    ]

    # 국가별 분포
    country_dist = data.get("국가별_분포", [])
    if country_dist:
        dist_str = " | ".join([f"{c['국가']} {c['건수']}" for c in country_dist[:4]])
        lines.append(f"🌍 {dist_str}")

    lines.append(f"{'─'*24}")

    # 성분별 현황
    ingredients = data.get("성분별_현황", [])
    if ingredients:
        for item in ingredients[:8]:
            lines.append(f"\n💊 {item['성분명'][:20]}")
            lines.append(f"   제조원 {item['제조원수']}개사")
            for mfr in item.get('제조원', [])[:3]:
                lines.append(f"   ▪ {mfr['제조소'][:22]} ({mfr['국가']})")

    if len(ingredients) > 8:
        lines.append(f"\n... 외 {len(ingredients) - 8}개 성분")

    lines.append("\n출처: 의약품안전나라")
    return "\n".join(lines)


def format_manufacturer_for_kakao(data: dict) -> str:
    """제조소 검색 결과를 카카오톡 메시지 형태로 포맷"""
    if data.get("총건수", 0) == 0:
        return f"🏭 '{data['검색_제조소']}' 검색 결과\n\n{data.get('메시지', '등록 없음')}"

    lines = [
        f"🏭 '{data['검색_제조소']}' 제조소 현황",
        f"{'─'*24}",
        f"📋 총 {data['총_등록건수']}건 | 취급 성분 {data['취급_성분수']}종",
    ]

    country_dist = data.get("국가별_분포", [])
    if country_dist:
        dist_str = " | ".join([f"{c['국가']} {c['건수']}" for c in country_dist[:4]])
        lines.append(f"🌍 {dist_str}")

    lines.append(f"{'─'*24}")

    ingredients = data.get("성분별_현황", [])
    if ingredients:
        for item in ingredients[:12]:
            linked_mark = f"✅{item['연계심사_수']}" if item['연계심사_수'] > 0 else "⬜0"
            apps = ", ".join(item.get('신청인', [])[:2])
            lines.append(f"💊 {item['성분명'][:20]}")
            lines.append(f"   제조원 {item['제조원수']}개 | 연계 {linked_mark} | {apps[:15]}")

    if len(ingredients) > 12:
        lines.append(f"\n... 외 {len(ingredients) - 12}개 성분")

    lines.append("\n출처: 의약품안전나라")
    return "\n".join(lines)


def format_date_range_for_kakao(data: dict) -> str:
    """기간별 검색 결과를 카카오톡 메시지 형태로 포맷"""
    if data.get("총건수", 0) == 0:
        return f"📅 {data['기간']} DMF 현황\n\n{data.get('메시지', '등록 없음')}"

    lines = [
        f"📅 {data['기간']} DMF 현황",
        f"{'─'*24}",
        f"총 {data['총건수']}건 (최초 {data['최초등록']} / 허여 {data['허여']})",
        f"연계심사 {data['연계심사']}건",
    ]

    country_dist = data.get("국가별_분포", [])
    if country_dist:
        lines.append(f"\n🌍 국가별:")
        for c in country_dist[:5]:
            lines.append(f"  {c['국가']}: {c['건수']}건")

    ingredients = data.get("성분별_현황", [])
    if ingredients:
        lines.append(f"\n💊 등록 성분:")
        for item in ingredients[:10]:
            lines.append(f"  {item['성분명'][:18]} ({item['국가']}) {item['신청인'][:8]}")

    if len(ingredients) > 10:
        lines.append(f"  ... 외 {len(ingredients) - 10}개")

    lines.append("\n출처: 의약품안전나라")
    return "\n".join(lines)


# ═══════════════════════════════════════════
# Gemini Function Calling 도구 정의
# ═══════════════════════════════════════════

GEMINI_TOOLS = [{
    "function_declarations": [
        {
            "name": "analyze_weekly_dmf",
            "description": "주간 DMF 등록 현황을 조회합니다. '주간', '이번주', '금주' 등의 요청에 사용합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "weeks_ago": {"type": "integer", "description": "몇 주 전 (1=지난주, 0=이번주)"}
                }
            }
        },
        {
            "name": "analyze_monthly_dmf",
            "description": "월간 DMF 등록 현황을 조회합니다. '월간', '이번달', '전월' 등의 요청에 사용합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "months_ago": {"type": "integer", "description": "몇 개월 전 (1=전월)"}
                }
            }
        },
        {
            "name": "search_ingredient",
            "description": "성분명으로 DMF 등록 현황을 검색합니다. 제조원, 연계심사 현황을 포함합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ingredient": {"type": "string", "description": "검색할 성분명 (부분 매칭)"},
                    "linked_filter": {"type": "string", "description": "연계심사 필터: linked(연계만), unlinked(미연계만), null(전체)", "enum": ["linked", "unlinked"]}
                },
                "required": ["ingredient"]
            }
        },
        {
            "name": "search_country",
            "description": "특정 국가의 DMF 등록 현황을 검색합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "country": {"type": "string", "description": "국가명 (한국어: 인도, 중국, 미국 등)"}
                },
                "required": ["country"]
            }
        },
        {
            "name": "search_applicant",
            "description": "신청인(수입사)별 DMF 등록 현황을 검색합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "applicant": {"type": "string", "description": "신청인명 (부분 매칭)"},
                    "month": {"type": "integer", "description": "특정 월 필터 (1-12, 선택)"}
                },
                "required": ["applicant"]
            }
        },
        {
            "name": "search_manufacturer",
            "description": "제조소명으로 DMF 등록 현황을 검색합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "제조소명 (부분 매칭)"}
                },
                "required": ["keyword"]
            }
        },
        {
            "name": "search_date_range",
            "description": "특정 기간 내 DMF 등록 현황을 검색합니다. '오늘', '어제', '최근 N일', '2월 9일부터 오늘까지' 등.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "시작일 (YYYY-MM-DD)"},
                    "end_date": {"type": "string", "description": "종료일 (YYYY-MM-DD)"}
                },
                "required": ["start_date", "end_date"]
            }
        },
        {
            "name": "compare_countries",
            "description": "두 국가의 DMF 등록 현황을 비교합니다. '인도 vs 중국', '인도와 중국 비교' 등.",
            "parameters": {
                "type": "object",
                "properties": {
                    "country_a": {"type": "string", "description": "첫 번째 국가명"},
                    "country_b": {"type": "string", "description": "두 번째 국가명"}
                },
                "required": ["country_a", "country_b"]
            }
        },
        {
            "name": "get_top_rankings",
            "description": "카테고리별 상위 랭킹을 조회합니다. '가장 많이 등록된 성분 TOP 10', '주요 국가 순위' 등.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "카테고리", "enum": ["ingredient", "country", "applicant", "manufacturer"]},
                    "top_n": {"type": "integer", "description": "상위 N개 (기본 10)"},
                    "period_months": {"type": "integer", "description": "최근 N개월 필터 (선택, 없으면 전체 기간)"}
                },
                "required": ["category"]
            }
        },
        {
            "name": "generate_chat_summary",
            "description": "카카오톡 공유용 간결한 DMF 주간 요약 텍스트를 생성합니다."
        }
    ]
}]


def _execute_gemini_function(name: str, args: dict) -> str:
    """Gemini가 요청한 함수를 실행하고 JSON 문자열로 반환"""
    try:
        if name == 'analyze_weekly_dmf':
            result = analyze_weekly_dmf(args.get('weeks_ago', 1))
        elif name == 'analyze_monthly_dmf':
            result = analyze_monthly_dmf(args.get('months_ago', 1))
        elif name == 'search_ingredient':
            result = search_ingredient(args['ingredient'], args.get('linked_filter'))
        elif name == 'search_country':
            result = search_country(args['country'])
        elif name == 'search_applicant':
            result = search_applicant(args['applicant'], args.get('month'))
        elif name == 'search_manufacturer':
            result = search_manufacturer(args['keyword'])
        elif name == 'search_date_range':
            start = datetime.strptime(args['start_date'], '%Y-%m-%d')
            end = datetime.strptime(args['end_date'], '%Y-%m-%d')
            result = search_date_range(start, end)
        elif name == 'compare_countries':
            result = compare_countries(args['country_a'], args['country_b'])
        elif name == 'get_top_rankings':
            result = get_top_rankings(args['category'], args.get('top_n', 10), args.get('period_months'))
        elif name == 'generate_chat_summary':
            return generate_chat_summary()
        else:
            return json.dumps({"error": f"알 수 없는 함수: {name}"}, ensure_ascii=False)
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def handle_with_gemini(utterance: str) -> Optional[str]:
    """
    Gemini 2.0 Flash Function Calling + 자연어 응답 생성

    Flow:
    1. 사용자 메시지 + 데이터 요약 + 도구 선언을 Gemini에 전송
    2. Gemini가 function_call 반환 → 로컬 실행 → 결과를 Gemini에 다시 전송
    3. Gemini가 자연어 응답 생성 → 반환
    4. 실패 시 None → regex fallback

    Returns: 카카오톡용 텍스트 응답 또는 None
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return None

    today = datetime.today()
    digest = _cache.get("digest", "데이터 로딩 중")

    system_prompt = f"""당신은 DMF Intelligence, 한국 의약품안전나라의 DMF(Drug Master File) 등록 데이터 전문 AI 어시스턴트입니다.

규칙:
1. 항상 한국어로 응답하세요.
2. 응답은 3500자 이내로 유지하세요 (카카오톡 제한).
3. 데이터 조회가 필요하면 제공된 도구(함수)를 호출하세요.
4. 일반적인 DMF 지식 질문은 도구 없이 직접 답변하세요.
5. 아래 데이터 요약을 참고하여 분석/비교/랭킹 질문에 답할 수 있습니다.
6. 이모지를 적절히 사용하되 과하지 않게 하세요.
7. 출처: 의약품안전나라를 응답 끝에 포함하세요.
8. 모르는 것은 솔직히 모른다고 하세요.

오늘 날짜: {today.strftime('%Y-%m-%d')}

데이터 요약:
{digest}"""

    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"

    try:
        # Step 1: 사용자 메시지 + 도구 전송
        resp = requests.post(api_url, json={
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": utterance}]}],
            "tools": GEMINI_TOOLS,
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 1000}
        }, timeout=3.5)

        if resp.status_code != 200:
            logger.warning(f"Gemini API 실패: {resp.status_code}")
            return None

        result = resp.json()
        candidate = result['candidates'][0]['content']
        parts = candidate.get('parts', [])

        if not parts:
            return None

        # Case A: Gemini가 직접 텍스트로 응답 (도구 호출 불필요)
        if 'text' in parts[0]:
            text = parts[0]['text'].strip()
            if len(text) > 3500:
                text = text[:3450] + "\n\n... (응답이 잘렸습니다)"
            logger.info(f"🤖 Gemini 직접 응답 ({len(text)}자)")
            return text

        # Case B: Gemini가 함수 호출 요청
        if 'functionCall' in parts[0]:
            fc = parts[0]['functionCall']
            fn_name = fc['name']
            fn_args = fc.get('args', {})
            logger.info(f"🔧 Gemini 함수 호출: {fn_name}({fn_args})")

            # 함수 실행
            fn_result = _execute_gemini_function(fn_name, fn_args)

            # Step 2: 함수 결과를 Gemini에 보내서 자연어 응답 생성
            resp2 = requests.post(api_url, json={
                "system_instruction": {"parts": [{"text": system_prompt}]},
                "contents": [
                    {"role": "user", "parts": [{"text": utterance}]},
                    {"role": "model", "parts": [{"functionCall": {"name": fn_name, "args": fn_args}}]},
                    {"role": "user", "parts": [{"functionResponse": {"name": fn_name, "response": {"result": fn_result}}}]}
                ],
                "tools": GEMINI_TOOLS,
                "generationConfig": {"temperature": 0.3, "maxOutputTokens": 1000}
            }, timeout=3.5)

            if resp2.status_code != 200:
                logger.warning(f"Gemini 2차 응답 실패: {resp2.status_code}")
                return None

            result2 = resp2.json()
            parts2 = result2['candidates'][0]['content'].get('parts', [])
            if parts2 and 'text' in parts2[0]:
                text = parts2[0]['text'].strip()
                if len(text) > 3500:
                    text = text[:3450] + "\n\n... (응답이 잘렸습니다)"
                logger.info(f"🤖 Gemini 함수 기반 응답 ({len(text)}자)")
                return text

        return None

    except requests.Timeout:
        logger.warning("Gemini API 타임아웃")
        return None
    except Exception as e:
        logger.warning(f"Gemini 대화 처리 실패: {e}")
        return None


def parse_user_intent(utterance: str) -> tuple:
    """
    사용자 발화를 분석하여 의도와 파라미터 추출

    Returns:
        (intent, params) 튜플
        intent: 'weekly' | 'monthly' | 'date_range' | 'ingredient' | 'country' | 'applicant' | 'summary' | 'help'
    """
    text = utterance.strip().lower()
    today = datetime.today()

    # ─── 1. 인사 / 도움말 ───
    if text in ['안녕', '하이', 'hi', 'hello', '시작', ''] or len(text) <= 1:
        return ('help', {})
    if any(kw in text for kw in ['도움', '사용법', '안내', '메뉴', '뭘 할 수', '기능', '명령', '뭐 할', '뭘 물', '어떻게']):
        return ('help', {})

    # ─── 2. 요약 ───
    if any(kw in text for kw in ['요약', '공유', '정리', '카톡', '챗']):
        return ('summary', {})

    # ─── 3. 날짜/기간 관련 검색 ───
    # "N월 N일부터 (오늘/N월 N일)까지" (구체적 범위 먼저!)
    range_match = re.search(r'(\d{1,2})월\s*(\d{1,2})일\s*부터\s*(?:오늘|(\d{1,2})월\s*(\d{1,2})일)\s*까지', text)
    if range_match:
        sm, sd = int(range_match.group(1)), int(range_match.group(2))
        start = datetime(today.year, sm, sd)
        if range_match.group(3):
            em, ed = int(range_match.group(3)), int(range_match.group(4))
            end = datetime(today.year, em, ed)
        else:
            end = today
        return ('date_range', {'start': start, 'end': end})

    # "N일부터 오늘까지" (월 생략)
    range_match2 = re.search(r'(\d{1,2})일\s*부터\s*오늘\s*까지', text)
    if range_match2:
        day = int(range_match2.group(1))
        month_ctx = re.search(r'(\d{1,2})월', text)
        m = int(month_ctx.group(1)) if month_ctx else today.month
        start = datetime(today.year, m, day)
        return ('date_range', {'start': start, 'end': today})

    # "최근 N일"
    recent_match = re.search(r'최근\s*(\d+)\s*일', text)
    if recent_match:
        days = int(recent_match.group(1))
        return ('date_range', {'start': today - timedelta(days=days), 'end': today})

    # "오늘 등록", "어제 현황" (일반적 오늘/어제)
    if re.search(r'오늘.*(등록|dmf|현황|신규)', text) or re.search(r'(등록|dmf|현황|신규).*오늘', text):
        return ('date_range', {'start': today, 'end': today})

    if re.search(r'어제.*(등록|dmf|현황|신규)', text) or re.search(r'(등록|dmf|현황|신규).*어제', text):
        yesterday = today - timedelta(days=1)
        return ('date_range', {'start': yesterday, 'end': yesterday})

    # "이번주", "주간", "금주"
    if any(kw in text for kw in ['주간', '이번주', '이번 주', '금주', '지난주', '지난 주', '주별']):
        return ('weekly', {})

    # "월간", "이번달"
    if any(kw in text for kw in ['월간', '이번달', '이번 달', '전월', '지난달', '지난 달', '월별']):
        return ('monthly', {})

    # ─── 4. 신청인 검색 ───
    month_match = re.search(r'(\d{1,2})월', text)
    month = int(month_match.group(1)) if month_match else None

    applicant_match = re.search(r'(?:신청인|수입사|수입업체|거래처)\s*[:]?\s*(.+?)(?:\s*(?:현황|검색|조회|dmf|등록|몇개|갯수).*$|\s*\??\s*$)', text)
    if applicant_match:
        name = applicant_match.group(1).strip()
        if name:
            return ('applicant', {'applicant': name, 'month': month})

    # ─── 5. 국가 검색 ───
    country_keywords = ['인도', '중국', '일본', '미국', '독일', '이탈리아', '스페인',
                        '프랑스', '영국', '캐나다', '브라질', '대만', '한국', '이스라엘']
    for kw in country_keywords:
        if kw in text:
            return ('country', {'country': kw})

    # ─── 6. 성분/통합 검색 ───
    linked_filter = None
    if any(kw in text for kw in ['연계 안', '미연계', '연계안', '비연계', '연계 없']):
        linked_filter = 'unlinked'
    elif any(kw in text for kw in ['연계심사', '연계', 'linked']):
        linked_filter = 'linked'

    # 검색 키워드 추출 (모든 불필요 단어 제거)
    clean_text = re.sub(
        r'(?:연계심사|미연계|비연계|연계|제조원|제조사|현황|검색|조회|dmf|등록|허여|신규|'
        r'된|안된|있는|없는|몇개|갯수|수|알려줘|보여줘|뭐야|좀|해줘|'
        r'부터|까지|오늘|어제|최근|기간|중에서|중|에서|의|에)',
        ' ', text
    ).strip()
    clean_text = re.sub(r'\d{1,2}월\s*(?:\d{1,2}일)?\s*(?:에|의|은|는)?\s*', '', clean_text).strip()
    clean_text = re.sub(r'\d{1,2}일', '', clean_text).strip()
    clean_text = re.sub(r'\s+', ' ', clean_text).strip()
    clean_text = re.sub(r'[?？!]$', '', clean_text).strip()
    clean_text = re.sub(r'(?:은|는|이|가|을|를)$', '', clean_text).strip()

    if clean_text and clean_text not in ['안녕', '하이', 'hi', 'hello', '시작'] and len(clean_text) > 1:
        return ('ingredient', {'ingredient': clean_text, 'linked_filter': linked_filter, 'month': month})

    # ─── 7. 위 모든 것에 해당 안 되면 ───
    # 날짜 관련 단어만 있었으면 → 주간 현황으로
    if any(kw in text for kw in ['등록', '현황', 'dmf', '신규']):
        return ('weekly', {})

    return ('help', {})


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

        # ═══════════════════════════════════════
        # 3-Tier 라우팅
        # ═══════════════════════════════════════

        # ─── Tier 1: Regex 빠른 경로 (<100ms) ───
        intent, extracted = parse_user_intent(utterance)
        logger.info(f"📏 Regex: intent={intent}, params={extracted}")

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

        elif intent == 'date_range':
            start = extracted.get('start', datetime.today())
            end = extracted.get('end', datetime.today())
            data = search_date_range(start, end)
            text = format_date_range_for_kakao(data)
            return JSONResponse(kakao_quick_replies(text, [
                {"messageText": "주간", "action": "message", "label": "📋 주간 현황"},
                {"messageText": "월간", "action": "message", "label": "📊 월간 리포트"},
                {"messageText": "도움", "action": "message", "label": "❓ 메뉴"}
            ]))

        elif intent == 'country':
            country = extracted.get('country', params.get('country', ''))
            if not country:
                return JSONResponse(kakao_simple_text("어느 국가의 DMF를 검색할까요?\n\n예: 인도, 중국, 일본, 미국"))
            data = search_country(country)
            text = format_country_for_kakao(data)
            return JSONResponse(kakao_quick_replies(text, [
                {"messageText": "주간", "action": "message", "label": "📋 주간 현황"},
                {"messageText": "월간", "action": "message", "label": "📊 월간 리포트"},
                {"messageText": "도움", "action": "message", "label": "❓ 메뉴"}
            ]))

        elif intent == 'applicant':
            applicant = extracted.get('applicant', params.get('applicant', ''))
            month = extracted.get('month')
            if not applicant:
                return JSONResponse(kakao_simple_text("검색할 신청인명을 입력해주세요.\n\n예: 신청인 휴시드\n예: 1월에 신청인 국전약품 현황"))
            data = search_applicant(applicant, month)
            text = format_applicant_for_kakao(data)
            return JSONResponse(kakao_quick_replies(text, [
                {"messageText": "주간", "action": "message", "label": "📋 주간 현황"},
                {"messageText": "월간", "action": "message", "label": "📊 월간 리포트"},
                {"messageText": "도움", "action": "message", "label": "❓ 메뉴"}
            ]))

        elif intent == 'ingredient':
            keyword = extracted.get('ingredient', params.get('ingredient', ''))
            linked_filter = extracted.get('linked_filter')
            if not keyword:
                return JSONResponse(kakao_simple_text("검색어를 입력해주세요.\n\n예: 클래리, Synthimed, 휴시드"))

            # 연계 필터가 있으면 성분명 검색 고정
            if linked_filter:
                data = search_ingredient(keyword, linked_filter)
                text = format_ingredient_for_kakao(data)
                replies = []
                if linked_filter != 'linked':
                    replies.append({"messageText": f"{keyword} 연계심사", "action": "message", "label": "✅ 연계심사만"})
                if linked_filter != 'unlinked':
                    replies.append({"messageText": f"{keyword} 미연계", "action": "message", "label": "⬜ 미연계만"})
                replies.append({"messageText": keyword, "action": "message", "label": "📋 전체 보기"})
                replies.append({"messageText": "도움", "action": "message", "label": "❓ 메뉴"})
                return JSONResponse(kakao_quick_replies(text, replies[:4]))

            # 통합 검색: 성분명 → 신청인 → 제조소명
            month = extracted.get('month')
            search_type, uni_data = search_universal(keyword, month)

            if search_type == 'ingredient':
                data = search_ingredient(keyword)
                text = format_ingredient_for_kakao(data)
                return JSONResponse(kakao_quick_replies(text, [
                    {"messageText": f"{keyword} 연계심사", "action": "message", "label": "✅ 연계심사만"},
                    {"messageText": f"{keyword} 미연계", "action": "message", "label": "⬜ 미연계만"},
                    {"messageText": "도움", "action": "message", "label": "❓ 메뉴"}
                ]))

            elif search_type == 'applicant':
                text = format_applicant_for_kakao(uni_data)
                return JSONResponse(kakao_quick_replies(text, [
                    {"messageText": "주간", "action": "message", "label": "📋 주간 현황"},
                    {"messageText": "월간", "action": "message", "label": "📊 월간 리포트"},
                    {"messageText": "도움", "action": "message", "label": "❓ 메뉴"}
                ]))

            elif search_type == 'manufacturer':
                text = format_manufacturer_for_kakao(uni_data)
                return JSONResponse(kakao_quick_replies(text, [
                    {"messageText": "주간", "action": "message", "label": "📋 주간 현황"},
                    {"messageText": "월간", "action": "message", "label": "📊 월간 리포트"},
                    {"messageText": "도움", "action": "message", "label": "❓ 메뉴"}
                ]))

            else:
                # 통합 검색 실패 → Tier 2/3으로 전달
                pass

        # ─── Tier 2/3: Gemini 대화형 (intent == 'help' 또는 검색 실패) ───
        # Gemini Function Calling + 자연어 응답 생성
        gemini_response = handle_with_gemini(utterance)
        if gemini_response:
            logger.info(f"🤖 Gemini 대화형 응답 사용")
            return JSONResponse(kakao_quick_replies(gemini_response, [
                {"messageText": "주간", "action": "message", "label": "📋 주간 현황"},
                {"messageText": "월간", "action": "message", "label": "📊 월간 리포트"},
                {"messageText": "최근 3일", "action": "message", "label": "📅 최근 3일"},
                {"messageText": "도움", "action": "message", "label": "❓ 메뉴"}
            ]))

        # ─── Fallback: Gemini도 실패하면 도움말 ───
        help_text = (
            "💊 DMF Intelligence\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "의약품안전나라 DMF 데이터를\n"
            "실시간으로 조회·분석합니다.\n\n"
            "아래 버튼을 누르거나 직접 입력하세요!\n\n"
            "💡 입력 예시:\n"
            "• 세파클러 → 제조원 현황\n"
            "• 세파클러 연계심사 → 연계 제조원만\n"
            "• 휴시드 → 신청인 검색\n"
            "• 인도 → 국가별 DMF 현황\n"
            "• 인도 vs 중국 → 국가 비교\n"
            "• 성분 랭킹 → TOP 10 성분\n"
            "• 2월9일부터 오늘까지 → 기간 검색\n"
            "• DMF가 뭐야? → 무엇이든 질문\n"
            "• 최근 3일 → 최근 등록 현황"
        )
        return JSONResponse(kakao_quick_replies(help_text, [
            {"messageText": "주간", "action": "message", "label": "📋 주간 현황"},
            {"messageText": "월간", "action": "message", "label": "📊 월간 리포트"},
            {"messageText": "최근 3일", "action": "message", "label": "📅 최근 3일"},
            {"messageText": "인도", "action": "message", "label": "🇮🇳 인도 DMF"}
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

