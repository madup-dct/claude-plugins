# MADUP Claude Plugins

매드업 DCT 팀용 Claude Code 플러그인 마켓플레이스.

## 설치 (팀원용 — 2줄이면 끝)

Claude Code에서:

```
/plugin marketplace add madup-dct/claude-plugins
/plugin install naver-keywordtool@madup
```

재시작하면 스킬이 활성화된다.

## 플러그인 목록

| 플러그인 | 설명 | 설치 |
|---|---|---|
| `naver-keywordtool` | 네이버 검색광고 API 연관 키워드·검색량 실측 + GEO 프롬프트 제안. 최초 1회 API 키 등록 필요 — 설치 후 "네이버 키워드툴 세팅해줘"라고 치면 안내받으며 진행. | `/plugin install naver-keywordtool@madup` |
| `copy-tone-gate` | 슬라이드·제안서 카피의 AI 말투 게이트. "카피 말투 게이트 돌려줘", "AI 티 나는 워딩 고쳐줘"라고 치면 소리 테스트 + 금지 패턴 7종으로 교정. | `/plugin install copy-tone-gate@madup` |
| `humanize-korean` | AI가 쓴 한글 장문 윤문. "AI 티 없애줘", "사람이 쓴 것처럼 윤문해줘"라고 치면 40+ 패턴 탐지 후 내용 보존 재작성. | `/plugin install humanize-korean@madup` |
| `madup-writing` | 슬랙·보고서·제안서·발표 카피용 한글 실무 문안을 매드업 톤으로 정리. "문장만 정리해줘", "제안서 말투로 다듬어줘", "발표 카피 다듬어줘"라고 치면 사실·숫자는 그대로 두고 결론과 액션이 먼저 보이게 재작성. | `/plugin install madup-writing@madup` |
| `geo-page-audit` | 웹페이지가 생성형 AI 검색에 인용될 수 있는 상태인지 실측 점검. "이 페이지 GEO 점검해줘", "AI 검색에 왜 안 나오지"라고 치면 AI 크롤러 접근·robots.txt·JSON-LD·텍스트 밀도를 진단하고 개발팀 전달용 요청서까지 정리. API 키 불필요. | `/plugin install geo-page-audit@madup` |

## 사용 예

```
여행자보험 연관 키워드랑 검색량 뽑아줘
여행자보험으로 GEO 프롬프트 제안까지 해줘
이 슬랙 문장만 매드업 톤으로 정리해줘 — 날짜랑 숫자는 그대로
이 주간보고 결론 먼저 나오게 다듬어줘
이 발표 헤드라인 입으로 말할 수 있게 다듬어줘
```

## 보안

- API 키는 각자 로컬 `~/.config/naver-searchad/credentials.env`(chmod 600)에만 저장한다.
- 이 저장소에 자격증명을 커밋하는 것은 절대 금지.

## 사용 집계 고지

각 플러그인에는 사용 횟수 집계 비콘이 포함되어 있다. 스킬이 호출될 때 **스킬명 · 플러그인 버전 · 익명 해시 uid** 세 값만 사내 엔드포인트로 전송한다.
**프롬프트 내용·작업 내용·파일명은 어떤 것도 수집하지 않는다.** 집계 목적은 어떤 도구가 얼마나 쓰이는지 파악해 개선 우선순위를 정하는 것이다.
(리포트: `telemetry/report.sh`)
