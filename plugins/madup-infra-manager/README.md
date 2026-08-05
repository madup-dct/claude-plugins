# Madup Infra Manager

Madup Infra Manager(MIM)는 마케터가 Claude에서 “배포해줘”, “매시간 돌려줘”, “상태 보여줘”라고 요청했을 때 회사의 보안·비용 정책 안에서만 작업을 실행하는 중앙 인프라 관리 계층이다.

MIM은 임의의 클라우드 명령을 대신 실행하는 도구가 아니다. 모든 변경은 승인된 `madupmarketing` 저장소의 고정 커밋을 대상으로 계획을 먼저 만들고, 사용자가 그 계획을 확인한 뒤에만 실행된다.

## 지원 범위

- 앱: Streamlit, Next.js
- 배치: 승인된 Python 스크립트와 Cloud Run Job
- 소스: 사전에 등록된 `madupmarketing` GitHub 저장소만 허용
- 플랫폼 저장소: `madup-dct/claude-plugins`는 배포 대상에서 제외
- 클라우드: 중앙 GCP 프로젝트 `mim-prod-123456`
- 사용자 진입점: `https://mim.madup.app`
- 대화 진입점: Claude 플러그인의 MIM 스킬과 제한된 MCP 도구

## 사용자 흐름

1. Google Workspace 계정과 MIM 그룹으로 로그인한다.
2. Claude가 요청을 배포, 스케줄, 상태, 비용, 복구 중 하나로 분류한다.
3. MIM이 안전한 소스 식별자(저장소 owner/name, 검토된 ref가 있으면 그 ref, 고정 SHA, 확인된 source root)와 현재 워크로드 문맥을 바탕으로 읽기 전용 계획을 만든다.
4. 사용자가 같은 대화에서 계획을 명시적으로 확인한다.
5. MIM이 계획 해시와 현재 상태를 다시 검증한 뒤 중앙 프로젝트 안에서만 실행한다.
6. Claude가 작업 ID를 조회해 성공 또는 실패가 확정될 때까지 상태를 확인한다.

원시 API 키나 OAuth 비밀값은 Claude 대화나 MCP 인자에 넣지 않는다. MIM이 발급한 로그인된 브라우저 입력 화면에서만 값을 받고, 사용자 화면에는 이름·소유자·회전 상태 같은 메타데이터만 표시한다.

## 비용과 리소스 경계

| 항목 | 기본 정책 |
| --- | --- |
| 사용자 월 비용 목표 | 1,000원 |
| 조직 비상 정지선 | 10,000원 |
| 사용자 Cloud Run 서비스 | 최대 2개 |
| 사용자 스케줄 | 최대 3개 |
| 사용자 시크릿 | 기본 5개, 승인 포함 절대 최대 10개 |
| Cloud Run 인스턴스 | `min=0`, `max=1` |
| Cloud Run 기본 크기 | 1 vCPU, 512 MiB, concurrency 20 |

비용 정책은 UTC 기준 현재 달 비용만 한도에 반영한다. 이전 달 자료는 감사와 추세 확인을 위해 보존하지만 새 배포 차단 판단에는 합산하지 않는다. 사용자 직접 비용은 개인 한도에, 공용 플랫폼 비용은 조직 한도에만 반영한다.

`min=0`은 요청이 없을 때 인스턴스를 0개로 줄인다는 뜻이다. 사용자가 대시보드나 앱을 호출하면 Cloud Run이 다시 시작하므로 항상 켜 둔 VM이 필요하지 않다. 사용자가 말하는 “24시간 웹앱”은 상시 인스턴스 유지 요청이 아니라 요청 기반으로 언제든 다시 뜨는 웹앱을 뜻한다. 정기 실행은 컨테이너 안의 crontab이 아니라 Cloud Scheduler와 Cloud Run Jobs가 담당한다.

## 보안 경계

