# MIM 운영 런북

이 문서는 Madup Infra Manager의 계획, 적용, 검증, 비용 정지, 퇴사 처리, 복구 절차를 한 곳에 기록한다. 실제 비밀값은 이 문서나 Git에 저장하지 않는다.

## 1. 운영 원칙

1. 읽기 전용 preflight와 plan을 먼저 실행한다.
2. plan에는 발견한 현재 상태, 원하는 상태, blocker, 실행 action, 만료 시각, 입력 fingerprint가 포함되어야 한다.
3. apply는 검토한 plan만 재생하며, 현재 상태가 plan의 최초 상태와 다르면 중단한다.
4. GCP 발견 명령은 명시한 회사 운영자 계정으로 실행한다.
5. 최초 API·IAM·비용 격리 부트스트랩은 설정된 회사 운영자만 검토형 plan/apply로 실행한다. 이후 런타임 릴리스와 부트스트랩 시크릿 버전 추가는 최소권한 `mim-release` 서비스 계정을 사칭한다.
6. 사용자나 Claude에게 프로젝트·조직·빌링 ID, 운영자 토큰, 서비스 계정 키를 입력시키지 않는다.
7. 개인 계정으로 로그인된 도구는 운영 변경에 사용하지 않는다.

## 2. Git에 들어가는 값과 들어가지 않는 값

중앙 프로젝트 ID와 고정 리전은 코드의 보안 경계라서 공개 코드에 들어갈 수 있다. 조직 ID, 실제 빌링 계정 ID, 운영자 이메일, 보호 프로젝트 목록, 비밀값은 운영자 전용 파일과 Secret Manager에 둔다.

다음 파일은 반드시 Git에서 무시되어야 하며 로그나 테스트 출력으로도 표시하지 않는다.

- `infra/domain/config.env`
- `infra/domain/protected-projects.exact`
- `infra/control-plane/config.env`
- `infra/billing/.state/`
- `infra/edge/.state/`
- `infra/github/.state/`
- `infra/runtime-bootstrap/.state/`
- `infra/release/denylist.exact`
- `infra/release/.state/`

예제 파일에는 형식만 남기고 실제 식별자나 토큰을 넣지 않는다.

## 3. 인프라 적용 순서

종속 관계 때문에 아래 순서를 지킨다.

1. 이미 보유한 `madup.app`의 권한 DNS 응답을 확인한다. 도메인 구매·등록 작업은 이 절차의 범위가 아니다.
2. `infra/domain`에서 중앙 프로젝트·조직·빌링·보호 프로젝트 경계를 검증한다.
3. `infra/control-plane`에서 API, 서비스 계정, 큐, 예산, 최소권한 IAM을 계획하고 적용한다.
4. `infra/billing`에서 원본 비용 내보내기의 정제된 authorized view와 뷰 단위 읽기 권한을 계획하고 적용한다.
5. `infra/github`에서 선택된 `madupmarketing` 저장소만 연결한다.
6. `infra/edge`에서 Google Workspace 기반 Cloudflare Access Managed OAuth 정책과 HMAC Worker를 계획하고 적용한다.
7. `infra/runtime-bootstrap`에서 운영자 전용 입력을 검증하고 Secret Manager 숫자 버전을 만든다.
8. `infra/release`에서 digest 고정 이미지, Cloud Run 서비스, 유지보수 Job, Scheduler를 계획하고 적용한다.
9. staging canary와 smoke test를 실행한다.

각 디렉터리의 `preflight.sh`, `plan.sh`, `apply.sh`, `test_*.sh`가 있는 경우 해당 스크립트를 유일한 변경 경로로 사용한다. 콘솔에서 임의로 만든 상태는 다음 plan에서 drift로 차단되어야 한다.

GitHub 연결 전에 회사 GitHub 조직 관리자가 브라우저에서 GitHub App을 정확히 선택한 저장소에만 설치하고, contents·metadata 읽기 전용 권한을 확인해야 한다. Secret Manager에는 authorizer token 메타데이터와 활성화된 숫자 버전의 webhook secret이 있어야 한다. 토큰이나 webhook 원문은 Claude 대화, 문서, Git에 넣지 않는다.

