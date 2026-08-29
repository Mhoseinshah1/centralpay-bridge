#!/usr/bin/env bash
set -euo pipefail

# Shared, fail-closed Trivy vulnerability scan for a locally built
# centralpay-bridge Docker image. Used identically by ci.yml (every pull
# request, before any release tag exists) and release.yml (on tag push) so
# the two workflows' vulnerability policy can never silently diverge --
# CVE-2026-14456 (found in the v0.6.0-rc3 tag's own base image) reached a
# release tag undetected specifically because ci.yml had no equivalent scan
# and only release.yml did.
#
# Trivy is pinned by tag AND verified digest: this script mounts the host
# Docker socket, so a tag that got silently republished (compromise or
# otherwise) would run different, unreviewed code with that access -- the
# required vulnerability scan itself could then be bypassed. The digest is
# the manifest-list digest for aquasec/trivy:0.58.0 (covers all published
# platforms; Docker resolves it to the runner's architecture at pull time,
# same as the tag would), confirmed via the Docker Hub v2 registry API's
# Docker-Content-Digest response header for that exact tag.
TRIVY_IMAGE="aquasec/trivy:0.58.0@sha256:b88012e2a0a309d6a8a00463d4e63e5e513377fb74eccbc8f9b0f8f81940ebeb"

image="${1:?usage: trivy-scan.sh <local-image-tag> [cache-dir]}"
cache_dir="${2:-${RUNNER_TEMP:-/tmp}/trivy-cache}"
mkdir -p "$cache_dir"

# --exit-code 1: fail closed on any finding at or above --severity.
# --ignore-unfixed: only vulnerabilities with an available fix are gates --
#   an unfixed CVE would just be a permanent, unactionable red build.
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "${cache_dir}:/root/.cache/trivy" \
  "$TRIVY_IMAGE" image \
  --format table \
  --exit-code 1 \
  --severity CRITICAL,HIGH \
  --ignore-unfixed \
  "$image"
