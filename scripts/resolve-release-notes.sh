#!/usr/bin/env bash
# Prints the release-notes filename for a given application version, e.g.
# "0.6.0-rc2" -> "RELEASE_NOTES_0.6.0_RC2.md". Used by
# .github/workflows/release.yml (both the required-documents check and the
# draft-release creation step) so a later release candidate can never
# silently reuse an older RC's release notes -- the mapping is derived from
# APP_VERSION, not hardcoded per-release in the workflow. Historical
# RELEASE_NOTES_*.md files for prior lines are untouched by this script.
set -euo pipefail

version="${1:?usage: resolve-release-notes.sh <APP_VERSION>}"
suffix=$(printf '%s' "$version" | sed -E 's/-rc([0-9]+)$/_RC\1/')
printf 'RELEASE_NOTES_%s.md\n' "$suffix"