Slack은 직원의 로그인 필수 수단이 아닌 선택 연동이다. 기본 launch 계약은 Google-only이며 `MIM_SLACK_ENABLED`를 설정하지 않거나 `false`로 두면 Slack app ID, org/workspace allowlist, `MIM_TASK18_SLACK_CONFIG_TOKEN`, tenant evidence가 전혀 필요하지 않다. Slack 검증을 켜려면 `MIM_SLACK_ENABLED=true`를 명시하고, 운영자 환경에만 `MIM_TASK18_SLACK_CONFIG_TOKEN`을 제공하며, 검토한 `infra/release/.state/slack-tenant-evidence.json`과 그 `.sha256` sidecar를 준비해야 한다. 증빙은 plan 시점 기준 30분 이내이며 설정한 app ID, org ID, workspace ID와 정확히 일치해야 한다. 토큰과 증빙 파일은 Git에 커밋하지 않는다.

## 4. DNS와 Cloudflare

운영 호스트는 `https://mim.madup.app`이다. 첫 버전은 Cloudflare Access와 Worker를 공용 입구로 사용해 트래픽이 없어도 발생하는 Google 글로벌 HTTPS 로드밸런서 고정비를 피한다.

정상 조건:

- 등록기관의 `madup.app` 위임 NS와 실제 권한 DNS zone이 일치한다.
- 권한 서버가 `SOA`, `NS`, `mim.madup.app` 질의에 `NOERROR`로 응답한다.
- Cloudflare zone과 Access 애플리케이션이 회사 계정 소유다.
- Access 애플리케이션의 Managed OAuth, 동적 클라이언트 등록, PKCE `S256`, 공개 클라이언트 인증 `none`이 검토한 설정과 일치한다.
- Worker만 Cloud Run 원본 HMAC을 만들 수 있다.
- Cloud Run 직접 URL에서 위조·누락 HMAC과 Access JWT를 거부한다.

공개 제어 서비스인 `mim-control-plane`은 Cloudflare Worker가 `run.app` 원본에 도달할 수 있도록 Cloud Run IAM에서만 `allUsers` invoker와 `ingress=all`을 사용한다. 이것은 브라우저 인증 경계가 아니다. `/healthz`, `/readyz`를 포함한 모든 공개 API·브라우저 경로는 애플리케이션 계층에서 Worker HMAC과 Cloudflare Access JWT를 모두 검증하며, 직접 원본 요청은 거부한다. 유지보수 Job과 개인 앱 같은 machine 서비스는 이 예외를 공유하지 않고 내부 ingress와 정확한 서비스 계정 IAM을 유지한다.

개인 앱은 Cloudflare -> Go 앱 게이트웨이 -> private Cloud Run IAM 경계만 사용한다. exact app-gateway invoker와 검토된 breakglass 외의 사용자 앱 invoker는 금지하며, legacy IAP 접근 정책은 현재 운영 계약에 포함되지 않는다.

`SERVFAIL` 또는 권한 서버의 `REFUSED`가 나오면 앱 레코드를 추가하기 전에 zone 위임부터 복구한다. 개인 Cloudflare 계정에 임시로 zone을 만들지 않는다.

## 5. 런타임과 정기 작업

Cloud Run 서비스는 요청 기반이며 `min=0`, `max=1`이다. 요청이 없으면 0개로 축소되고, 호출되면 cold start 후 처리한다.
직원 요청의 “24시간 웹앱”은 상시 인스턴스 고정이 아니라 언제든 요청 시 다시 시작되는 요청 기반 웹앱으로 해석한다.

고정 유지보수 작업:

| Job | UTC 일정 | 역할 |
| --- | --- | --- |
| `mim-identity-sync` | `*/15 * * * *` | Workspace 그룹과 사용자 상태 동기화, 퇴사·정지 감지 |
| `mim-lifecycle` | `7,22,37,52 * * * *` | 격리, 이전 유예, 미사용 경고·정리 실행 |
| `mim-usage-ingest` | `12 * * * *` | 비용 내보내기 수집, 이번 달 한도 판단, 활동 집계 |

세 작업은 각각 task 1개, parallelism 1, retry 0, timeout 600초로 실행하고 중복 실행 lease를 사용한다. 컨테이너에는 세 개의 고정 환경변수만 전달한다.

- `MIM_RUNTIME_MODE`
- `MIM_RUNTIME_BOOTSTRAP_SECRET_VERSION`
- `MIM_ENABLE_MUTATIONS=true`

부트스트랩 참조는 항상 중앙 Secret Manager의 숫자 버전 전체 경로여야 한다. `latest`는 재현성과 plan 검증을 깨므로 금지한다.
사용자-facing 스케줄 pilot은 정확히 시간당 1회(`0 * * * *`)와 `Asia/Seoul`만 지원한다. 그 외 cron 표현식이나 타임존은 범위 밖으로 거절한다.

## 6. 사용자 수명주기

