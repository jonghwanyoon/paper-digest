#!/usr/bin/env bash
# 매일 지정 시각(KST)에 `python3 -m digest run` 을 돌리는 systemd user timer 설치.
#   deploy/install-timer.sh            설치 + 활성화 (기본 07:00)
#   deploy/install-timer.sh 06:30      시각 지정
#   deploy/install-timer.sh --print    유닛 내용만 출력(설치 안 함)
# 로그아웃 상태에서도 돌게 하려면 한 번: sudo loginctl enable-linger "$USER"
set -euo pipefail

PRINT_ONLY=0
AT="07:00"
for arg in "$@"; do
  case "$arg" in
    --print) PRINT_ONLY=1 ;;
    [0-9][0-9]:[0-9][0-9]) AT="$arg" ;;
    *) echo "알 수 없는 인자: $arg" >&2; exit 1 ;;
  esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$(command -v python3)"
need() { command -v "$1" >/dev/null || { echo "$1 를 PATH 에서 찾을 수 없음" >&2; exit 1; }; }
need python3; need git

# systemd 는 로그인 셸 PATH 를 모르므로 필요한 실행 파일 디렉터리를 고정해 둔다(codex 는 node 필요).
path_dirs=()
for bin in python3 codex node claude git pdftotext; do
  if p="$(command -v "$bin" 2>/dev/null)"; then path_dirs+=("$(dirname "$p")"); fi
done
path_dirs+=(/usr/local/bin /usr/bin /bin)
PATH_VALUE="$(printf '%s\n' "${path_dirs[@]}" | awk '!seen[$0]++' | paste -sd:)"

SERVICE="[Unit]
Description=Journal paper daily digest (fetch → enrich → summarize → publish)
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$REPO
Environment=PATH=$PATH_VALUE
ExecStart=$PY -m digest run
TimeoutStartSec=8h
Nice=10
"

TIMER="[Unit]
Description=Run paper digest daily at $AT KST

[Timer]
OnCalendar=*-*-* $AT:00 Asia/Seoul
Persistent=true

[Install]
WantedBy=timers.target
"

if [[ $PRINT_ONLY == 1 ]]; then
  printf '# paper-digest.service\n%s\n# paper-digest.timer\n%s' "$SERVICE" "$TIMER"
  exit 0
fi

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"
printf '%s' "$SERVICE" > "$UNIT_DIR/paper-digest.service"
printf '%s' "$TIMER" > "$UNIT_DIR/paper-digest.timer"
systemctl --user daemon-reload
systemctl --user enable --now paper-digest.timer
systemctl --user list-timers paper-digest.timer --no-pager
if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]]; then
  echo
  echo "주의: linger 가 꺼져 있어 로그아웃하면 timer 가 멈춥니다. 한 번 실행: sudo loginctl enable-linger $USER"
fi
