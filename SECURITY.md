# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub: **Security** tab →
**Report a vulnerability** on
[3sky/terrakube-selfservice](https://github.com/3sky/terrakube-selfservice/security/advisories/new).
Do not open a public issue. Include the version, a description, and steps to
reproduce. You will get an answer within a week.

## Supported versions

Security fixes go into the latest release. The chart's `image.tag` defaults to
the matching image version.

## What protects a release

- Every push and pull request runs the checks in `.github/workflows/security.yml`
  (SAST, dependency, secret, workflow, Dockerfile, chart and image scans);
  publishing waits for them.
- Dependencies are locked with hashes (`requirements.lock`); actions and base
  images are pinned to commit SHAs and digests; Dependabot proposes updates
  after a 14-day cooldown.
- Images and charts are signed with cosign (keyless) and carry SLSA
  provenance; images also carry an SPDX SBOM. See
  [Verifying releases](README.md#verifying-releases).

Past findings and their fixes: [SECURITY-REVIEW.md](SECURITY-REVIEW.md).
