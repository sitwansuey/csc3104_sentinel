# Evidence

This folder holds sanitised evidence from live runs. Results tables in
[docs/results.md](../docs/results.md) and the claims in [claims.json](claims.json)
are generated from it, so every quoted number points back to a bundle here.

## Layout

- `<date>/<bundle_id>/`: one bundle per run. It holds allowlisted files copied
  from the run folder and a `MANIFEST.json` with the SHA-256 of every file, the
  run class, the sanitiser version and a provenance block.
- `index.json`: every bundle ever added, with its class, capture time, source
  revision, provenance and status.
- `claims.json`: quotable facts. Each has an id, a derived value, the bundles
  that support it and a mandatory scope caveat. Cite claim ids in the README,
  article, release notes and video script.
- `evaluation/latency/<date>/timings.json`: the per-stage latency ledger from
  `scripts/latency_report.py`. It is derived from the bundles, not a bundle.
  It holds timing numbers, roles, statuses, model tags and token counts only.
  `summarize.py --check` recomputes every value marked `bundle`; values marked
  `run_folder` came from local run folders and cannot be rechecked. Add a new
  dated folder when bundles are added; never edit an old ledger. `summarize.py`
  reads only the folder whose name sorts last, so a second ledger on the same
  date takes a suffix that sorts after the first (`2026-11-12b` after
  `2026-11-12`).

Files here are stored byte for byte (`-text` in `.gitattributes`), so the
MANIFEST hashes also hold on Windows checkouts.

## Policy

- **Append-only.** Bundles are never edited or silently deleted. To retire one,
  set its index status to `superseded` or `withdrawn` and give a `reason`.
  Withdrawn bundles drop out of the tables and any claim citing them fails.
- **Allowlist only.** `scripts/evidence/collect.py` copies only the files its
  class lists in `scripts/evidence/profiles.json`. Databases, `pki/`, key and
  certificate files and anything named like a kubeconfig are never copied, even
  if a profile asks for them.
- **Sanitised.** Values under token, password, secret, authorization, API-key,
  cookie and similar keys are replaced by `<redacted:hash>`, whatever their
  type. `secretName`, `secretKeyRef` and `secretRef` name a Secret, so they are
  kept. Also redacted: the `value` of a `name`/`value` pair with a sensitive
  name (env and header lists), the argument after a flag such as `--token`,
  and inline `token=...`, `"password": "..."`, `Authorization: ...` and
  `Cookie: ...` text, quoted or not, in logs and in JSON strings. JSON held
  inside a JSON string (such as `kubectl -o json` output) is checked like a
  document. Limits: `name`/`value` pairs are only found in JSON, not in YAML or
  other text, and a secret under an innocent key name is not detected, so
  human review still matters. PEM blocks, JWTs,
  kubeconfig key data, long base64 and known token shapes stop the collection
  with no partial bundle. Home and repository paths become `<home>` and
  `<repo>`, and local user names become `<user>`. From sanitiser version 5,
  private IPv4 addresses (RFC 1918 and link-local, such as kind Pod IPs) become
  `<private-ip>`; 127.0.0.1 is kept, and earlier bundles keep the addresses
  they were collected with. Files over 256 KB are left out and listed in the
  MANIFEST; a bundle over 3 MB is refused.
- **Failures stay in.** Failed, interrupted and incomplete runs are collected
  and count in every denominator.
- **Exclusions are recorded.** A run that is not collected is listed in
  `exclusions.json` with its class, revision, time and the reason, such as an
  infrastructure failure before any experiment step. `summarize.py --check`
  fails when an exclusion has no reason or names a collected bundle. The
  exclusions are rendered below.
- **Human review.** Skim a new bundle before committing it.

## Provenance

- `recorded`: the run wrote a provenance block itself (`shared/provenance.py`):
  the exact commit, whether the tree was dirty and the tool versions. Only
  recorded bundles should back release or article claims.
- `inferred`: the run files carry no provenance block, mostly because the run
  predates provenance stamping (the Executor wedge harness does not stamp one
  either). The revision is a best guess or an operator statement, and the
  MANIFEST `basis` says which. Treat these as history, not proof of a specific
  commit.

## Adding a bundle

