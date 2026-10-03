#!/usr/bin/env bash
# Reserve and print the next card id(s). Usage: new_card.sh [N]
# A thin front end of khms_alloc.py, the one locked allocator of the base (see there).
exec python3 "$(dirname "$(readlink -f "$0")")/khms_alloc.py" card "$@"
