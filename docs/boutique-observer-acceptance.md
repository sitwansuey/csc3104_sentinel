# Boutique Observer acceptance — 26 October 2026

Run `python scripts/test_boutique_observer_acceptance.py` with Docker, kind and
kubectl available. The test creates only the dedicated `sentinel-boutique-lab`,
refuses to reuse an existing cluster with that name, deploys the real upstream
shippingservice v0.10.7, captures its healthy template, injects PORT=invalid-port,
then restores the exact template and removes the cluster. It exercises detection
only. No Planner, recovery authorization or automatic recovery is claimed.

Fresh evidence: `runs/boutique-observer-20261026T115202Z-0a29a5`.
The retained `images.json`, `healthy-baseline.json`, `policy.json`, per-poll
observations, `polls.json` and `summary.json` record image digests, workload UID,
readiness, timestamps, actual Pod/events/logs and persisted incident identity.

| Acceptance | Measured result |
| --- | --- |
| Healthy observation | 62.275 seconds elapsed; 25 healthy samples span 61.765 seconds; zero incidents |
| Threshold | 5 seconds, maximum sample gap 45 seconds |
| Detection | First eligible poll at 5.055710 seconds; preceding poll at 2.521780 seconds |
| Post-detection suppression | 61.403 seconds elapsed; 25 samples span 60.872 seconds; no additional incident |
| Persistence | Exactly one Coordinator incident, INC-615436542D38, with actual shippingservice target/evidence |
| Cleanup | Saved healthy template restored manually by harness; rollout readiness confirmed; dedicated cluster removed |

The existing sentinel-lab was retained. An earlier attempt exposed Windows
Unicode decoding of kind progress output; the shared Boutique command wrapper now
decodes UTF-8 with replacement rather than failing on Windows' legacy codepage.
Inconsistent multi-object reads during rollout reset the detection timer and
are retried within the bounded fault interval, preserving Observer semantics.

Regression: `python -m unittest tests.test_boutique_live_lab tests.test_observer
tests.test_observer_worker -v` (35 tests passed on the reviewed lab interfaces).
The new harness uses the same collector, durable ObserverStore and public
Coordinator sink as the existing live recovery experiment. Raw run files are
Git-ignored; share only sanitized evidence if needed. The dashboard's separate
#25 rehearsal inspected this incident and confirmed OPEN remains distinct from
workload readiness and RECOVERED requires an actual outcome.
