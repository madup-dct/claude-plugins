#!/usr/bin/env python3
"""
geo_audit.py — 웹페이지 GEO(생성형 AI 검색 최적화) 실측 점검기

표준 라이브러리만 사용한다. 설치·API 키 불필요.

  python3 geo_audit.py <URL> [--json] [--timeout 20] [--no-bots]

점검 항목
  1. AI 크롤러 접근      : 주요 봇 UA로 실제 GET → 상태코드/바이트
  2. robots.txt          : allowlist 구조 파싱 → 봇별 Allow/Disallow 판정
  3. 구조화 데이터       : JSON-LD 개수·@type·필수 필드
  4. 텍스트 밀도         : HTML 바이트 대비 순수 텍스트 비율
  5. head 신호           : title / description / canonical / og / robots meta
  6. 텍스트 없는 정보    : 이미지로만 존재하는 dt/dd 라벨 (등급·배지류)
  7. 한글 인명 공백      : `이 동욱` 형태 탐지
  8. 렌더 의존도         : SSR 여부 추정 (noscript·본문 텍스트 유무)
"""

import argparse, gzip, io, json, re, sys, urllib.error, urllib.parse, urllib.request
from html import unescape

# ---------------------------------------------------------------- 상수

AI_BOTS = [
    ("GPTBot",              "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; GPTBot/1.2; +https://openai.com/gptbot"),
    ("OAI-SearchBot",       "Mozilla/5.0 (compatible; OAI-SearchBot/1.0; +https://openai.com/searchbot)"),
    ("ChatGPT-User",        "Mozilla/5.0 (compatible; ChatGPT-User/1.0; +https://openai.com/bot)"),
    ("ClaudeBot",           "Mozilla/5.0 (compatible; ClaudeBot/1.0; +claudebot@anthropic.com)"),
    ("Claude-SearchBot",    "Mozilla/5.0 (compatible; Claude-SearchBot/1.0; +claudebot@anthropic.com)"),
    ("PerplexityBot",       "Mozilla/5.0 (compatible; PerplexityBot/1.0; +https://perplexity.ai/perplexitybot)"),
    ("Perplexity-User",     "Mozilla/5.0 (compatible; Perplexity-User/1.0; +https://perplexity.ai/perplexity-user)"),
    ("Google-Extended",     "Mozilla/5.0 (compatible; Google-Extended)"),
    ("Googlebot",           "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"),
    ("Bingbot",             "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)"),
    ("Applebot-Extended",   "Mozilla/5.0 (compatible; Applebot-Extended/1.0)"),
    ("CCBot",               "CCBot/2.0 (https://commoncrawl.org/faq/)"),
    ("Amazonbot",           "Mozilla/5.0 (compatible; Amazonbot/0.1; +https://developer.amazon.com/support/amazonbot)"),
    ("Meta-ExternalAgent",  "meta-externalagent/1.1 (+https://developers.facebook.com/docs/sharing/webmasters/crawler)"),
    ("Bytespider",          "Mozilla/5.0 (compatible; Bytespider; spider-feedback@bytedance.com)"),
    ("DuckAssistBot",       "Mozilla/5.0 (compatible; DuckAssistBot/1.0; +https://duckduckgo.com/duckassistbot)"),
    ("YouBot",              "Mozilla/5.0 (compatible; YouBot (+http://www.you.com))"),
    ("MistralAI-User",      "Mozilla/5.0 (compatible; MistralAI-User/1.0)"),
]

# robots.txt allowlist 점검 대상 (실제 GET 없이 규칙만 판정)
ROBOTS_CHECK = [b for b, _ in AI_BOTS] + [
    "Google-CloudVertexBot", "cohere-ai", "AI2Bot", "Diffbot",
    "Timpibot", "Webzio-Extended", "ImagesiftBot", "GrokBot",
    "DeepSeekBot", "NaverBot", "Yeti",
]

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# GEO에서 인용 가치가 높은 schema.org 타입
VALUABLE_TYPES = {
    "Article", "NewsArticle", "BlogPosting", "Product", "FAQPage", "HowTo",
    "TVSeries", "Movie", "Recipe", "Event", "Organization", "LocalBusiness",
    "Person", "Course", "JobPosting", "SoftwareApplication", "Book", "Review",
    "QAPage", "Dataset", "MedicalWebPage", "VideoObject",
}

