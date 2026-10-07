"""뉴스 자동 분류 + 태그 추출 + 금융 감성 정밀 분석."""

import re

# ══════════════════════════════════════════
# 카테고리 분류
# ══════════════════════════════════════════

CATEGORIES = {
    "반도체/IT": ["반도체", "삼성전자", "SK하이닉스", "HBM", "AI반도체", "파운드리", "TSMC", "엔비디아", "GPU", "메모리", "DDR", "낸드", "시스템반도체"],
    "금리/환율": ["금리", "환율", "한국은행", "기준금리", "원달러", "원/달러", "통화정책", "CPI", "인플레이션", "디플레이션", "금통위"],
    "미국증시": ["나스닥", "S&P500", "다우", "월가", "미국증시", "뉴욕증시", "FOMC", "연준", "파월", "미국경제", "트럼프"],
    "에너지": ["에너지", "원유", "유가", "태양광", "풍력", "수소", "2차전지", "배터리", "LG에너지", "리튬", "니켈"],
    "자동차": ["자동차", "현대차", "기아", "테슬라", "전기차", "EV", "자율주행", "완성차", "하이브리드"],
    "바이오": ["바이오", "제약", "신약", "임상", "셀트리온", "삼성바이오", "FDA", "허가", "의약품"],
    "코스피/코스닥": ["코스피", "코스닥", "장마감", "장중", "주가지수", "외국인", "기관", "개인투자자", "수급"],
    "정책/규제": ["정책", "규제", "금감원", "금융위", "공매도", "양도세", "증권거래세", "대주주", "상장폐지"],
    "ETF": ["ETF", "KODEX", "TIGER", "ARIRANG", "SOL", "ACE", "인버스", "레버리지", "상장지수"],
}

SOURCE_CATEGORY = {
    "반도체_IT": "반도체/IT",
    "금리_환율": "금리/환율",
    "미국_증시": "미국증시",
    "코스피_코스닥": "코스피/코스닥",
    "증권_전체": "",
}

# ══════════════════════════════════════════
# 태그 추출
# ══════════════════════════════════════════

STOCK_NAMES = [
    "삼성전자", "SK하이닉스", "현대차", "기아", "LG에너지솔루션",
    "셀트리온", "카카오", "네이버", "삼성바이오", "포스코홀딩스",
    "LG화학", "삼성SDI", "NAVER", "현대모비스", "KB금융",
    "신한지주", "삼성물산", "LG전자", "한화에어로스페이스", "두산에너빌리티",
]

THEMES = [
    "AI", "HBM", "2차전지", "전기차", "배당", "공매도", "IPO", "M&A",
    "리츠", "원자력", "방산", "로봇", "양자컴퓨터", "메타버스", "우주항공",
]

# ══════════════════════════════════════════
# 금융 감성 정밀 분석
# ══════════════════════════════════════════

# 호재 패턴: (정규식, 점수, 근거)
# 점수: +1 약한 호재, +2 보통, +3 강한 호재
_BULLISH_PATTERNS = [
    # ── 실적/성장 ──
    (r"사상.?최[고대]", +3, "사상 최고치"),
    (r"역대.?최[고대]", +3, "역대 최고치"),
    (r"영업이익.{0,10}(증가|급증|성장|개선|흑자전환)", +3, "영업이익 개선"),
    (r"매출.{0,10}(증가|급증|성장|신기록)", +2, "매출 성장"),
    (r"흑자.?전환", +3, "흑자 전환"),
    (r"어닝.?서프라이즈", +3, "어닝 서프라이즈"),
    (r"호실적", +2, "호실적"),
    (r"깜짝.?실적", +2, "깜짝 실적"),

    # ── 주가/시장 ──
    (r"급등", +2, "급등"),
    (r"폭등", +3, "폭등"),
    (r"신고가", +3, "신고가"),
    (r"상승.?마감", +1, "상승 마감"),
    (r"상승.?출발", +1, "상승 출발"),
    (r"강세", +1, "강세"),
    (r"반등", +1, "반등"),
    (r"돌파", +2, "돌파"),
    (r"랠리", +2, "랠리"),
    (r"사흘.?연속.?상승", +2, "연속 상승"),
    (r"연속.?상승", +1, "연속 상승"),

    # ── 수급/투자 ──
    (r"외국인.{0,10}(매수|순매수|사들)", +2, "외국인 매수"),
    (r"기관.{0,10}(매수|순매수|사들)", +2, "기관 매수"),
    (r"대규모.?투자", +2, "대규모 투자"),
    (r"수주", +2, "수주"),
    (r"계약.?체결", +2, "계약 체결"),
    (r"공급.?계약", +2, "공급 계약"),

    # ── 정책/이벤트 ──
    (r"금리.?인하", +2, "금리 인하 (증시 호재)"),
    (r"부양.?책", +2, "경기 부양책"),
    (r"규제.?완화", +2, "규제 완화"),
    (r"세금.?감면|세제.?혜택", +1, "세제 혜택"),
    (r"상장.?승인|IPO.?승인", +1, "상장/IPO"),

    # ── 기술/성과 ──
    (r"양산.?본격", +2, "양산 본격화"),
    (r"개발.?성공", +2, "개발 성공"),
    (r"FDA.?승인|허가.?획득", +3, "규제 승인"),
    (r"임상.?성공|임상.?통과", +3, "임상 성공"),
    (r"특허.?취득", +1, "특허 취득"),

    # ── 배당/주주환원 ──
    (r"배당.?확대|배당.?인상", +2, "배당 확대"),
    (r"자사주.?매입|자사주.?소각", +2, "자사주 매입/소각"),
    (r"주주.?환원", +1, "주주 환원"),
]

