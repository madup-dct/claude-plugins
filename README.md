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
| `madup-infra-manager` | 승인된 `madupmarketing` 워크로드만 MIM 계획 기반으로 다루는 인프라 운영 스킬. "배포해줘", "매시간 돌려줘", "상태 알려줘", "비용 보여줘", "고쳐줘"라고 치면 계획 요약 후 확인된 작업만 이어간다. | `/plugin install madup-infra-manager@madup` |
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

`naver-keywordtool`, `copy-tone-gate`, `humanize-korean`, `madup-writing` 같은 일반 플러그인에는 사용 횟수 집계 비콘이 포함되어 있다. 스킬이 호출될 때 **스킬명 · 플러그인 버전 · 익명 해시 uid** 세 값만 사내 엔드포인트로 전송한다.
`madup-infra-manager`는 비콘 대신 `https://mim.madup.app/mcp` 로 authenticated plan/status/usage/operation 요청을 보낸다. MIM은 거버넌스에 필요한 normalized operational metadata 만 보관할 수 있다: authenticated user, repository/workload reference, commit SHA, normalized action/outcome/timing, resource/quota/cost measurements.
MIM은 raw Claude conversation text, secret values, raw request bodies, auth headers, cookies, client IPs, user agents 를 보관하지 않는다.
집계 목적은 어떤 도구가 얼마나 쓰이는지 파악하고 거버넌스·지원·비용 관리를 수행하는 것이다.
(리포트: `telemetry/report.sh`)

## MIM 로그인 경계

`madup-infra-manager`는 직원이 인프라 값을 직접 입력하는 방식으로 쓰지 않는다. Employees never enter GCP project, organization, billing, Cloudflare, or operator values, and they never receive a shared API key. Those settings stay in operator-only configuration, and the public placeholders are `MIM_OPERATOR_EMAIL`, `MIM_PROJECT_ID`, `MIM_ORGANIZATION_ID`, and `MIM_BILLING_ACCOUNT_ID`.

프로덕션 MIM 인증 경로가 활성화되면 첫 MIM tool call 에서 browser Cloudflare Access Managed OAuth 가 열리고, 사용자는 Google Workspace identity 와 MIM access group membership 로 승인된다. After the first grant, Claude stores and refreshes the OAuth token, and later calls reuse that token instead of asking the employee for cloud values.

Slack OAuth 는 optional integration 이다. It is not the primary MIM login. Slack-specific redirects complete with the authorized service, the resulting secret lands in Secret Manager, and tokens are never pasted into Claude. Least scopes, rotation, and revocation remain mandatory.

퇴사나 권한 제거 시에는 group removal 로 new or renewed Access sessions 가 막힌다. Periodic identity reconciliation quarantines schedules and access, then transfers or retires resources under admin policy. Active-session latency is not assumed to be zero, so release checks must include an active-session latency test.

## MIM Public Release Guard

MIM public release guard 는 tenant-specific operator metadata 가 퍼블릭 Git history 로 나가지 않도록 돕는 local defense-in-depth / procedural gate 다. 이건 server-side enforcement 가 아니라 local Git hook 과 manual verification 조합이므로, Git hook 을 설치하지 않거나 skip 하면 우회될 수 있다.

설치:

```bash
bash plugins/madup-infra-manager/infra/release/install_git_hooks.sh
```

이 스크립트는 repository-local `core.hooksPath` 를 정확히 `.githooks` 로 설정하고, tracked `pre-push` Git hook 과 release verifier 실행 비트를 다시 확인한다.

CI 점검:

```bash
bash plugins/madup-infra-manager/infra/release/verify.sh --ci
```

`verify.sh --ci` 는 generic local scanner path 와 public contract tests 를 돌리지만, ignored exact denylist 가 CI 에 없으므로 first public push 를 승인할 수는 없다고 명시적으로 출력한다.
이 advisory 경로는 의도적으로 empty temporary denylist 로 generic 패턴만 본다. exact tenant 값 검사는 하지 않으므로 non-comment exact 값이 하나도 없어도 돌아가지만, release approval 로 간주하면 안 된다.

릴리스 점검:

```bash
bash plugins/madup-infra-manager/infra/release/verify.sh --release origin/main
```

