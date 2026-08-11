#!/bin/bash
R2="https://cdn.qrf.cx/star"
LOCAL="/var/www/star/cdn/star"
DAYS="mon tue wed thu fri sat sun"

curl -sf "${R2}/manifest.json" -o "${LOCAL}/manifest.json"
mkdir -p "${LOCAL}/templates"
curl -sf "${R2}/templates/default.json" -o "${LOCAL}/templates/default.json"

CUTOFF=$(date -d '30 days ago' +%Y-%m-%d)
DATES=$(curl -sf "${R2}/manifest.json" | python3 -c "
import sys, json
for d in json.load(sys.stdin).get('days',[]):
    if d['date'] >= '$CUTOFF': print(d['date'])
")

for date in $DATES; do
  mkdir -p "${LOCAL}/${date}"
  for day in $DAYS; do
    curl -sf "${R2}/${date}/${day}.txt" -o "${LOCAL}/${date}/${day}.txt" 2>/dev/null
  done
  curl -sf "${R2}/${date}/meta.json" -o "${LOCAL}/${date}/meta.json" 2>/dev/null
done

# Local overrides win over whatever the remote had: the sync above overwrites
# cdn/star/<date>/<day>.txt, so any script edited through /api/save-script must
# be copied back on top of it, or the edit silently disappears on the next sync.
OVERRIDES="/var/www/star/content/overrides"
restored=0
if [ -d "$OVERRIDES" ]; then
  for dir in "$OVERRIDES"/*/; do
    [ -d "$dir" ] || continue
    date=$(basename "$dir")
    case "$date" in
      [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) ;;
      *) continue ;;
    esac
    for day in $DAYS; do
      src="${dir}${day}.txt"
      [ -f "$src" ] || continue
      mkdir -p "${LOCAL}/${date}"
      cp -f "$src" "${LOCAL}/${date}/${day}.txt" && restored=$((restored + 1))
    done
  done
fi

chown -R www-data:www-data "$LOCAL" 2>/dev/null
# curl/cp create files with the caller's umask, so group write is not guaranteed.
# Without it the API (ubuntu, in group www-data) can no longer rewrite synced
# files. X only sets +x on directories, so files stay non-executable.
chmod -R g+rwX "$LOCAL" 2>/dev/null
echo "Sync done: $(echo "$DATES" | wc -l) days, $restored override files reapplied"