# ---------------------------------------------------------------- 유틸


def fetch(url, ua, timeout=20):
    """(status, body_bytes, headers, error) 반환. 예외를 삼키고 튜플로 돌려준다."""
    req = urllib.request.Request(url, headers={
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Encoding": "gzip",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                try:
                    raw = gzip.decompress(raw)
                except Exception:
                    pass
            return r.status, raw, dict(r.headers), None
    except urllib.error.HTTPError as e:
        return e.code, b"", dict(getattr(e, "headers", {}) or {}), None
    except Exception as e:
        return None, b"", {}, f"{type(e).__name__}: {e}"


def visible_text(html_str):
    body = re.sub(r"(?is)<(script|style|noscript|template)[^>]*>.*?</\1>", " ", html_str)
    txt = unescape(re.sub(r"(?s)<[^>]+>", " ", body))
    return re.sub(r"\s+", " ", txt).strip()


# ---------------------------------------------------------------- robots.txt


def parse_robots(text):
    """연속 User-agent 줄을 하나의 그룹으로 묶어 [(agents, rules)] 반환."""
    groups, agents, rules, collecting = [], [], [], False
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field, value = field.strip().lower(), value.strip()
        if field == "user-agent":
            if collecting:                       # 규칙 뒤 새 UA → 이전 그룹 확정
                groups.append((agents, rules))
                agents, rules, collecting = [], [], False
            agents.append(value)
        elif field in ("allow", "disallow"):
            collecting = True
            rules.append((field, value))
    if agents or rules:
        groups.append((agents, rules))
    return groups


def _match_len(pattern, path):
    """robots 경로 패턴(* $ 지원) 매칭 → 매칭 길이 또는 -1"""
    if pattern == "":
        return -1
    rx = "".join(
        ".*" if c == "*" else ("$" if (c == "$" and i == len(pattern) - 1) else re.escape(c))
        for i, c in enumerate(pattern)
    )
    m = re.match(rx, path)
    return len(pattern.replace("*", "").replace("$", "")) if m else -1


def robots_verdict(groups, bot, path):
    """Google 규칙: 이름 매칭 그룹 하나만 적용, 없으면 '*'. 최장 매칭 우선, 동률은 Allow."""
    chosen, best = None, -1
    for agents, rules in groups:
        for a in agents:
            al = a.lower()
            if al == "*":
                continue
            if al in bot.lower() or bot.lower() in al:
                if len(al) > best:
                    chosen, best = rules, len(al)
    matched_by_name = chosen is not None
    if chosen is None:
        for agents, rules in groups:
            if any(a == "*" for a in agents):
                chosen = rules
                break
    if chosen is None:
        return "ALLOW", "규칙 없음", matched_by_name

    best_allow = max([_match_len(v, path) for f, v in chosen if f == "allow"] or [-1])
    best_dis = max([_match_len(v, path) for f, v in chosen if f == "disallow"] or [-1])
    if best_dis < 0 and best_allow < 0:
        return "ALLOW", "매칭 규칙 없음", matched_by_name
    if best_allow >= best_dis:
        return "ALLOW", f"Allow({best_allow}) ≥ Disallow({best_dis})", matched_by_name
    return "DISALLOW", f"Disallow({best_dis}) > Allow({best_allow})", matched_by_name


# ---------------------------------------------------------------- 점검 항목


def check_jsonld(html_str):
    blocks, types, errors, items = [], [], [], []
    for m in re.finditer(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html_str, re.S | re.I,
    ):
        raw = m.group(1).strip()
        blocks.append(raw)
        try:
            data = json.loads(raw)
        except Exception as e:
            errors.append(str(e)[:120])
            continue

        def walk(o):
            if isinstance(o, dict):
                t = o.get("@type")
                for tv in (t if isinstance(t, list) else [t]):
                    if tv:
                        types.append(tv)
                        items.append(o)
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        walk(data)

    valuable = sorted({t for t in types if t in VALUABLE_TYPES})
    return {
        "count": len(blocks),
        "types": sorted(set(types)),
        "valuable_types": valuable,
        "parse_errors": errors,
        "top_item_keys": sorted(items[0].keys()) if items else [],
    }


def check_head(html_str):
    def meta(pattern):
        m = re.search(pattern, html_str, re.I)
        return unescape(m.group(1)).strip() if m else None

    title = meta(r"<title[^>]*>(.*?)</title>")
    return {
        "title": title,
        "title_len": len(title) if title else 0,
        "description": meta(r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']'),
        "og_title": meta(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']'),
        "og_description": meta(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']'),
        "og_locale": meta(r'<meta[^>]+property=["\']og:locale["\'][^>]+content=["\'](.*?)["\']'),
        "canonical": meta(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\'](.*?)["\']'),
        "robots_meta": meta(r'<meta[^>]+name=["\']robots["\'][^>]+content=["\'](.*?)["\']'),
        "h1": [unescape(re.sub(r"<[^>]+>", "", x)).strip()[:120]
               for x in re.findall(r"<h1[^>]*>(.*?)</h1>", html_str, re.S | re.I)][:5],
        "hreflang_count": len(re.findall(r"hreflang=", html_str, re.I)),
    }


def check_image_only_labels(html_str):
    """<dt>라벨</dt><dd>…<img|picture…</dd> 처럼 값이 이미지뿐인 항목 탐지."""
    hits = []
    for m in re.finditer(r"<dt[^>]*>(.*?)</dt>\s*(<dd[^>]*>.*?</dd>)", html_str, re.S | re.I):
        label = unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
        dd = m.group(2)
        dd_text = unescape(re.sub(r"<[^>]+>", "", dd)).strip()
        has_img = bool(re.search(r"<(img|picture|svg)\b", dd, re.I))
        if has_img and len(dd_text) < 2 and label:
            alt = re.search(r'(?:alt|title)=["\']([^"\']+)["\']', dd, re.I)
            hits.append({"label": label[:40], "image_hint": alt.group(1)[:40] if alt else None})
    return hits


# 한국 성씨 상위 집합 (인구 대부분을 덮는 닫힌 집합). 관형사 오탐을 막는 1차 필터.
KOREAN_SURNAMES = set(
    "김 이 박 최 정 강 조 윤 장 임 한 오 서 신 권 황 안 송 류 유 홍 전 고 문 손 양 배 백 "
    "허 남 심 노 하 곽 성 차 주 우 구 민 진 지 엄 채 원 천 방 공 현 함 변 염 여 추 도 소 "
    "석 선 설 마 길 위 표 명 기 반 왕 금 옥 육 인 맹 제 모 남궁 탁 국 어 은 편 용".split()
)

# `이 문서`처럼 관형사+명사로 읽히는 조합을 걸러내기 위한 흔한 후행 명사
_COMMON_NOUNS = set(
    "문서 주제 메뉴 만들기 사람 경우 때문 부분 내용 정보 목록 이름 방법 상태 결과 "
    "화면 페이지 항목 기능 설정 파일 폴더 링크 버튼 그림 사진 표시 위치 시간 날짜 "
    "번호 순서 종류 단계 과정 기준 조건 이유 목적 대상 범위 수준 관련 이상 이하 "
    "이후 이전 다음 마지막 처음 전체 일부 기타 등등".split()
)


def check_korean_name_spacing(html_str):
    """`>이 동욱<` 처럼 성-이름이 공백으로 갈린 한글 인명 탐지.

    성씨 화이트리스트 + 흔한 명사 제외로 `새 주제`·`이 문서` 류 오탐을 막는다.
    """
    hits = set()
    # 복성(남궁 등)과 외자 이름(남궁 민)을 함께 잡되, 조사가 붙은 명사구는 제외한다.
    for sur, given in re.findall(r">\s*([가-힣]{1,2})\s([가-힣]{1,3})\s*<", html_str):
        if sur not in KOREAN_SURNAMES:
            continue
        # 조사(는/은/이/가/을/를/의/에/도/만)를 떼고 일반명사인지 본다
        stem = re.sub(r"(는|은|이|가|을|를|의|에|도|만|와|과|로|으로)$", "", given)
        if given in _COMMON_NOUNS or stem in _COMMON_NOUNS:
            continue
        if len(given) == 3 and given.endswith(("하기", "되기", "지기", "들기", "기")):
            continue          # `책 만들기` 류 동명사
        if len(sur) == 1 and len(given) == 1:
            continue          # `이 것` 등 한 글자+한 글자는 인명 근거가 약함
        hits.add(f"{sur} {given}")
    return sorted(hits)


def check_render_dependency(html_str, text_len):
    root_only = bool(re.search(r'<div[^>]+id=["\'](root|__next|app)["\'][^>]*>\s*</div>', html_str, re.I))
    return {
        "likely_csr_shell": root_only or text_len < 200,
        "empty_root_div": root_only,
        "noscript_present": bool(re.search(r"<noscript\b", html_str, re.I)),
    }


# ---------------------------------------------------------------- 판정


def build_findings(r):
    """점검 결과 → 심각도별 지적 목록"""
    f = []
    d = r["density"]
    j = r["jsonld"]
    h = r["head"]

    if j["count"] == 0:
        f.append(("CRITICAL", "structured-data",
                  "JSON-LD 구조화 데이터가 0개입니다. AI가 사실을 귀속할 근거가 없습니다."))
    elif not j["valuable_types"]:
        f.append(("HIGH", "structured-data",
                  f"JSON-LD는 있으나 인용 가치가 큰 타입이 없습니다 (현재: {', '.join(j['types'][:5]) or '없음'})."))
    if j["parse_errors"]:
        f.append(("HIGH", "structured-data",
                  f"JSON-LD 파싱 오류 {len(j['parse_errors'])}건 — 크롤러가 읽지 못합니다."))

    if d["ratio_pct"] < 0.5:
        f.append(("CRITICAL", "text-density",
                  f"텍스트 밀도 {d['ratio_pct']}% (텍스트 {d['text_chars']:,}자 / HTML {d['html_bytes']:,}B). "
                  "AI가 읽는 비용 대비 건질 내용이 거의 없습니다."))
    elif d["ratio_pct"] < 1.0:
        f.append(("HIGH", "text-density", f"텍스트 밀도 {d['ratio_pct']}%로 낮습니다 (권장 1.0% 이상)."))
    if d["text_chars"] < 1000:
        f.append(("HIGH", "thin-content",
                  f"순수 텍스트가 {d['text_chars']:,}자뿐입니다. 인용될 만한 고유 정보가 부족합니다."))

    if r["render"]["likely_csr_shell"]:
        f.append(("CRITICAL", "rendering",
                  "서버 렌더 본문이 거의 없습니다(CSR 셸 추정). 다수 AI 크롤러는 JS를 실행하지 않습니다."))

    blocked = [b for b, v in r["robots"]["verdicts"].items() if v["verdict"] == "DISALLOW"]
    if blocked:
        sev = "CRITICAL" if len(blocked) > 6 else "HIGH"
        f.append((sev, "robots",
                  f"robots.txt가 AI 봇 {len(blocked)}종을 차단합니다: {', '.join(blocked[:8])}"
                  + (" 외" if len(blocked) > 8 else "")))

    failed = [b for b, v in r["bots"].items() if v.get("status") not in (200, 301, 302, None)]
    if failed:
        f.append(("HIGH", "crawler-access",
                  f"실제 접근 실패 봇: {', '.join(f'{b}({r[chr(98)+chr(111)+chr(116)+chr(115)][b][chr(115)+chr(116)+chr(97)+chr(116)+chr(117)+chr(115)]})' for b in failed[:6])}"))

    if not h["title"]:
        f.append(("HIGH", "head", "title 태그가 없습니다."))
    if not h["description"]:
        f.append(("MEDIUM", "head", "meta description이 없습니다. AI 요약의 1차 재료가 빠집니다."))
    if not h["canonical"]:
        f.append(("MEDIUM", "head", "canonical이 없습니다. 중복 URL 간 대표본이 불명확합니다."))
    if h["robots_meta"] and re.search(r"noindex", h["robots_meta"], re.I):
        f.append(("CRITICAL", "head", f'robots meta에 noindex가 있습니다: "{h["robots_meta"]}"'))
    if not h["h1"]:
        f.append(("MEDIUM", "head", "h1이 없습니다. 페이지 주제어가 구조적으로 드러나지 않습니다."))

    if r["image_only_labels"]:
        labels = ", ".join(x["label"] for x in r["image_only_labels"][:5])
        f.append(("HIGH", "image-only-fact",
                  f"값이 이미지로만 존재하는 항목 {len(r['image_only_labels'])}건 ({labels}). "
                  "화면엔 보이지만 텍스트 레이어에는 없습니다."))

    if r["korean_name_spacing"]:
        names = ", ".join(r["korean_name_spacing"][:5])
        f.append(("HIGH", "entity-linking",
                  f"성·이름이 공백으로 갈린 한글 인명 {len(r['korean_name_spacing'])}건 ({names}). "
                  "AI가 한 인물로 묶지 못합니다."))

    # UUID / 긴 숫자 ID만으로 이뤄진 URL — 접두어(entity- 등)가 붙어도 잡는다
    last_seg = urllib.parse.urlparse(r["url"]).path.rstrip("/").rsplit("/", 1)[-1]
    uuid_like = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", last_seg, re.I)
    numeric_id = re.fullmatch(r"[0-9]{6,}", last_seg)
    if uuid_like or numeric_id:
        # 슬러그(영문 단어나 한글)가 함께 있으면 완화
        residue = re.sub(r"[0-9a-f-]{8,}", "", last_seg, flags=re.I).strip("-_")
        has_slug = len(residue) >= 3 and residue.lower() not in ("entity", "title", "id", "item")
        if not has_slug:
            f.append(("MEDIUM", "url-semantics",
                      f"URL 마지막 구간이 식별자뿐입니다 ({last_seg[:48]}). "
                      "주제어 슬러그가 없어 사람이 공유·인용하지 않고, 외부 링크가 쌓이지 않습니다."))

    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    f.sort(key=lambda x: order.get(x[0], 9))
    return [{"severity": s, "category": c, "message": m} for s, c, m in f]


# ---------------------------------------------------------------- 실행


def audit(url, timeout=20, run_bots=True):
    parsed = urllib.parse.urlparse(url)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    status, raw, headers, err = fetch(url, BROWSER_UA, timeout)
    if err or not raw:
        return {"url": url, "fatal": err or f"본문 없음 (HTTP {status})", "status": status}
    html_str = raw.decode("utf-8", "ignore")

    text = visible_text(html_str)
    density = {
        "html_bytes": len(raw),
        "text_chars": len(text),
        "ratio_pct": round(100 * len(text) / max(len(raw), 1), 3),
    }

    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rs, rraw, _, rerr = fetch(robots_url, BROWSER_UA, timeout)
    robots_txt = rraw.decode("utf-8", "ignore") if rraw else ""
    groups = parse_robots(robots_txt) if robots_txt else []
    verdicts = {}
    for bot in ROBOTS_CHECK:
        v, why, named = robots_verdict(groups, bot, path) if groups else ("ALLOW", "robots.txt 없음", False)
        verdicts[bot] = {"verdict": v, "reason": why, "named_in_robots": named}

    bots = {}
    if run_bots:
        for name, ua in AI_BOTS:
            st, body, _, e = fetch(url, ua, timeout)
            bots[name] = {"status": st, "bytes": len(body), "error": e}

    result = {
        "url": url,
        "status": status,
        "density": density,
        "jsonld": check_jsonld(html_str),
        "head": check_head(html_str),
        "render": check_render_dependency(html_str, len(text)),
        "image_only_labels": check_image_only_labels(html_str),
        "korean_name_spacing": check_korean_name_spacing(html_str),
        "robots": {"url": robots_url, "status": rs, "fetched": bool(robots_txt), "verdicts": verdicts},
        "bots": bots,
        "text_preview": text[:400],
    }
    result["findings"] = build_findings(result)
    return result


def render(r):
    if r.get("fatal"):
        return f"❌ 점검 실패: {r['fatal']}\n   URL: {r['url']}"

    L = []
    A = L.append
    A("=" * 68)
    A(f"GEO 실측 점검  |  {r['url']}")
    A("=" * 68)

    d, j, h = r["density"], r["jsonld"], r["head"]
    A("")
    A(f"[HTTP] {r['status']}   [텍스트 밀도] {d['ratio_pct']}%  "
      f"(텍스트 {d['text_chars']:,}자 / HTML {d['html_bytes']:,}B)")
    A(f"[JSON-LD] {j['count']}개" + (f"  타입: {', '.join(j['types'][:6])}" if j["types"] else "  ← 없음"))
    A(f"[title] {h['title'] or '(없음)'}")
    A(f"[description] {(h['description'] or '(없음)')[:80]}")
    hl = f"{h['hreflang_count']}개" if h["hreflang_count"] else "HTML에 없음(sitemap 선언일 수 있음)"
    A(f"[canonical] {h['canonical'] or '(없음)'}   [h1] {len(h['h1'])}개   [hreflang] {hl}")

    if r["bots"]:
        A("")
        A("── AI 크롤러 실제 접근 ──")
        ok = [b for b, v in r["bots"].items() if v.get("status") == 200]
        ng = {b: v for b, v in r["bots"].items() if v.get("status") != 200}
        A(f"  ✅ 200 OK ({len(ok)}종): {', '.join(ok)}")
        for b, v in ng.items():
            A(f"  ❌ {b}: {v.get('status') or v.get('error')}")

    A("")
    A("── robots.txt 판정 ──")
    blocked = {b: v for b, v in r["robots"]["verdicts"].items() if v["verdict"] == "DISALLOW"}
    named = [b for b, v in r["robots"]["verdicts"].items() if v["named_in_robots"]]
    if not r["robots"]["fetched"]:
        A("  robots.txt 없음 → 전체 허용으로 간주")
    else:
        A(f"  이름이 명시된 봇: {len(named)}종 / 점검 {len(r['robots']['verdicts'])}종")
        if blocked:
            for b, v in list(blocked.items())[:12]:
                A(f"  🚫 {b}: {v['reason']}")
        else:
            A("  ✅ 점검한 AI 봇 전부 Allow")

    A("")
    A("── 지적 사항 ──")
    if not r["findings"]:
        A("  ✅ 주요 항목에서 지적 없음")
    else:
        icon = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🟢"}
        for f in r["findings"]:
            A(f"  {icon.get(f['severity'], '·')} [{f['severity']}] {f['message']}")

    A("")
    A(f"본문 미리보기: {r['text_preview'][:160]}…")
    A("=" * 68)
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="웹페이지 GEO 실측 점검")
    ap.add_argument("url")
    ap.add_argument("--json", action="store_true", help="JSON으로 출력")
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--no-bots", action="store_true", help="봇 UA 실제 요청 생략(빠름)")
    a = ap.parse_args()

    url = a.url if "://" in a.url else "https://" + a.url
    r = audit(url, timeout=a.timeout, run_bots=not a.no_bots)
    print(json.dumps(r, ensure_ascii=False, indent=2) if a.json else render(r))
    sys.exit(0 if not r.get("fatal") else 1)


if __name__ == "__main__":
    main()