# 악재 패턴: (정규식, 점수, 근거)
# 점수: -1 약한 악재, -2 보통, -3 강한 악재
_BEARISH_PATTERNS = [
    # ── 실적/재무 ──
    (r"영업이익.{0,10}(감소|급감|적자|반토막)", -3, "영업이익 악화"),
    (r"매출.{0,10}(감소|급감|하락)", -2, "매출 하락"),
    (r"적자.?전환", -3, "적자 전환"),
    (r"적자.?확대|적자.?지속", -2, "적자 지속"),
    (r"어닝.?쇼크", -3, "어닝 쇼크"),
    (r"실적.?쇼크|실적.?부진", -2, "실적 부진"),
    (r"부실", -2, "재무 부실"),

    # ── 주가/시장 ──
    (r"급락", -2, "급락"),
    (r"폭락", -3, "폭락"),
    (r"하락.?마감", -1, "하락 마감"),
    (r"하락.?출발", -1, "하락 출발"),
    (r"약세", -1, "약세"),
    (r"신저가", -3, "신저가"),
    (r"하한가", -3, "하한가"),
    (r"고평가|과열", -2, "고평가/과열"),
    (r"고점", -1, "고점 부담"),
    (r"경고|우려|불안|부담", -1, "경계 신호"),
    (r"혼조|차별화|흔들", -1, "방향성 약화"),
    (r"버블.{0,8}(우려|논란|경고)", -2, "버블 우려"),
    (r"사흘.?연속.?하락", -2, "연속 하락"),
    (r"연속.?하락", -1, "연속 하락"),
    (r"블랙.?먼데이|블랙.?스완", -3, "시장 충격"),
    (r"패닉|공포", -2, "시장 공포"),

    # ── 수급/매도 ──
    (r"외국인.{0,10}(매도|순매도|팔아)", -2, "외국인 매도"),
    (r"기관.{0,10}(매도|순매도|팔아)", -2, "기관 매도"),
    (r"투매|매도세", -2, "매도세"),
    (r"공매도.{0,5}(급증|폭증|증가)", -2, "공매도 증가"),

    # ── 정책/리스크 ──
    (r"금리.?인상", -2, "금리 인상 (증시 악재)"),
    (r"긴축", -2, "통화 긴축"),
    (r"규제.?강화", -2, "규제 강화"),
    (r"과징금|벌금|제재", -2, "제재/과징금"),
    (r"상장.?폐지", -3, "상장 폐지 위험"),
    (r"관세.{0,10}(부과|인상|폭탄)", -2, "관세 리스크"),

    # ── 위기/리스크 ──
    (r"위기", -2, "위기"),
    (r"리스크|불확실", -1, "리스크"),
    (r"전쟁|분쟁", -2, "지정학 리스크"),
    (r"갈등", -2, "갈등 리스크"),
    (r"의존도.?커", -1, "집중 리스크"),
    (r"파산|부도|디폴트", -3, "파산/부도 위험"),
    (r"리콜", -2, "리콜"),
    (r"소송|피소", -1, "법적 리스크"),
    (r"손실.?확대", -2, "손실 확대"),

    # ── 바이오 ──
    (r"임상.?실패|임상.?중단", -3, "임상 실패"),
    (r"FDA.?거부|FDA.?반려", -3, "FDA 거부"),
]

# 반전 패턴: 앞뒤 문맥에 따라 감성이 뒤집히는 경우
_NEGATION_WORDS = ["않", "안 ", "못 ", "없", "아닌", "부진", "우려", "불안", "불구"]
_INTENSIFIERS = ["매우", "크게", "대폭", "급격", "심각", "사상", "역대"]

# 감성 라벨 (5단계)
SENTIMENT_LABELS = {
    "strong_positive": "강한 호재",
    "positive": "호재",
    "neutral": "중립",
    "negative": "악재",
    "strong_negative": "강한 악재",
}


def classify(title: str, summary: str = "", source: str = "") -> str:
    """뉴스 카테고리 분류."""
    text = _strip(f"{title} {summary}")
    scores = {}
    for cat, keywords in CATEGORIES.items():
        score = sum(1 for kw in keywords if kw.lower() in text)
        if score > 0:
            scores[cat] = score
    if scores:
        return max(scores, key=scores.get)
    return SOURCE_CATEGORY.get(source, "기타") or "기타"


