#!/usr/bin/env bash
# Local memory snapshots are retired. Restore individual files from the configured
# server version history; reconcile ledgers/counters and rebuild generated views.
echo "khms_snapshot: RETIRED; use server file versions (no local copy created)" >&2
exit 3
