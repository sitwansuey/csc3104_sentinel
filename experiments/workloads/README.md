# Validation workloads

These are retained for meaningful tests, not unused implementation stubs:

- `demo/`: small injectable HTTP startup-failure workload for restricted Executor
  RBAC/CAS/crash tests independent of Boutique.
- `otel/`: real SDK-emitting services for Observer traces/metrics/logs acceptance.
- `verifier-lab/`: connected services for Verifier dependency, sustained health
  and trace-correlation checks; its SDK requirements belong to its Docker build.
- `security/`: inspection gateways, certificates and candidate-image fixtures
  for the separate protected workload lab.
- `boutique/`: the vendored cart API schema (`cart.proto`) that the
  cartservice recovery probe passes to grpcurl, since cartservice offers no
  server reflection.

The primary evaluation app is upstream Boutique under `deploy/boutique/`.
See [testing](../../docs/testing.md) and [security](../../docs/security.md).
