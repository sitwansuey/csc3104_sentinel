# Release process

A release is a version tag, a set of images built from that tag on a
GitHub-hosted runner, and a GitHub release that a person has reviewed and
published. The first planned release is `v1.0.0-rc.1`, then `v1.0.0` after a
clean-machine rehearsal against the candidate's assets.

Status: the tooling below is in the repository and covered by offline tests.
No tag has been pushed, `release.yml` has not run, and no image from it has
been scanned or published yet (`L-NO-RELEASE` in [limitations](limitations.md)).

## Pieces

| File | Role |
| --- | --- |
| `VERSION` | The version being prepared, `X.Y.Z` or `X.Y.Z-rc.N`. `pyproject.toml` holds the same version in PEP 440 form (`1.0.0rc1`). |
| `scripts/release/policy.py` | The image build list, version rules, required evidence classes and waiver rules, shared by the scripts below. |
| `scripts/release/preflight.py` | Local go/no-go table before a tag. Changes nothing in git or on GitHub. |
| `scripts/release/notes.py` | Draft release notes from merged pull request titles, the limitations register and the evidence index. |
| `scripts/release/build.py` | Builds, scans and packages the images. CI runs it; it can also run locally as a pre-check. |
| `.github/workflows/release.yml` | Runs on a pushed version tag and creates a draft release. |
| `.trivyignore` | Reviewed, time-boxed scan waivers. Empty today. |

## What a person must approve

Nothing in this process pushes a tag or publishes a release by itself. A
maintainer with the owner's agreement does three things by hand:

1. **Merge the version bump.** A normal pull request changes `VERSION` and the
   `pyproject.toml` version.
2. **Push the tag**, only after preflight prints `GO` on the merged commit.
3. **Publish the draft release** after reading its notes, the scan summary and
   `release-manifest.json`.

Any scan waiver is also a human decision, made in a reviewed pull request.

## Steps

### 1. Preflight

On an up-to-date checkout of `main`:

```powershell
git fetch origin
git switch --detach origin/main
python scripts/release/preflight.py --tag v1.0.0-rc.1 --notes release-notes.md
```

`--notes` writes the draft notes outside the repository folder or into an
ignored path; a new file inside the checkout would make the next preflight
run fail its clean-tree check.

