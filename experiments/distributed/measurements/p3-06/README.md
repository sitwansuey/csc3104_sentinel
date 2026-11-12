# P3.06 measurements (file mode, one host)

Reports written by `python -m experiments.distributed.claims ... --out FILE`
on 12 November 2026, on one Windows host with Python 3.13, while other test
runs shared the machine. The Coordinator and five workers ran as separate
processes over HTTPS on loopback. They are not separate machines, and no
cluster was involved. The numbers are summarised in the
[distributed model](../../../../docs/distributed-model.md#recovery-time-and-restore-measured-on-one-host).

| File | Code | Command |
| --- | --- | --- |
| `before-restart-poll2-down{0,30,75}.json` | the base commit (`bab8563`), back-off up to 60 s without jitter | `restart --repeat 5 --poll-seconds 2 --down-seconds 0` (and 30); `--repeat 3 --down-seconds 75` |
| `after-restart-poll2-down{0,30,75}.json` | this change, before it was committed (the reports name the base commit) | the same |
| `before-rate-{1,2}.json`, `after-rate-{1,2}.json` | base commit, this change | `rate --backlog 50,200,1000 --clients 6`; run 1 of the base commit ran while the host was heavily loaded |
| `after-restore-staged.json` | this change | `restore --repeat 5`: the workers holding replay items are cleared first |
| `after-restore-allatonce-{1,2}.json` | this change | `restore --repeat 3` before staged clearing existed, and `restore --repeat 5 --all-at-once` |

`git_revision` in the "after" reports is the base commit because the change
was not yet committed when they ran. `first_claim_seconds` counts from the
restarted Coordinator answering `/readyz` to each worker's first successful
claim, read from the Coordinator's own access log.