- Google Workspace가 사용자 수명주기의 기준이며 첫 production launch는 Slack 없이도 동작한다. Slack은 선택 연동일 뿐 로그인 권한의 기준이 아니다.
- 개인 Google/GCP/Cloudflare 계정과 회사의 기존 중요 프로젝트에는 접근하지 않는다.
- 런타임은 사람 계정, 사용자 ADC, 서비스 계정 키 파일을 사용하지 않는다.
- 모든 서비스 계정과 리소스 이름은 중앙 프로젝트의 MIM 접두사로 제한된다.
- 내부 BigQuery와 원본 비용 내보내기 데이터셋은 조회할 수 없다. 유지보수 계정은 별도 데이터셋의 정제된 단일 뷰 `mim_billing_secure.mim_usage_costs_v1`만 조회한다.
- 공개 제어 서비스의 Cloud Run IAM은 Worker 원본 도달을 위해서만 `allUsers` invoker와 `ingress=all`을 사용한다. 브라우저 인증은 애플리케이션 계층에서 수행하며 `/healthz`, `/readyz`를 포함한 모든 공개 경로가 Cloudflare Access JWT와 Worker HMAC을 모두 통과해야 한다. 직접 `run.app` 요청은 거부한다.
- 유지보수 Job과 개인 앱 같은 machine 서비스는 공개 제어 서비스의 원본 도달 예외를 공유하지 않으며 내부 ingress와 정확한 서비스 계정 IAM을 유지한다.
- 개인별 앱은 공개 Cloudflare -> Go 앱 게이트웨이 -> private Cloud Run IAM 경계로만 접근한다. 사용자 앱은 exact gateway invoker와 검토된 breakglass만 허용하며, legacy IAP 바인딩은 두지 않는다.
- 퇴사자는 디렉터리 동기화에서 즉시 격리되고, 7일 후 이전되지 않은 계산 리소스를 정리한다.
- 23일 미사용 시 경고하고 30일 미사용 시 상태 없는 계산 리소스를 정리한다. 컨테이너 이미지는 복구를 위해 유지한다.

## 저장소 구조

- `skills/`: Claude가 따라야 하는 대화·도구·확인 계약
- `control-plane/`: 인증, 계획, 실행, 상태, 비용, 수명주기, 대시보드
- `builder/`: 승인된 소스 스냅샷만 이미지로 만드는 제한된 빌더
- `edge/worker/`: Cloudflare Access 검증 뒤 원본 요청을 HMAC으로 서명하는 Worker
- `infra/domain/`: 도메인과 최초 Cloud Run 부트스트랩
- `infra/control-plane/`: 중앙 프로젝트와 최소권한 IAM 계획/적용
- `infra/billing/`: 원본 비용 내보내기를 숨긴 정제 뷰와 뷰 단위 읽기 권한 계획/적용
- `infra/github/`: 승인된 GitHub 연결 계획/적용
- `infra/edge/`: Google Workspace 기반 Cloudflare Access Managed OAuth와 원본 HMAC Worker
- `infra/runtime-bootstrap/`: Git에 비밀값을 남기지 않는 숫자 버전 고정 런타임 설정
- `infra/release/`: 이미지 출처, 런타임, 유지보수 Job, Scheduler의 검토형 릴리스

운영 순서와 장애 대응은 [운영 런북](docs/operations.md)을 따른다.

## 완료의 의미

로컬 테스트가 통과한 것과 실제 운영 배포가 끝난 것은 다르다. 다음 조건을 모두 만족하기 전에는 “운영 배포 완료”라고 표시하지 않는다.

- 회사 GCP 운영 계정 인증이 유효함
- 회사 Cloudflare 계정과 zone 소유권이 확인됨
- `madup.app` 권한 DNS가 정상 응답함
- 계획 파일이 `ready`이며 검토한 상태와 apply 직전 상태가 일치함
- 배포 후 인증, Worker 증명, 앱 게이트웨이 정책, 사용자 앱 IAM, 민감 프로젝트 거부 canary가 통과함
- 대시보드에서 세 정기 작업의 최근 성공 시각을 확인할 수 있음

## 수리와 정기 실행 해석

- 실패 원인 확인은 `explain_failure`로 시작한다. 드리프트의 상세 관찰값은 private 운영 신호라서 별도의 공개 repair 도구로 노출하지 않는다.
- 소스 수정이 필요한 경우, 검토된 GitHub branch push와 auto-deploy가 이미 연결된 워크로드만 기존 webhook이 새 배포를 큐잉한다.
- auto-deploy가 없거나 새 확인이 필요하면 `plan_deploy`를 다시 만들고 같은 대화에서 명시적 확인을 받은 뒤에만 실행한다.
- 어떤 경우에도 operation이 terminal 상태가 되기 전에는 “복구 완료”라고 말하지 않는다.
- 정기 실행 pilot은 정확히 `0 * * * *`와 `Asia/Seoul`만 지원한다. 커스텀 cron 요청은 범위 밖으로 거절한다.
