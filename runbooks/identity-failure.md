# Workload identity failure

Tags: certificate, TLS, mTLS, CertificateRejected, expired, unknown CA, SAN, identity, cert-manager

A TLS peer refused a certificate: it has expired or is not yet valid, it was
issued by a CA the peer does not trust, or its name (SAN) is not one the peer
accepts. The workload's own configuration may be unchanged. The certificate
lives in a Secret that the Pod template only names, so rolling the template
back or restarting its Pods mounts the same Secret again and cannot repair the
identity. Escalate; never propose a rollback or a restart for this signal.

Gather the cert-manager Certificate that owns the Secret: its Ready condition
and reason, notBefore, notAfter, revision, issuerRef and dnsNames, and its
CertificateRequests and events. Do not read the Secret's key. Check whether the
cert-manager controller is running and whether its Issuer is Ready. Compare the
issuer and DNS names with the reviewed identity, and the trust bundle with the
CAs the peers should accept (during a CA rollover both the old and the new CA
are in the bundle until every certificate has been reissued).

Repairs are operator actions outside Sentinel: restore the controller or the
issuer so the certificate renews, revert an unreviewed change to the
Certificate's issuer or names, or put the issuing CA back into the trust bundle.
Envoy reloads renewed files from its watched directories without a restart, so
the workload's restart count should not change. Afterwards confirm the
Certificate is Ready with a new revision and that authenticated calls succeed.

This is general development guidance. Evaluation-case solutions and benchmark
labels must not be added to the retrieval corpus.
