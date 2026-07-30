#!/bin/sh
# 플러그인 사용 리포트 — 최근 7일/누적 호출·사용자 + GitHub 트래픽
# 필요 권한: BQ 조회(dataconsulting-imagen2-test) + repo 관리자(gh)
echo "== 최근 7일 스킬 호출 =="
bq --project_id=dataconsulting-imagen2-test query --use_legacy_sql=false --format=pretty '
SELECT skill, COUNT(*) AS calls, COUNT(DISTINCT uid) AS users
FROM plugin_telemetry.calls
WHERE ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
  AND skill NOT LIKE "\\_%"
GROUP BY skill ORDER BY calls DESC'

echo "== 누적 =="
bq --project_id=dataconsulting-imagen2-test query --use_legacy_sql=false --format=pretty '
SELECT skill, COUNT(*) AS calls, COUNT(DISTINCT uid) AS users,
       MIN(ts) AS first_call, MAX(ts) AS last_call
FROM plugin_telemetry.calls
GROUP BY skill ORDER BY calls DESC'

echo "== GitHub 트래픽 (14일 롤링: 설치·업데이트 프록시) =="
gh api repos/madup-dct/claude-plugins/traffic/clones --jq '{clones_14d: .count, unique_cloners: .uniques}'
