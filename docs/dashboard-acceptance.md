# Dashboard parent acceptance

Issue #22 is the parent integration deliverable. Existing PRs #26/#29/#32 supplied
the read-only frontend and live data. The remaining work is split by ownership,
with one independently reviewable PR per child issue. This parent change records
the combined acceptance evidence; it does not duplicate their implementation.

| Parent requirement | Implementation and evidence |
| --- | --- |
| Configured cluster names/status | Existing cluster selection and read-only Kubernetes adapter; live Receiving, Stale and Disconnected rehearsal in #25 / PR #44 |
| Selected namespace/workload/replicas/time | Existing Monitoring/Kubernetes views and identity filtering; live shippingservice inventory and incident drawer inspected in PR #44 |
| No missing-data health claims | Missing metrics remain unavailable; stopped collection displays LAST KNOWN; HTTP 503 displays Disconnected; OPEN incident stays unresolved without outcome |
| Server-side restricted read access | Existing collector/serving boundary; public Coordinator read interface added by #23 / PR #39, with no database creation/mutation and bounded reads |
| Loading/errors/refresh/tests/startup | Existing unit coverage plus 60 passing frontend tests, production/type-check build, responsive browser review in #24 / PR #43 and live refresh/outage rehearsal in PR #44 |
| Fixture mode and live instructions | Labelled `python scripts/dashboard.py --demo`; live connection instructions in the existing dashboard/Boutique guides; PR #44 adds the opt-in outage rehearsal |

Review/merge the child PRs #39, #43 and #44 before merging this parent acceptance
record. Backend/frontend owners should sign off on the documented response
contract in PR #39/#43 during review. That human agreement is not asserted by
the automated tests. Keep all issues open until their reviewed PRs merge.

This dashboard remains read-only. It exposes no browser approval, execution or
fault-injection control and sends no Kubernetes credentials to the client.
The new live rehearsal used actual Boutique detection evidence and explicitly
manual/harness restoration. It is separate from full six-component recovery,
RAG/LLM capability evaluation and distributed deployment.
