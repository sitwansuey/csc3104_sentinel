BEGIN TRANSACTION;
CREATE TABLE actions (
                action_id TEXT PRIMARY KEY, incident_id TEXT NOT NULL
                    REFERENCES incidents(incident_id),
                proposal TEXT NOT NULL, state TEXT NOT NULL,
                guard_result TEXT, execution_result TEXT
            );
INSERT INTO "actions" VALUES('ACT-DEMO-001','INC-DEMO-001','{"action":"ROLLBACK","action_id":"ACT-DEMO-001","based_on_resource_version":"102","evidence_refs":["E-POD-1","E-LOG-1"],"incident_id":"INC-DEMO-001","kind":"proposal","parameters":{"baseline_id":"BASE-DEMO-001","template_sha256":"a9091159ce70bcf4679f6fa7a70c33ce7169e8dd3296ec647d9bfee47c9445bb"},"rationale":"Repeated startup failures after a template change; an observed healthy baseline is retained.","schema_version":"1.0","target":{"cluster_id":"sentinel-lab","kind":"Deployment","name":"demo-api","namespace":"sentinel-demo","uid":"synthetic-demo-uid-001"}}','VALIDATING',NULL,NULL);
CREATE TABLE approval_revocations (
                approval_id INTEGER PRIMARY KEY REFERENCES approvals(id),
                record TEXT NOT NULL
            );
CREATE TABLE approvals (
                id INTEGER PRIMARY KEY, action_id TEXT NOT NULL
                    REFERENCES actions(action_id), record TEXT NOT NULL
            );
CREATE TABLE dependency_revalidations (
                action_id TEXT PRIMARY KEY REFERENCES actions(action_id),
                dependency_sha256 TEXT NOT NULL
            );
CREATE TABLE guard_reports (
                decision_sha256 TEXT PRIMARY KEY, action_id TEXT NOT NULL
                    REFERENCES actions(action_id), report TEXT NOT NULL
            );
CREATE TABLE incidents (
                incident_id TEXT PRIMARY KEY, target TEXT NOT NULL,
                state TEXT NOT NULL, active_action_id TEXT
            );
INSERT INTO "incidents" VALUES('INC-DEMO-001','{"cluster_id":"sentinel-lab","kind":"Deployment","name":"demo-api","namespace":"sentinel-demo","uid":"synthetic-demo-uid-001"}','VALIDATING','ACT-DEMO-001');
CREATE TABLE managed_approvals (
                approval_id INTEGER PRIMARY KEY REFERENCES approvals(id)
            );
CREATE TABLE messages (
                event_key TEXT PRIMARY KEY, payload TEXT NOT NULL
            );
INSERT INTO "messages" VALUES('["incident","INC-DEMO-001",""]','{"incident_id":"INC-DEMO-001","kind":"incident","observed_at":"2026-10-10T12:00:30Z","observed_generation":2,"observed_resource_version":"102","schema_version":"1.0","signals":[{"at":"2026-10-10T12:00:30Z","detail":"One desired replica, zero ready; repeated exit code 1","evidence_id":"E-POD-1","reason":"CrashLoopBackOff","source":"pod_status"},{"at":"2026-10-10T12:00:30Z","detail":"Invalid startup configuration; process exited with code 1","evidence_id":"E-LOG-1","reason":"StartupError","source":"container_log"}],"target":{"cluster_id":"sentinel-lab","kind":"Deployment","name":"demo-api","namespace":"sentinel-demo","uid":"synthetic-demo-uid-001"}}');
INSERT INTO "messages" VALUES('["proposal","ACT-DEMO-001",""]','{"action":"ROLLBACK","action_id":"ACT-DEMO-001","based_on_resource_version":"102","evidence_refs":["E-POD-1","E-LOG-1"],"incident_id":"INC-DEMO-001","kind":"proposal","parameters":{"baseline_id":"BASE-DEMO-001","template_sha256":"a9091159ce70bcf4679f6fa7a70c33ce7169e8dd3296ec647d9bfee47c9445bb"},"rationale":"Repeated startup failures after a template change; an observed healthy baseline is retained.","schema_version":"1.0","target":{"cluster_id":"sentinel-lab","kind":"Deployment","name":"demo-api","namespace":"sentinel-demo","uid":"synthetic-demo-uid-001"}}');
CREATE TABLE outbox (
                id INTEGER PRIMARY KEY, topic TEXT NOT NULL, payload TEXT NOT NULL,
                action_id TEXT, delivered INTEGER NOT NULL DEFAULT 0
            );
INSERT INTO "outbox" VALUES(1,'planner.request','{"incident":{"incident_id":"INC-DEMO-001","kind":"incident","observed_at":"2026-10-10T12:00:30Z","observed_generation":2,"observed_resource_version":"102","schema_version":"1.0","signals":[{"at":"2026-10-10T12:00:30Z","detail":"One desired replica, zero ready; repeated exit code 1","evidence_id":"E-POD-1","reason":"CrashLoopBackOff","source":"pod_status"},{"at":"2026-10-10T12:00:30Z","detail":"Invalid startup configuration; process exited with code 1","evidence_id":"E-LOG-1","reason":"StartupError","source":"container_log"}],"target":{"cluster_id":"sentinel-lab","kind":"Deployment","name":"demo-api","namespace":"sentinel-demo","uid":"synthetic-demo-uid-001"}}}',NULL,1);
INSERT INTO "outbox" VALUES(2,'guard.request','{"proposal":{"action":"ROLLBACK","action_id":"ACT-DEMO-001","based_on_resource_version":"102","evidence_refs":["E-POD-1","E-LOG-1"],"incident_id":"INC-DEMO-001","kind":"proposal","parameters":{"baseline_id":"BASE-DEMO-001","template_sha256":"a9091159ce70bcf4679f6fa7a70c33ce7169e8dd3296ec647d9bfee47c9445bb"},"rationale":"Repeated startup failures after a template change; an observed healthy baseline is retained.","schema_version":"1.0","target":{"cluster_id":"sentinel-lab","kind":"Deployment","name":"demo-api","namespace":"sentinel-demo","uid":"synthetic-demo-uid-001"}}}','ACT-DEMO-001',0);
CREATE TABLE transport_dependencies (
                guard_event_id INTEGER PRIMARY KEY REFERENCES outbox(id),
                dependency_event_id INTEGER NOT NULL REFERENCES outbox(id));
CREATE TABLE transport_receipts (
            event_id INTEGER PRIMARY KEY REFERENCES outbox(id),
            delivery_id TEXT UNIQUE NOT NULL, owner TEXT, lease_token TEXT,
            attempts INTEGER NOT NULL DEFAULT 0, expires REAL NOT NULL DEFAULT 0,
            available REAL NOT NULL DEFAULT 0, finished REAL, result_digest TEXT);
COMMIT;