Google Workspace와 승인 그룹이 유일한 권한 기준이다.

- 그룹에 없는 신규 사용자: 접근 거부
- 재직 중 그룹 제거 또는 계정 잠금: 즉시 MIM 사용자 정지, 앱 호스트 비활성화, 스케줄·시크릿 접근 격리
- 퇴사 처리 후 7일: 관리자 이전 hold가 없는 계산 리소스 삭제
- 최근 활동 23일 없음: 경고
- 최근 활동 30일 없음: 상태 없는 서비스·Job·Scheduler 정리
- 이미지와 감사 메타데이터: 복구와 조사에 필요한 기간 보존

Slack 연결은 사용자 편의를 위한 보조 식별자다. 기본값은 비활성화이며 `MIM_SLACK_ENABLED=true`일 때만 중앙 Slack authorize 경로를 사용한다. Slack 연결이 남아 있어도 Workspace 권한이 사라지면 MIM 작업을 승인하지 않는다.

## 7. 비용 정지 정책

비용은 UTC 현재 달로 집계한다.

- 사용자 직접 비용: 사용자 1,000원 정책에 반영
- 공용 플랫폼 비용: 사용자 한도에서는 제외, 조직 10,000원 한도에는 포함
- 경고선 도달: 대시보드와 알림에 표시
- 새 작업 차단선 도달: 새 배포·스케줄·시크릿 증가 차단
- 일시정지선 도달: 해당 사용자 계산 작업 일시정지
- 조직 비상 정지선 도달: 중앙 정책이 허용한 계산 작업을 중단하고 관리자 조사 요구

비용 내보내기 지연을 고려한 예약 금액을 포함한다. finalized 금액이 estimate보다 크면 더 큰 값을 정책 계산에 사용한다.

## 8. 대시보드 운영 계약

일반 사용자는 자기 리소스와 상태만 볼 수 있다.

- 서비스, 스케줄, 시크릿 메타데이터
- 이번 달 직접 비용과 1,000원 대비 비율
- 리소스 한도 사용량
- 최근 작업과 정제된 실패 설명
- 자기 계정의 활성·정지·퇴사 처리 상태

관리자는 조직 전체를 볼 수 있다.

- 비용이 0원인 사용자를 포함한 전체 사용자
- 사용자별 상태, 리소스 수, 한도, 이번 달 비용, 경고·정지 상태
- 공용 비용과 조직 총액
- 최근 24시간·7일·30일 사용자/대시보드 활동
- `identity-sync`, `lifecycle`, `usage-ingest`의 최근 시작·성공·실패와 stale 여부

대시보드와 로그에는 원시 시크릿 값, 환경변수 값, Authorization/Cookie, 프롬프트 본문, 사용자 IP를 표시하지 않는다.

## 9. 장애 처리

### 정기 작업 실패

1. 대시보드에서 실패한 Job과 마지막 성공 시각을 확인한다.
2. Cloud Run Job 실행 로그에서는 구조화된 상태 코드와 실행 ID만 확인한다.
3. IAM drift, 부트스트랩 버전 비활성화, 정제 비용 뷰 `mim_billing_secure.mim_usage_costs_v1`의 권한·스키마, lease 만료를 순서대로 검사한다. 유지보수 계정에 원본 `mim_billing_export` 접근 권한을 추가해서 복구하지 않는다.
4. 코드를 수정하면 새 digest 이미지를 만들고 새 plan을 검토한다.
5. apply가 새 digest와 현재 상태를 다시 확인한 후 Job을 갱신한다.
6. 수동 실행으로 한 번 검증하고 다음 Scheduler 실행 성공까지 확인한다.

### 배포 실패

1. 사용자에게는 정제된 실패 분류와 허용된 다음 행동만 보여 준다.
2. 동일 계획을 무한 재시도하지 않는다.
3. 소스 SHA, 이미지 digest, 리소스 한도, IAM 경계, 운영 상태 drift를 확인한다.
4. drift의 상세 관찰값은 private 운영 신호라서 별도 공개 repair 도구로 노출하지 않는다. 사용자-facing 시작점은 항상 `explain_failure`다.
5. 고친 코드가 같은 저장소의 새 SHA이고 auto-deploy가 검토된 branch에 연결되어 있으면 기존 GitHub webhook이 새 배포를 큐잉한다.
6. auto-deploy가 없거나 새 확인이 필요하면 새 `plan_deploy`를 만들고 같은 대화에서 다시 확인받는다.
7. 어떤 경우에도 operation이 terminal 상태가 되기 전에는 완료로 보고하지 않는다.

