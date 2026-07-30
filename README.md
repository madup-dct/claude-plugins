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

| 플러그인 | 설명 |
|---|---|
| `naver-keywordtool` | 네이버 검색광고 API 연관 키워드·검색량 실측 + GEO 프롬프트 제안. 최초 1회 API 키 등록 필요 — 설치 후 Claude Code에 "네이버 키워드툴 세팅해줘"라고 치면 안내받으며 진행. |

## 사용 예

```
여행자보험 연관 키워드랑 검색량 뽑아줘
여행자보험으로 GEO 프롬프트 제안까지 해줘
```

## 보안

- API 키는 각자 로컬 `~/.config/naver-searchad/credentials.env`(chmod 600)에만 저장한다.
- 이 저장소에 자격증명을 커밋하는 것은 절대 금지.