`verify.sh --release origin/main` 은 strict local diff scan 과 `origin/main..HEAD` outbound history scan 을 둘 다 실행한다. strict local scan 은 내부적으로 `--require-exact-values` 를 써서 `plugins/madup-infra-manager/infra/release/denylist.exact` 안에 적어도 하나의 non-comment exact value 가 있어야 통과한다. 이 파일은 반드시 ignored 상태와 mode `0600` 을 유지해야 한다. 운영자는 require `verify.sh --release` before first public push 라는 절차를 문서화해야 한다.

MIM 자체의 로컬 품질 게이트는 다음 명령으로 실행한다.

```bash
bash plugins/madup-infra-manager/infra/release/verify.sh --local
```

이 모드는 플러그인 strict validation, Python lint/type/unit/integration, 모든 인프라 shell contract, Cloudflare Worker와 Go 앱 게이트웨이 테스트, builder/control-plane/app-gateway 컨테이너 빌드, generic secret scan 을 순서대로 실행하며 외부 배포는 하지 않는다. `MIM_ENABLE_MUTATIONS` 는 설정하지 않으면 정확히 `false` 로 취급된다. `true` 이외의 임의 문자열은 실패하고, 이 검증 스크립트 자체는 값이 `true` 여도 plan/apply 또는 배포 명령을 실행하지 않는다.

인증된 staging 이 준비된 뒤에는 operator 전용 `MIM_CONFIG_FILE` 과 정확한 `MIM_STAGING_BASE_URL=https://mim.madup.app` 을 제공해 다음 명령을 실행한다.

```bash
bash plugins/madup-infra-manager/infra/release/verify.sh --staging
```

staging 모드는 로컬 게이트를 다시 통과한 뒤 IAM diff, Worker·앱 게이트웨이·사용자 앱의 direct-origin denial, sensitive-project denial, gateway-only runtime IAM, Streamlit WebSocket/Next.js 경로, Slack OAuth, staging contract, authenticated read-only smoke 를 모두 확인한다. 이 단계에서는 `MIM_REQUIRE_STAGING_CANARIES=true` 를 강제해서 필수 staging 설정이나 canary 가 하나라도 빠지면 skipped 로 승인하지 않고 nonzero 로 종료한다. 제한된 mutation canary 를 의도적으로 검증할 때만 `MIM_ENABLE_MUTATIONS=true` 와 `MIM_STAGING_MUTATION_CANARY=true` 를 함께 사용한다.

`pre-push` hook 은 remote name, remote URL, 그리고 Git 이 넘긴 stdin ref lines 를 그대로 scanner 로 전달한다. ignored exact denylist 가 없거나 unreadable 하면 network transfer 전에 fail-closed 로 막는다. 다만 이 hook 은 local defense-in-depth 이라서 설치하지 않거나 skip 하면 회피될 수 있으므로, 퍼블릭 공개 이후에는 protected review 와 branch policy 로 후속 통제를 걸어야 한다.

GitHub Actions 쪽은 두 단계로 나눈다.

- advisory: `.github/workflows/mim-public-release-advisory.yml`
  - `pull_request` 와 `push` to `main` 에서 generic scanner 와 release contract tests 를 돌린다.
  - secret 이 없는 public repo CI 이므로 exact denylist 기반 승인 판단은 하지 않는다.
- manual exact gate: `.github/workflows/mim-public-release-gate.yml`
  - `workflow_dispatch` 와 protected environment `mim-public-release` 로만 돈다.
  - trusted `main` scanner checkout 과 candidate checkout 을 분리하고, environment secret `MIM_PUBLIC_RELEASE_DENYLIST_EXACT` 로 0600 temp denylist 를 만든다.
  - trusted scanner 가 끝나면 secret file 을 지우고 unset 한 뒤에만 candidate test code 를 실행한다.

중요한 한계도 명확하다. public repo CI cannot prevent the first leaked commit. 이미 비밀값이 public Git history 에 들어간 뒤에는 workflow 가 그 사실을 늦게 발견할 뿐이다. hard guarantee 가 필요하면 private repository, pre-receive hook, 또는 server-side mirror gate 같은 private/pre-receive 통제가 추가로 있어야 한다.
