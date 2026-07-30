---
name: naver-keywordtool
description: 네이버 검색광고 API로 연관 키워드·월간 검색량(PC/모바일)을 실측 조회하고, 결과를 GEO 목표 프롬프트 후보(질문형)로 변환 제안하는 스킬. "연관 키워드 뽑아줘", "검색량 조회해줘", "네이버 키워드 검색량", "키워드 발굴", "GEO 프롬프트 제안해줘", "네이버 키워드툴 세팅해줘" 요청 시 이 스킬을 사용할 것. API 키만 등록하면 누구나 사용 가능.
---

# 네이버 연관 키워드·검색량 조회 + GEO 프롬프트 제안

네이버 검색광고 공식 API(`GET /keywordstool`)로 시드 키워드의 연관 키워드 + 월간 검색량(PC/모바일) + 경쟁도를 실측하고, 상위 연관어를 GEO 목표 프롬프트 후보(질문형)로 변환해 제안한다.

## 실행 방법

스크립트는 이 플러그인 안에 있다:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/naver-keywordtool/scripts/keywordstool.py" 여행자보험
python3 "${CLAUDE_PLUGIN_ROOT}/skills/naver-keywordtool/scripts/keywordstool.py" 선크림 앰플 --limit 100
python3 "${CLAUDE_PLUGIN_ROOT}/skills/naver-keywordtool/scripts/keywordstool.py" 여행자보험 --csv out.csv   # 엑셀 호환 CSV
python3 "${CLAUDE_PLUGIN_ROOT}/skills/naver-keywordtool/scripts/keywordstool.py" 여행자보험 --json          # 프로그램 처리용
```

(`${CLAUDE_PLUGIN_ROOT}`가 비어 있으면 이 SKILL.md와 같은 폴더의 `scripts/keywordstool.py`를 실행하면 된다.)

## 사용자 안내 — 이렇게 치면 됩니다

```
여행자보험 연관 키워드랑 검색량 뽑아줘        → 연관 키워드 표 (월간 검색량 내림차순)
여행자보험으로 GEO 프롬프트 제안까지 해줘     → 검색량 실측 + 질문형 프롬프트 후보
네이버 키워드툴 세팅해줘                      → 최초 1회 API 키 등록 안내
```

## 최초 설정 (API 키 등록 — 1회만)

자격증명 없이 실행하면 스크립트가 아래 안내를 출력한다. 사용자가 "세팅해줘"라고 하면 Claude가 이 과정을 함께 진행할 것:

1. **키 발급**: https://manage.searchad.naver.com → 도구 > API 사용 관리에서 액세스라이선스·비밀키 발급. CUSTOMER_ID는 광고시스템 우측 상단의 광고계정 번호.
2. **파일 저장** (`~/.config/naver-searchad/credentials.env`, chmod 600):
   ```
   NAVER_SEARCHAD_API_KEY=<액세스라이선스>
   NAVER_SEARCHAD_SECRET_KEY=<비밀키>
   NAVER_SEARCHAD_CUSTOMER_ID=<광고계정 번호>
   ```
3. 같은 이름의 환경변수가 있으면 그쪽이 우선. 키 재발급 시 이 파일만 교체.

**보안**: 키는 홈 디렉토리(`~/.config/`)에만 저장한다. 저장소 안에 credentials 파일을 만들거나 커밋하는 것은 절대 금지.

## GEO 프롬프트 제안 워크플로 (Claude 수행 지침)

사용자가 "프롬프트 제안"까지 요청하면, 검색량 조회 후 다음을 수행한다:

1. 상위 연관 키워드를 **확장 축으로 분류**: 수식어 축(지역·기간·대상·조건) / 의도 축(추천·비교·후기·방법).
2. 각 축을 **사람이 AI에게 실제로 던질 질문형**으로 변환한다. 검색량을 근거로 병기한다.
   - 예: `여행자보험추천 7,450` → "여행자보험 어디가 제일 나아? 기준도 알려줘" (추천형)
   - 예: `단기여행자보험 10,230` → "3박 4일만 짧게 드는 여행자보험 있어?" (상황형)
   - 예: `여행자보험비교 5,960` → "여행자보험 3개만 비교해서 골라줘" (비교형)
3. 유형 라벨(추천형/비교형/의도형/상황형/후기형)과 근거 검색량을 함께 표로 제시한다.
4. 심의 리스크 질문(특정 상품 수익 보장·타사 비방 유도형)은 후보에서 제외한다.

## API 제약 (스크립트가 자동 처리)

- 힌트 키워드 최대 5개/호출 → 5개씩 배치 분할 + 0.3s 대기 + dedupe.
- 힌트 키워드 공백 불허 → 자동 제거 ("발편한 운동화" → "발편한운동화").
- 검색량 10 미만은 `"< 10"` 문자열 → 정렬 시 9 취급, 표시는 `<10`.
- 429/5xx 3회 재시도. 401 지속 시 키 만료 — 재발급 안내.

## 응답 컬럼 의미

| 컬럼 | 의미 |
|---|---|
| `monthlyPcQcCnt` / `monthlyMobileQcCnt` | 최근 30일 월간 검색수 (PC/모바일) |
| `monthlyAvePcClkCnt` / `monthlyAveMobileClkCnt` | 월평균 광고 클릭수 |
| `monthlyAvePcCtr` / `monthlyAveMobileCtr` | 월평균 광고 클릭률(%) |
| `plAvgDepth` | 월평균 노출 광고수 (경쟁 광고 개수) |
| `compIdx` | 경쟁정도 (낮음/중간/높음) |
