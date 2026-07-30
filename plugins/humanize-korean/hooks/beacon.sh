#!/bin/sh
# 사용 집계 비콘 — 스킬명·버전·익명 uid만 전송. 프롬프트 내용은 수집하지 않는다.
SKILL_NAME="humanize-korean"
VER="1.5.1"
URL="https://asia-northeast3-dataconsulting-imagen2-test.cloudfunctions.net/plugin-beacon"

payload=$(cat)
case "$payload" in
  *"$SKILL_NAME"*) : ;;
  *) exit 0 ;;
esac
uid=$( (whoami; hostname) 2>/dev/null | /usr/bin/shasum 2>/dev/null | cut -c1-12 )
( curl -s -m 3 "$URL?skill=$SKILL_NAME&ver=$VER&uid=$uid" >/dev/null 2>&1 & )
exit 0
