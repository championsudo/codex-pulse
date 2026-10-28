#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
for candidate in "$PWD/.venv/bin/python" "$PWD/.venv/bin/python3" python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
        exec "$candidate" "$PWD/server.py" --open "$@"
    fi
done
printf '%s\n' 'Python 3.10+ is required. Install Python, or create .venv with Python 3.10+ in this folder.' >&2
exit 1
