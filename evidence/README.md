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
- `replay/v1/`: the incident replay corpus, generated from the bundles by
  `scripts/build_replay_corpus.py` (never edited by hand). It is not a bundle:
  its `corpus-manifest.json` lists every file's SHA-256, and part of each case
  is reconstructed, as the [replay guide](../docs/evaluation/replay/README.md)
  explains. `evaluation/recovery-ablation/<run>/` holds replay runs of it
  (`scripts/evaluate_recovery.py`): decision-level results only, never a
  recovery.

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
| agent | 41 | 21 | 20 | 0 | 0 |
| chaos-batch | 17 | 0 | 17 | 0 | 0 |
| chaos-recovery | 13 | 13 | 0 | 1 | 0 |
| connected-harness | 12 | 8 | 4 | 0 | 0 |
| distributed-acceptance | 11 | 11 | 0 | 0 | 0 |
| distributed-scenario | 39 | 0 | 39 | 0 | 0 |
| executor-admission | 1 | 0 | 1 | 0 | 0 |
| executor-wedge | 7 | 0 | 7 | 0 | 0 |
| guard-eval | 1 | 1 | 0 | 0 | 0 |
| guard-rejection | 4 | 2 | 2 | 0 | 0 |
| kind-lab-session | 3 | 0 | 3 | 0 | 0 |
| observer-acceptance | 3 | 0 | 3 | 0 | 0 |
| packaged | 9 | 0 | 9 | 0 | 0 |
| provider-recovery | 1 | 0 | 1 | 0 | 0 |
| scenario-pair | 8 | 8 | 0 | 0 | 0 |
| security-lab | 4 | 1 | 3 | 0 | 0 |
| shipping-lab | 2 | 0 | 2 | 0 | 0 |
| ui-session | 7 | 0 | 7 | 0 | 0 |
| worker-container | 2 | 0 | 2 | 0 | 0 |
<!-- END GENERATED:index -->

## Excluded runs

