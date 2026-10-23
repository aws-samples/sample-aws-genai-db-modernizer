#!/usr/bin/env bash
# Connect to the SOURCE Aurora MySQL. Interactive if no args, else runs SQL.
#   ./connect_mysql.sh                                   # interactive mysql prompt
#   ./connect_mysql.sh -e "SELECT COUNT(*) FROM products;"
#   ./connect_mysql.sh -e "SELECT id,status,price FROM products LIMIT 5;"
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; . "$DIR/00_env.sh"
exec mysql -h "$SRC" -u "$DBUSER" -p"$PW" "$DB" "$@"
