# Optional protected-workload experiments

Security code is retained as a separate, opt-in experiment. It does not complete
the Sentinel guarded recovery chain on protected templates. The current baseline
still uses restricted repair RBAC, strict Guard checks, non-root demo workloads,
scoped contexts, bounded reads and private test credentials.

`scripts/security/` contains the inspection labs, scan/candidate tools and scan
monitor. `services/grpc_inspection.py` and `services/image_security.py` own their
decision/validation logic; the dashboard projects their journaled events.

For the full protected Boutique lab, inspect help and use a fresh evidence path:

```powershell
python scripts/security/boutique_security.py --help
python scripts/security/boutique_security.py start --directory data/experimentation/runs/protected-boutique --exit-after-test
python scripts/security/boutique_security.py test --directory data/experimentation/runs/protected-boutique
python scripts/security/boutique_security.py stop --directory data/experimentation/runs/protected-boutique
```

Run these steps separately. Without `--exit-after-test`, `start` keeps serving
the storefront and dashboard until stopped, and `test` would then fail because
the default ports are taken; use `serve` to reopen the dashboard later, or give
`test` other ports with `--storefront-port` and `--dashboard-port`. OpenSSL
must be on PATH for certificate generation; if it fails to open an
`openssl.cfg`, a stale `OPENSSL_CONF` environment variable is the usual cause.
The script owns a dedicated Calico/kind lab with mTLS and inspected shipping
traffic. Only the shipping path is protected: the other Boutique services keep
their upstream plaintext connections and have no NetworkPolicy. The smaller `scripts/security/security_lab.py`
supports `start`, `test` and `stop` too. Normal connected recovery does not
install either overlay. Read each module's setup/ownership checks before use.

Image scan/candidate workflows remain explicit. A candidate needs reviewed
metadata and adjacent passing scan evidence; a scan does not grant repair
authorization. The monitor's inventory/freshness and candidate rollback handling
remain incomplete for a production release. `--help` on
`scan_image_security.py`, `build_security_candidate.py` and `watch_security.py`
documents the exact arguments.

No new protected live run or CVE scan was performed in this review. Lab
certificates are short-lived fixtures, not a rotation/revocation/workload-identity
system. Availability/capacity, current-image reconciliation and protected
Guard/Executor/Verifier integration remain tracked work. Keep credentials,
private keys and unsanitised logs out of Git and exported image contexts.