```powershell
python scripts/evidence/collect.py --run <run folder> --class <class> --bundle-id <id>
python scripts/evidence/summarize.py
python scripts/evidence/summarize.py --check
```

The capture time comes from the run's own files. If they record none, a UTC
folder stamp (`20261110T213026Z`) is used. Packaged run folders
(`packaged-boutique-20261111-050418-...`) are named in local time with no zone,
so the collector refuses them unless you pass `--folder-utc-offset +08:00` (the
offset of the machine that ran them) or `--date`.

CI runs the `--check` form. It fails when a generated table was hand-edited or
is stale, a bundle no longer matches its MANIFEST, or a claim is unsupported.

## Bundles by class

<!-- BEGIN GENERATED:index -->
| Class | Bundles | Recorded | Inferred | Superseded | Withdrawn |
| --- | --- | --- | --- | --- | --- |
| agent | 39 | 19 | 20 | 0 | 0 |
| chaos-batch | 17 | 0 | 17 | 0 | 0 |
| connected-harness | 7 | 3 | 4 | 0 | 0 |
| executor-wedge | 7 | 0 | 7 | 0 | 0 |
| guard-eval | 1 | 1 | 0 | 0 | 0 |
| guard-rejection | 4 | 2 | 2 | 0 | 0 |
| observer-acceptance | 3 | 0 | 3 | 0 | 0 |
| packaged | 9 | 0 | 9 | 0 | 0 |
| provider-recovery | 1 | 0 | 1 | 0 | 0 |
| security-lab | 3 | 0 | 3 | 0 | 0 |
| shipping-lab | 2 | 0 | 2 | 0 | 0 |
| ui-session | 7 | 0 | 7 | 0 | 0 |
<!-- END GENERATED:index -->

## Excluded runs

<!-- BEGIN GENERATED:exclusions -->
| Run | Class | Revision | Attempted at | Reason not collected |
| --- | --- | --- | --- | --- |
| executor-wedge-admission-deny-p2b-20261112T050456Z | executor-wedge | 5409092 | 2026-11-12T05:04:56Z | Infrastructure failure, not a Sentinel result: the Docker Desktop engine was down and the wedge harness stopped with a RuntimeError (its only log line); no run folder was kept. Rerun at the same revision as executor-wedge-admission-deny-p2b-20261112t050853z. |
| boutique-live-20261112T050457Z-1072d5 | agent | 5409092 | 2026-11-12T05:04:58Z | Infrastructure failure, not a Sentinel result: the Docker Desktop engine was down, so 'kind get nodes' failed before any experiment step (no checks ran and no fault was injected; only the preflight model warm-up ran, recorded in the run folder's model-runtime.json, and there was no RCA or planning model call). Its warm-up was a cold load of qwen3:14b (16.2 s), so the set rerun at this revision started with the model already loaded. The run folder keeps its FAILED summary. Docker was restarted and the set was rerun at the same revision (boutique-live-20261112t050930z-ba0f81, -051156z-009b69 and -051406z-ef3fbb). |
| boutique-live-20261112T050516Z-f67f76 | agent | 5409092 | 2026-11-12T05:05:17Z | Infrastructure failure, not a Sentinel result: the Docker Desktop engine was down, so 'kind get nodes' failed before any experiment step (no checks ran and no fault was injected; only the preflight model warm-up ran, recorded in the run folder's model-runtime.json, and there was no RCA or planning model call). The run folder keeps its FAILED summary. Docker was restarted and the set was rerun at the same revision (boutique-live-20261112t050930z-ba0f81, -051156z-009b69 and -051406z-ef3fbb). |
| boutique-live-20261112T050518Z-faa61d | agent | 5409092 | 2026-11-12T05:05:19Z | Infrastructure failure, not a Sentinel result: the Docker Desktop engine was down, so 'kind get nodes' failed before any experiment step (no checks ran and no fault was injected; only the preflight model warm-up ran, recorded in the run folder's model-runtime.json, and there was no RCA or planning model call). The run folder keeps its FAILED summary. Docker was restarted and the set was rerun at the same revision (boutique-live-20261112t050930z-ba0f81, -051156z-009b69 and -051406z-ef3fbb). |
<!-- END GENERATED:exclusions -->