### Desired-state v2 전환

`gateway_iam`/`public_iam` 경계와 논리 환경변수 이름을 포함하는 현재 아티팩트는
`mim-desired-state-v2`로만 기록한다. 구형 `mim-desired-state-v1`은 동일 스키마로
재해석하지 않고 fail-closed 한다. 최초 운영 배포 전 `desired_state_artifacts`와
대기 중인 배포 작업이 비어 있음을 확인해야 하며, v1 자료가 발견되면 배포를
중단하고 별도 검토된 일회성 재계획/재서명 절차로 처리한다.

### Firestore 대시보드 인덱스 전환

대시보드의 최신 작업 조회는 `operations` 컬렉션의 소유자 범위 복합 인덱스가
`READY`인 경우에만 운영 준비 완료로 본다. 중앙 인프라 apply는 인덱스 생성을
동기식으로 기다리고, `CREATING`, `NEEDS_REPAIR`, 중복 또는 다른 필드 조합을
발견하면 배포를 중단한다. 최초 배포에서는 Firestore 데이터베이스가 아직 없고
기존 `operations` 문서가 없음을 읽기 전용 plan으로 확인한다. 기존 데이터베이스를
가진 환경을 전환할 때는 `workload_owner_id`가 없는 과거 문서를 검토된 일회성
백필로 먼저 보완하며, 소유자 없는 전체 컬렉션 조회로 우회하지 않는다.

`origin_request_claims.expires_at` TTL은 재전송 차단 자체가 아니라 보존 정리용이다.
재전송 차단은 동기식 create-only 트랜잭션이 담당하므로 TTL 상태 `CREATING`은
허용하되, 필드·컬렉션·0초 오프셋이 다르면 fail-closed 한다.

### 비용 급증

1. 새 배포와 리소스 증가를 먼저 차단한다.
2. 사용자 직접 비용과 공용 비용을 분리해 원인을 찾는다.
3. 승인되지 않은 리소스나 프로젝트 전체 권한 drift를 감사한다.
4. 조직 정지선을 넘으면 계산 작업을 일시정지하고 관리자가 예산을 검토한다.

## 10. 배포 완료 체크리스트

- 모든 shell 스크립트 구문 검사 통과
- Python Ruff, mypy, unit/integration test 통과
- edge Worker와 Go 앱 게이트웨이 테스트 통과
- builder, control-plane, app-gateway Docker build 통과
- 공개 릴리스 가드와 비밀값 스캔 통과
- 기존 `mim-desired-state-v1` 아티팩트와 대기 중인 v1 작업이 없음을 확인
- `operations` 소유자 범위 복합 인덱스가 `READY`이고, 기존 DB라면 모든 운영 문서의 `workload_owner_id` 백필 완료를 확인
- GitHub App이 조직 관리자를 통해 정확한 선택 저장소에만 읽기 전용으로 설치되고 authorizer·webhook secret 선행조건이 충족됨
- `MIM_SLACK_ENABLED`가 unset/`false`이면 Google-only 검증만으로 통과하고 Slack 자격증명·증빙이 요구되지 않음을 확인
- `MIM_SLACK_ENABLED=true`이면 운영자 환경의 `MIM_TASK18_SLACK_CONFIG_TOKEN`과 30분 이내 tenant 증빙 JSON·SHA sidecar가 정확한 app/org/workspace와 일치함
- GitHub 연결이 선택된 저장소 외 대상을 거부
- 서비스 `min=0`, `max=1` 및 정확한 서비스 계정 확인
- 프로젝트 전체 `run.invoker`, legacy IAP accessor, BigQuery viewer 부재와 앱별 gateway-only invoker 확인
- maintenance의 `roles/bigquery.dataViewer`가 정제 뷰 `mim_billing_secure.mim_usage_costs_v1`에만 있고 원본 데이터셋·테이블에는 없는지 확인
- Cloudflare Managed OAuth 메타데이터, 동적 클라이언트 등록, PKCE 로그인이 검토한 계약과 일치하는지 확인
- 직접 앱 게이트웨이 요청의 Worker 증명 거부, 사용자 앱 `run.app` IAM 거부, Cloudflare Access 로그인과 `*.madup.app` 앱 접근 canary 통과
- 세 유지보수 Job의 수동 실행과 다음 예약 실행 성공 확인
- 기존 `madup.app` DNS의 권한 응답과 `https://mim.madup.app/readyz` 확인

체크리스트가 하나라도 실패하면 코드 준비 상태와 운영 배포 상태를 구분해 보고한다.