def extract_tags(title: str, summary: str = "") -> list[str]:
    """종목명, 테마 키워드 추출. 최대 8개."""
    text = f"{title} {summary}"
    tags = []
    for name in STOCK_NAMES:
        if name in text:
            tags.append(name)
    for theme in THEMES:
        if theme in text:
            tags.append(theme)
    return list(dict.fromkeys(tags))[:8]


def detect_sentiment(title: str, summary: str = "") -> str:
    """금융 뉴스 감성 정밀 분석. 5단계 판정.

    Returns:
        "strong_positive" | "positive" | "neutral" | "negative" | "strong_negative"
    """
    result = analyze_sentiment(title, summary)
    return result["sentiment"]


def analyze_sentiment(title: str, summary: str = "") -> dict:
    """감성 정밀 분석 — 점수, 판정, 근거, 영향도 반환.

    Returns:
        {
            "sentiment": "strong_positive"|"positive"|"neutral"|"negative"|"strong_negative",
            "score": float (-10 ~ +10),
            "impact": "high"|"medium"|"low",
            "reasons": ["사상 최고치", "외국인 매수"],
            "label": "강한 호재"
        }
    """
    text = f"{title} {summary}"
    clean = _strip(text)

    total_score = 0.0
    reasons = []

    # ── 호재 패턴 매칭 ──
    for pattern, score, reason in _BULLISH_PATTERNS:
        if re.search(pattern, text):
            # 부정어 반전 체크
            if _has_negation(text, pattern):
                total_score -= abs(score) * 0.7  # 반전: "상승 못 해" → 약한 악재
                reasons.append(f"{reason}(부정)")
            else:
                total_score += score
                reasons.append(reason)

    # ── 악재 패턴 매칭 ──
    for pattern, score, reason in _BEARISH_PATTERNS:
        if re.search(pattern, text):
            if _has_negation(text, pattern):
                total_score += abs(score) * 0.7  # 반전: "하락 없이" → 약한 호재
                reasons.append(f"{reason}(반전)")
            else:
                total_score += score  # score는 이미 음수
                reasons.append(reason)

    # ── 강도 증폭 ──
    intensifier_count = sum(1 for w in _INTENSIFIERS if w in text)
    if intensifier_count > 0 and total_score != 0:
        multiplier = 1 + intensifier_count * 0.2
        total_score *= multiplier

    # ── 숫자 패턴 보강 ──
    # "30% 급등" → 숫자가 크면 영향도 증가
    pct_match = re.search(r'(\d+(?:\.\d+)?)\s*%', text)
    if pct_match:
        pct = float(pct_match.group(1))
        if pct >= 10 and total_score > 0:
            total_score += 1  # 큰 수치 + 호재 → 보강
        elif pct >= 10 and total_score < 0:
            total_score -= 1  # 큰 수치 + 악재 → 보강

    # ── 판정 ──
    total_score = max(-10, min(10, total_score))

    if total_score >= 4:
        sentiment = "strong_positive"
    elif total_score >= 1.5:
        sentiment = "positive"
    elif total_score <= -4:
        sentiment = "strong_negative"
    elif total_score <= -1.5:
        sentiment = "negative"
    else:
        sentiment = "neutral"

    # ── 영향도 ──
    abs_score = abs(total_score)
    if abs_score >= 4:
        impact = "high"
    elif abs_score >= 2:
        impact = "medium"
    else:
        impact = "low"

    return {
        "sentiment": sentiment,
        "score": round(total_score, 1),
        "impact": impact,
        "reasons": reasons[:5],
        "label": SENTIMENT_LABELS.get(sentiment, "중립"),
    }


def _has_negation(text: str, pattern: str) -> bool:
    """패턴 앞에 부정어가 있는지 확인."""
    match = re.search(pattern, text)
    if not match:
        return False
    before = text[max(0, match.start() - 10):match.start()]
    return any(neg in before for neg in _NEGATION_WORDS)


def _strip(text: str) -> str:
    """HTML 제거 + 소문자."""
    return re.sub(r'<[^>]+>', '', text).lower()


def should_keep(sentiment_json: str) -> bool:
    """파기 전 보존 여부 판정. True면 kept=1로 마킹.

    보존 조건 (OR):
    - impact == "high"
    - |score| >= 3
    - sentiment in ("strong_positive", "strong_negative")
    """
    import json
    try:
        s = json.loads(sentiment_json) if isinstance(sentiment_json, str) else sentiment_json
    except (json.JSONDecodeError, TypeError):
        return False

    if s.get("impact") == "high":
        return True
    if abs(s.get("score", 0)) >= 3:
        return True
    if s.get("sentiment") in ("strong_positive", "strong_negative"):
        return True
    return False
