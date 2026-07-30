#!/usr/bin/env python3
"""네이버 검색광고 API 연관 키워드·검색량 조회 (GET /keywordstool).

사용 예:
  python3 keywordstool.py 선크림 앰플              # 연관 키워드 상위 30개 표
  python3 keywordstool.py 선크림 --limit 100
  python3 keywordstool.py 선크림 --json            # 원본 JSON 전체
  python3 keywordstool.py 선크림 --csv out.csv     # 전체 컬럼 CSV 저장

자격증명: ~/.config/naver-searchad/credentials.env (환경변수가 있으면 우선)
"""
import argparse
import base64
import csv
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://api.searchad.naver.com"
CRED_PATH = os.path.expanduser("~/.config/naver-searchad/credentials.env")
BATCH = 5  # keywordstool은 hintKeywords 최대 5개/호출


def load_creds():
    creds = {}
    if os.path.exists(CRED_PATH):
        with open(CRED_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    creds[k.strip()] = v.strip()
    api_key = os.environ.get("NAVER_SEARCHAD_API_KEY") or creds.get("NAVER_SEARCHAD_API_KEY")
    secret = os.environ.get("NAVER_SEARCHAD_SECRET_KEY") or creds.get("NAVER_SEARCHAD_SECRET_KEY")
    customer = os.environ.get("NAVER_SEARCHAD_CUSTOMER_ID") or creds.get("NAVER_SEARCHAD_CUSTOMER_ID")
    if not (api_key and secret and customer):
        sys.exit(
            "[설정 필요] 네이버 검색광고 API 자격증명이 없습니다. 최초 1회만 등록하면 됩니다.\n"
            "\n"
            "1) 키 발급: https://manage.searchad.naver.com → 도구 > API 사용 관리\n"
            "   - 액세스라이선스(API KEY) / 비밀키(SECRET KEY) 발급\n"
            "   - CUSTOMER_ID = 광고시스템 우측 상단의 광고계정 번호\n"
            "\n"
            "2) 아래 3줄을 파일로 저장:\n"
            f"   mkdir -p ~/.config/naver-searchad && chmod 700 ~/.config/naver-searchad\n"
            f"   {CRED_PATH} 에 작성 후 chmod 600:\n"
            "   NAVER_SEARCHAD_API_KEY=<액세스라이선스>\n"
            "   NAVER_SEARCHAD_SECRET_KEY=<비밀키>\n"
            "   NAVER_SEARCHAD_CUSTOMER_ID=<광고계정 번호>\n"
            "\n"
            "3) 다시 실행하면 됩니다. (Claude Code에서는 \"네이버 키워드툴 세팅해줘\"라고 치면 이 과정을 안내받으며 진행할 수 있습니다)\n"
            "   ※ 키는 절대 저장소에 커밋하지 마세요 — 홈 디렉토리 밖으로 나가지 않습니다."
        )
    return api_key, secret, customer


def signed_get(path, params, api_key, secret, customer):
    # 서명은 path만 포함 (쿼리스트링 제외): base64(HMAC-SHA256(secret, "{ts}.{method}.{path}"))
    url = f"{BASE_URL}{path}?{urllib.parse.urlencode(params)}"
    last_err = None
    for attempt in range(3):
        ts = str(int(time.time() * 1000))
        sig = base64.b64encode(
            hmac.new(secret.encode(), f"{ts}.GET.{path}".encode(), hashlib.sha256).digest()
        ).decode()
        req = urllib.request.Request(url, headers={
            "X-Timestamp": ts,
            "X-API-KEY": api_key,
            "X-Customer": customer,
            "X-Signature": sig,
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            last_err = f"HTTP {e.code}: {body}"
            if e.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            sys.exit(last_err)
        except urllib.error.URLError as e:
            last_err = f"네트워크 오류: {e.reason}"
            if attempt < 2:
                time.sleep(2)
                continue
            sys.exit(last_err)
    sys.exit(last_err)


def to_int(v):
    # 검색량 10 미만은 "< 10" 문자열로 옴 → 정렬용으로 9 취급
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().startswith("<"):
        return 9
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def fetch_all(hint_keywords, api_key, secret, customer):
    merged = {}
    for i in range(0, len(hint_keywords), BATCH):
        batch = hint_keywords[i:i + BATCH]
        data = signed_get(
            "/keywordstool",
            {"hintKeywords": ",".join(batch), "showDetail": "1"},
            api_key, secret, customer,
        )
        for row in data.get("keywordList", []):
            merged.setdefault(row["relKeyword"], row)
        if i + BATCH < len(hint_keywords):
            time.sleep(0.3)
    rows = list(merged.values())
    rows.sort(key=lambda r: to_int(r.get("monthlyPcQcCnt")) + to_int(r.get("monthlyMobileQcCnt")), reverse=True)
    return rows


def fmt_n(v):
    n = to_int(v)
    return f"{n:,}" if not (isinstance(v, str) and v.strip().startswith("<")) else "<10"


def main():
    ap = argparse.ArgumentParser(description="네이버 검색광고 연관 키워드·검색량 조회")
    ap.add_argument("keywords", nargs="+", help="힌트 키워드 (여러 개 가능, 공백은 자동 제거)")
    ap.add_argument("--limit", type=int, default=30, help="표 출력 개수 (기본 30, 0=전체)")
    ap.add_argument("--json", action="store_true", help="원본 JSON 전체 출력")
    ap.add_argument("--csv", metavar="PATH", help="전체 컬럼 CSV 저장 경로")
    args = ap.parse_args()

    # API는 힌트 키워드에 공백 불허 → 제거
    kws = []
    for k in args.keywords:
        cleaned = k.replace(" ", "")
        if cleaned != k:
            print(f"[알림] 공백 제거: '{k}' → '{cleaned}'", file=sys.stderr)
        if cleaned:
            kws.append(cleaned)

    api_key, secret, customer = load_creds()
    rows = fetch_all(kws, api_key, secret, customer)

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return

    if args.csv:
        cols = ["relKeyword", "monthlyPcQcCnt", "monthlyMobileQcCnt",
                "monthlyAvePcClkCnt", "monthlyAveMobileClkCnt",
                "monthlyAvePcCtr", "monthlyAveMobileCtr", "plAvgDepth", "compIdx"]
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(cols + ["monthlyTotal"])
            for r in rows:
                w.writerow([r.get(c, "") for c in cols] +
                           [to_int(r.get("monthlyPcQcCnt")) + to_int(r.get("monthlyMobileQcCnt"))])
        print(f"CSV 저장: {args.csv} ({len(rows)}개 키워드)")
        return

    show = rows if args.limit == 0 else rows[:args.limit]
    print(f"{'연관키워드':<22} {'PC':>10} {'모바일':>10} {'합계':>10} {'경쟁':>4} {'광고수':>4}")
    print("-" * 68)
    for r in show:
        total = to_int(r.get("monthlyPcQcCnt")) + to_int(r.get("monthlyMobileQcCnt"))
        kw = r["relKeyword"]
        pad = 22 - sum(2 if ord(c) > 0x1100 else 1 for c in kw)
        print(f"{kw}{' ' * max(1, pad)} {fmt_n(r.get('monthlyPcQcCnt')):>10} "
              f"{fmt_n(r.get('monthlyMobileQcCnt')):>10} {total:>10,} "
              f"{r.get('compIdx', '-'):>4} {r.get('plAvgDepth', 0):>4}")
    print(f"\n총 {len(rows)}개 연관 키워드 (표시 {len(show)}개, 월간 검색량 합계 내림차순)")


if __name__ == "__main__":
    main()
