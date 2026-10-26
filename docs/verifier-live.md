# Live Verifier evidence sources

Issue #21 provides bounded HTTP health collection, Kubernetes Deployment identity and readiness checks, and OTLP trace dependency extraction. Missing evidence remains unknown. Inventory coverage must be explicitly declared by the operator.

Run `python -m services.verifier_live_cli --help` for sample, dependencies and verification commands. `samples/verifier-live-inventory.json` documents configuration. Run `python scripts/demo_verifier_http.py` for a local HTTP demonstration.

Trace input is an OTLP JSON export supplied by file or HTTP GET, not a Collector ingestion endpoint. Collection checks resource identity and configuration before and after reads, and retains template hashes, availability and source provenance.

Durable worker integration and connected acceptance testing are tracked separately in #30.