<!-- BEGIN GENERATED:exclusions -->
| Run | Class | Revision | Attempted at | Reason not collected |
| --- | --- | --- | --- | --- |
| executor-wedge-admission-deny-p2b-20261112T050456Z | executor-wedge | 5409092 | 2026-11-12T05:04:56Z | Infrastructure failure, not a Sentinel result: the Docker Desktop engine was down and the wedge harness stopped with a RuntimeError (its only log line); no run folder was kept. Rerun at the same revision as executor-wedge-admission-deny-p2b-20261112t050853z. |
| boutique-live-20261112T050457Z-1072d5 | agent | 5409092 | 2026-11-12T05:04:58Z | Infrastructure failure, not a Sentinel result: the Docker Desktop engine was down, so 'kind get nodes' failed before any experiment step (no checks ran and no fault was injected; only the preflight model warm-up ran, recorded in the run folder's model-runtime.json, and there was no RCA or planning model call). Its warm-up was a cold load of qwen3:14b (16.2 s), so the set rerun at this revision started with the model already loaded. The run folder keeps its FAILED summary. Docker was restarted and the set was rerun at the same revision (boutique-live-20261112t050930z-ba0f81, -051156z-009b69 and -051406z-ef3fbb). |
| boutique-live-20261112T050516Z-f67f76 | agent | 5409092 | 2026-11-12T05:05:17Z | Infrastructure failure, not a Sentinel result: the Docker Desktop engine was down, so 'kind get nodes' failed before any experiment step (no checks ran and no fault was injected; only the preflight model warm-up ran, recorded in the run folder's model-runtime.json, and there was no RCA or planning model call). The run folder keeps its FAILED summary. Docker was restarted and the set was rerun at the same revision (boutique-live-20261112t050930z-ba0f81, -051156z-009b69 and -051406z-ef3fbb). |
| boutique-live-20261112T050518Z-faa61d | agent | 5409092 | 2026-11-12T05:05:19Z | Infrastructure failure, not a Sentinel result: the Docker Desktop engine was down, so 'kind get nodes' failed before any experiment step (no checks ran and no fault was injected; only the preflight model warm-up ran, recorded in the run folder's model-runtime.json, and there was no RCA or planning model call). The run folder keeps its FAILED summary. Docker was restarted and the set was rerun at the same revision (boutique-live-20261112t050930z-ba0f81, -051156z-009b69 and -051406z-ef3fbb). |
| worker-image-build-0a8d4d7-20261112 | worker-container | 0a8d4d7 | 2026-11-12T16:10:34Z | A build, not a run: docker build of deploy/docker/worker.Dockerfile at this integration/phase3 revision failed in the image's own import check (ImportError: cannot import name 'scenarios' from 'experiments.boutique.harness', because the image did not copy the catalog modules agent_live imports), so no container started. This was a Sentinel defect, fixed in c973edd and merged in e5823f1; the 12 Nov container run (worker-container-planner-0a8d4d7-20261112) used an image built from c973edd, and the be6e636 image built cleanly. The time is the build log's last write; no run folder exists. |
| worker-container-task2-attempt0-be6e636-20261112T181009Z | worker-container | be6e636 | 2026-11-12T18:10:09Z | Operator tooling failure, not a Sentinel result: the scratch orchestrator stopped while making the throwaway TLS certificates, because the inherited OPENSSL_CONF named a file that did not exist (openssl req exited 1). Its own record shows it created no RBAC, container or volume and injected no fault; the only Sentinel step that ran was a kind_bundle restore that found the target already healthy at baseline (0.3 s, generation unchanged at 184), and a scan of its files found no credential. Rerun at the same revision with a private openssl.cnf as worker-container-planner-be6e636-20261112t181036z. |
| spike-pod-9d64aa1-20261113T082720Z | chaos-batch | 9d64aa1 | 2026-11-13T08:27:20Z | Operator spike, not a Sentinel run: a Chaos Mesh object created with kubectl on the connected lab's shippingservice before the P4.03 trials, with no Sentinel component running and no run folder; only the operator's console log was kept, and it is not committed. Valid: the PodChaos pod-failure record was named '<namespace>/<pod>' with desiredPhase Run and the Pod went 0/1 (the pause image cannot start under pull policy Never); after the operator deleted that Pod the replacement was not re-selected (the chaos object still listed only the old Pod) and was Ready within about 15 s. The result is recorded in docs/chaos.md (pre-registration commit c9a44ca). |
| spike-cpu-9d64aa1-20261113T083048Z | chaos-batch | 9d64aa1 | 2026-11-13T08:30:48Z | Operator spike, not a Sentinel run: a Chaos Mesh object created with kubectl on the connected lab's shippingservice before the P4.03 trials, with no Sentinel component running and no run folder; only the operator's console log was kept, and it is not committed. Invalid attempt: the StressChaos was applied (08:30:48Z) and recovered (08:35:48Z), but every Pod-metrics read failed, because the operator shell (Git Bash) rewrote the Metrics API path and the API server answered NotFound, so no CPU sample under stress exists. Repeated as spike-cpu3. |
| spike-cpu2-9d64aa1-20261113T083610Z | chaos-batch | 9d64aa1 | 2026-11-13T08:36:10Z | Operator spike, not a Sentinel run: a Chaos Mesh object created with kubectl on the connected lab's shippingservice before the P4.03 trials, with no Sentinel component running and no run folder; only the operator's console log was kept, and it is not committed. Invalid attempt: the metric reads worked (about 0.0005 cores at rest), but the StressChaos was never created, because kubectl create was given a file path that did not exist; every sample is a rest sample. Repeated as spike-cpu3. |
| spike-cpu3-9d64aa1-20261113T084314Z | chaos-batch | 9d64aa1 | 2026-11-13T08:43:14Z | Operator spike, not a Sentinel run: a Chaos Mesh object created with kubectl on the connected lab's shippingservice before the P4.03 trials, with no Sentinel component running and no run folder; only the operator's console log was kept, and it is not committed. Valid: about 0.0005 cores at rest; under the StressChaos (one worker, load 90) the shipping container held about 0.200 cores, its 200m limit, for the 300 s window and stayed Ready with no restart, so the pre-registered 0.18-core threshold was kept (docs/chaos.md, c9a44ca). |
| spike-redis-29e41f8-20261113T092828Z | chaos-batch | 29e41f8 | 2026-11-13T09:28:28Z | Operator spike for P4.05, not a Sentinel run: a PodChaos pod-failure on redis-cart created with kubectl on the connected lab, with no Sentinel component running and no run folder; the console log is kept by the operator and not committed. redis-cart stayed 0/1 for about 6 minutes while cartservice stayed 1/1 Ready with no restart (one Pod throughout), so a Redis outage alone did not make cartservice NotReady; recorded in docs/dual-fault.md. |
| boutique-pair-20261113T093435Z-2f30b6 | scenario-pair | 29e41f8 | 2026-11-13T09:34:36Z | Harness failure, not a Sentinel result: pair shipping-config+cart-pod applied both faults within 0.3 s and the Observer opened an incident for each target, then the harness stopped with ValueError 'Unknown Boutique observation stage', because runtime/boutique_observability.py did not accept the pair-fault stage of the storefront journal (the offline pair tests had mocked the journal). No proposal was planned, so there was no Guard decision and no Executor write. The chaos object's cleanup was confirmed and the administrator's emergency restore returned the configuration-faulted service, shippingservice, to its baseline. Fixed in 1941683 (merged in 02d91e5); the pair runs were repeated at dca7369 (C-DUAL-FAULT-PAIRS-DCA7369). No evidence class for pair runs exists in the collector at this revision, so the run folder (FAILED summary) stays local. |
| boutique-pair-20261113T093703Z-2ab18a | scenario-pair | 29e41f8 | 2026-11-13T09:37:04Z | Harness failure, not a Sentinel result: pair shipping-config+cart-pod applied both faults within 0.3 s and the Observer opened an incident for each target, then the harness stopped with ValueError 'Unknown Boutique observation stage', because runtime/boutique_observability.py did not accept the pair-fault stage of the storefront journal (the offline pair tests had mocked the journal). No proposal was planned, so there was no Guard decision and no Executor write. The chaos object's cleanup was confirmed and the administrator's emergency restore returned the configuration-faulted service, shippingservice, to its baseline. Fixed in 1941683 (merged in 02d91e5); the pair runs were repeated at dca7369 (C-DUAL-FAULT-PAIRS-DCA7369). No evidence class for pair runs exists in the collector at this revision, so the run folder (FAILED summary) stays local. |
| boutique-pair-20261113T093912Z-94e2cf | scenario-pair | 29e41f8 | 2026-11-13T09:39:13Z | Harness failure, not a Sentinel result: pair shipping-config+cart-pod applied both faults within 0.3 s and the Observer opened an incident for each target, then the harness stopped with ValueError 'Unknown Boutique observation stage', because runtime/boutique_observability.py did not accept the pair-fault stage of the storefront journal (the offline pair tests had mocked the journal). No proposal was planned, so there was no Guard decision and no Executor write. The chaos object's cleanup was confirmed and the administrator's emergency restore returned the configuration-faulted service, shippingservice, to its baseline. Fixed in 1941683 (merged in 02d91e5); the pair runs were repeated at dca7369 (C-DUAL-FAULT-PAIRS-DCA7369). No evidence class for pair runs exists in the collector at this revision, so the run folder (FAILED summary) stays local. |
| boutique-pair-20261113T094129Z-02c275 | scenario-pair | 29e41f8 | 2026-11-13T09:41:30Z | Harness failure, not a Sentinel result: pair redis-pod+cart-config applied both faults within 0.3 s and the Observer opened an incident for each target, then the harness stopped with ValueError 'Unknown Boutique observation stage', because runtime/boutique_observability.py did not accept the pair-fault stage of the storefront journal (the offline pair tests had mocked the journal). No proposal was planned, so there was no Guard decision and no Executor write. The chaos object's cleanup was confirmed and the administrator's emergency restore returned the configuration-faulted service, cartservice, to its baseline. Fixed in 1941683 (merged in 02d91e5); the pair runs were repeated at dca7369 (C-DUAL-FAULT-PAIRS-DCA7369). No evidence class for pair runs exists in the collector at this revision, so the run folder (FAILED summary) stays local. |
| boutique-pair-20261113T094347Z-79ea32 | scenario-pair | 29e41f8 | 2026-11-13T09:43:48Z | Harness failure, not a Sentinel result: pair redis-pod+cart-config applied both faults within 0.3 s and the Observer opened an incident for each target, then the harness stopped with ValueError 'Unknown Boutique observation stage', because runtime/boutique_observability.py did not accept the pair-fault stage of the storefront journal (the offline pair tests had mocked the journal). No proposal was planned, so there was no Guard decision and no Executor write. The chaos object's cleanup was confirmed and the administrator's emergency restore returned the configuration-faulted service, cartservice, to its baseline. Fixed in 1941683 (merged in 02d91e5); the pair runs were repeated at dca7369 (C-DUAL-FAULT-PAIRS-DCA7369). No evidence class for pair runs exists in the collector at this revision, so the run folder (FAILED summary) stays local. |
| boutique-pair-20261113T094614Z-3432a9 | scenario-pair | 29e41f8 | 2026-11-13T09:46:15Z | Harness failure, not a Sentinel result: pair redis-pod+cart-config applied both faults within 0.3 s and the Observer opened an incident for each target, then the harness stopped with ValueError 'Unknown Boutique observation stage', because runtime/boutique_observability.py did not accept the pair-fault stage of the storefront journal (the offline pair tests had mocked the journal). No proposal was planned, so there was no Guard decision and no Executor write. The chaos object's cleanup was confirmed and the administrator's emergency restore returned the configuration-faulted service, cartservice, to its baseline. Fixed in 1941683 (merged in 02d91e5); the pair runs were repeated at dca7369 (C-DUAL-FAULT-PAIRS-DCA7369). No evidence class for pair runs exists in the collector at this revision, so the run folder (FAILED summary) stays local. |
| boutique-pair-20261113T094829Z-1e8d84 | scenario-pair | 29e41f8 | 2026-11-13T09:48:30Z | Harness failure, not a Sentinel result: pair shipping-config+cart-pod (Executor-kill variant, shippingservice) applied both faults within 0.3 s and the Observer opened an incident for each target, then the harness stopped with ValueError 'Unknown Boutique observation stage', because runtime/boutique_observability.py did not accept the pair-fault stage of the storefront journal (the offline pair tests had mocked the journal). No proposal was planned, so there was no Guard decision and no Executor write. The chaos object's cleanup was confirmed and the administrator's emergency restore returned the configuration-faulted service, shippingservice, to its baseline. Fixed in 1941683 (merged in 02d91e5); the pair runs were repeated at dca7369 (C-DUAL-FAULT-PAIRS-DCA7369). No evidence class for pair runs exists in the collector at this revision, so the run folder (FAILED summary) stays local. |
| boutique-pair-20261113T095052Z-613964 | scenario-pair | 29e41f8 | 2026-11-13T09:50:53Z | Harness failure, not a Sentinel result: pair redis-pod+cart-config (Executor-kill variant, cartservice) applied both faults within 0.3 s and the Observer opened an incident for each target, then the harness stopped with ValueError 'Unknown Boutique observation stage', because runtime/boutique_observability.py did not accept the pair-fault stage of the storefront journal (the offline pair tests had mocked the journal). No proposal was planned, so there was no Guard decision and no Executor write. The chaos object's cleanup was confirmed and the administrator's emergency restore returned the configuration-faulted service, cartservice, to its baseline. Fixed in 1941683 (merged in 02d91e5); the pair runs were repeated at dca7369 (C-DUAL-FAULT-PAIRS-DCA7369). No evidence class for pair runs exists in the collector at this revision, so the run folder (FAILED summary) stays local. |
| protected-boutique-netpol-attempt1-02d91e5-20261113T101635Z | security-lab | 02d91e5 | 2026-11-13T10:16:35Z | Operator environment failure, not a Sentinel result: scripts/security/boutique_security.py start stopped while making the throwaway lab certificates, because the inherited OPENSSL_CONF named a temporary file that did not exist (openssl req failed). The sentinel-boutique-security cluster had been created, but no policy, probe or scan ran; the partial run folder (calico.yaml only) was removed. Rerun at the same revision with OPENSSL_CONF unset as protected-boutique-netpol. |
<!-- END GENERATED:exclusions -->
