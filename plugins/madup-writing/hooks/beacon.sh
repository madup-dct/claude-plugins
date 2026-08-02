#!/bin/sh
# 사용 집계 비콘 — 스킬명·버전·익명 uid만 전송. Prompt 내용은 수집하지 않는다.
SKILL_NAME="madup-writing"
VER="1.2.0"
URL="https://asia-northeast3-dataconsulting-imagen2-test.cloudfunctions.net/plugin-beacon"

payload=$(cat)
# tool_input의 skill 필드만 비교한다 — 다른 스킬의 args에 스킬명이 언급된 경우의 과집계 방지.
skill=$(printf '%s' "$payload" | sed -n 's/.*"skill"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)
case "$skill" in
  *"$SKILL_NAME"*) : ;;
  *) exit 0 ;;
esac
uid=$( (whoami; hostname) 2>/dev/null | /usr/bin/shasum 2>/dev/null | cut -c1-12 )
( curl -s -m 3 "$URL?skill=$SKILL_NAME&ver=$VER&uid=$uid" >/dev/null 2>&1 & )
exit 0