| Check | Fails when |
| --- | --- |
| `clean` | There are uncommitted changes or untracked files. |
| `branch` | `HEAD` is not `origin/main` (change it with `--base`). |
| `version` | `VERSION` is malformed, differs from the tag or from `pyproject.toml`, or does not sort above every existing version tag. A final release sorts above its candidates. |
| `tag` | The tag already exists. |
| `ci` | The newest `checks.yml` run for `HEAD` is missing, still running or not green. This uses read-only `gh run list`. |
| `evidence` | One of the classes `connected-harness`, `agent`, `guard-rejection`, `security-lab` or `guard-eval` has no current bundle with `recorded` provenance whose code is unchanged at `HEAD`. Code means `services`, `runtime`, `experiments`, `agent`, `shared`, `scripts`, `deploy`, `dashboard` and the requirement files; doc-only commits after the evidence are fine. |
| `waivers` | A `.trivyignore` entry has no issue link, no expiry, an expiry in the past or more than 90 days ahead. |
| `images` | A Dockerfile under `deploy/docker` is not in the build list, or a listed one is missing. |
| `notes` | The git history cannot be read. |
| `gate: ...` | Any command of the [quality gate](testing.md#quality-gates) fails. `pip-audit` needs the network and runs in CI only. |

The verdict is `GO` only when every check passes. A failed check gives
`NO-GO` (exit 1). `--skip` is allowed for a quick look, but the verdict is
then `INCOMPLETE` (exit 2), never `GO`.

On 12 November 2026 the `evidence` check fails. Four of the five classes have
only inferred bundles, and the one recorded `guard-eval` bundle predates later
changes under `agent/`. Fresh recorded runs on the candidate commit come first.

### 2. Tag

With `GO` on the merged commit, and the owner's explicit approval:

```powershell
git tag -a v1.0.0-rc.1 -m "Sentinel v1.0.0-rc.1"
git push origin v1.0.0-rc.1
```

### 3. The release workflow

`release.yml` starts on tags that look like `v1.2.3` or `v1.2.3-rc.4`. Its
`build` job has read-only repository access and:

1. Reruns the `version`, `waivers` and `images` preflight checks against the tag.
2. Runs `build.py`. For each image it runs `docker buildx build --platform
   linux/amd64 --load` with OCI `version`, `revision` and `source` labels and
   checks that they landed, then `docker image save`. The pinned Trivy (the
   digest in `scripts/security/scan_image_security.py`, shared with the
   security lab) downloads its database once and then, per image:
   - **Gate:** `--severity HIGH,CRITICAL --ignore-unfixed --ignorefile
     .trivyignore --exit-code 1`. A High or Critical finding with a fix
     available fails the release unless waived.
   - **Full report:** every severity, fixed or not, no waivers applied.
   - **SBOM:** CycloneDX.
   Every image is scanned even after one fails, so one run shows all findings.
3. Writes `release-manifest.json` and `SHA256SUMS`, then generates the notes.
4. Uploads everything as a workflow artifact, also when the gate fails, so
   the reports can be read.

The `draft` job runs only for a pushed tag and only when `build` passed. It
is the only job with `contents: write`. It checks `SHA256SUMS` and runs
`gh release create --draft`, marked as a pre-release for `-rc.N` tags.

Every action is pinned to a full commit SHA, like the other workflows. A
manual run (`workflow_dispatch`) builds and scans the `VERSION` on the chosen
branch as a rehearsal and never creates a release.

Findings with no fix do not block the release; they are in each full report
and the counts in the manifest. Third-party images (Online Boutique, kind
nodes, the grpcurl probe) are not built or scanned by this workflow.

### 4. Review and publish

Read the job summary table, the notes and `release-manifest.json`. Edit the
notes if needed, then publish the draft in the GitHub UI.

### Assets

| Asset | Content |
| --- | --- |
| `<image>-<version>.tar.gz` | `docker save` archive of one image. |
| `<image>-<version>.cdx.json` | CycloneDX SBOM. |
| `<image>-<version>.trivy.json` | Full Trivy report, every severity. |
| `<image>-<version>.gate.json` | The gate's findings; empty when it passed. |
| `release-manifest.json` | Tag, source revision, CI run URL, Trivy version and database date, waivers, and per image the image ID, OS, architecture, OCI labels, gate result and finding counts. |
| `SHA256SUMS` | SHA-256 of every other asset, in `sha256sum` format. |

The images are `sentinel-boutique-runner`, `sentinel-coordinator`,
`sentinel-guard`, `sentinel-observer` and `sentinel-security-inspector`,
each tagged with the version. Nothing is pushed to a registry; the repository
is private, so archives plus checksums match the offline transfer in
[packaging](packaging.md).

## Recording the release under evidence/

After publishing, copy the small, verifiable records into
`evidence/releases/<tag>/` in a doc-only pull request:

- `release-manifest.json`: image IDs, labels, Trivy version, database date,
  gate result and counts.
- `SHA256SUMS`: lets anyone check the archives, SBOMs and full reports that
  stay on the GitHub release.
- `*.gate.json`: the gate's own output.

These are copied byte for byte (`evidence/** -text`) and never edited. They
are not run bundles, so they sit outside `index.json`. The archives and full
reports are too large for the evidence policy and stay as release assets.

## Using a release on another machine

Download the assets and verify them before loading:

```powershell
Get-FileHash .\sentinel-boutique-runner-1.0.0-rc.1.tar.gz -Algorithm SHA256
docker load --input .\sentinel-boutique-runner-1.0.0-rc.1.tar.gz
docker image inspect sentinel-boutique-runner:1.0.0-rc.1 --format "{{.Os}}/{{.Architecture}} {{index .Config.Labels \"org.opencontainers.image.revision\"}}"
```

Compare the hash with `SHA256SUMS` and the revision with the manifest. On
Docker Desktop with the containerd image store, `docker image inspect` may
show a manifest digest as the ID instead of the configuration digest that the
manifest records, so the archive hash and the revision label are the checks
to rely on. The packaged launcher still names `sentinel-boutique-runner:0.3.0`
and its Dockerfile still carries a `0.3.0` version label that the release
build overrides; until the launcher reads `VERSION`, `docker tag` the loaded
image to that name before `demo-start.ps1 -SkipBuild`.

## When something goes wrong

- **Gate fails:** fix the image (usually a base or dependency bump) and cut
  the next candidate. A waiver is a last resort: a `.trivyignore` entry under
  a comment naming the issue, with an expiry at most 90 days ahead.
- **Draft is wrong:** delete the draft release. The tag name is not reused;
  prepare `rc.N+1`.
- **Published release is wrong:** never move or reuse a published tag.
  Publish the next version and say in its notes what it replaces.

Builds are not bit-reproducible, so a local `build.py` run gives different
image IDs. The CI build is the release artefact; a local run is only a
pre-check. Private-repository Actions minutes are limited on the free plan;
tag-only triggers keep the cost small.
