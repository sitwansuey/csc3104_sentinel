# Video storyboard

A plan for a 10-15 minute video (target 13:30) built on the browser-driven
connected run (instructions step B1) plus a separately recorded agent-mode
segment (step C4). The team narrates, records and edits; the **Narration**
column is theirs to write. Prepare with the [recording checklist](recording-checklist.md).

Rules for every segment:

- A number that is said or shown comes from a claim id in
  [evidence/claims.json](../../evidence/claims.json) or a generated table in
  [results](../results.md), and its scope caveat is said or shown with it.
- Live footage comes from a run on the release tag. Keep that run's folder and
  collect it as an evidence bundle, so the footage can be traced to it.
- APPLIED and RECOVERED are shown as two separate states.
- Waiting time may be cut, but the real duration is shown on screen where a cut
  happens.

| Time | Segment | On screen | Step | Evidence | Narration |
| --- | --- | --- | --- | --- | --- |
| 0:00-1:00 | Problem and team | Title; the role table from the README | none | none | TODO (team) |
| 1:00-2:30 | Architecture | `docs/architecture.md`; one slide of failure and timing assumptions from `docs/distributed-model.md` | none | none | TODO (team) |
| 2:30-3:30 | Healthy baseline | Dashboard Overview at 127.0.0.1:8765/#overview and the storefront at 127.0.0.1:8080; lab setup cut, real duration shown | B1 | none | TODO (team) |
| 3:30-6:00 | Scenario 1: invalid PORT | Button 1; checkout fails in the storefront; detection; proposal; Guard report; Executor patch (APPLIED); Verifier (RECOVERED) | B1 | `C-CONNECTED-HARNESS`, `C-OBSERVER` | TODO (team) |
| 6:00-7:30 | Scenario 2: crash after write | Button 2; the Executor exits after its patch; the restart reconciles without a second write | B1 | `C-CONNECTED-HARNESS` | TODO (team) |
| 7:30-8:30 | Scenario 3: unnecessary rollback | Button 3; the Guard's rejection and its reasons; zero writes | B1 | `C-GUARD-REJECTION` | TODO (team) |
| 8:30-10:30 | Agent mode | Separate take: the agent's planning events, its proposal (`model-proposal.json` in the run folder) and the Guard's decision; model wait cut with the real duration shown; say that the approval is typed at the terminal through the authenticated approval API, not a login, and that the committed agent runs used pre-authorised lab approval | C4 | `C-AGENT-WARM` with `C-AGENT-ALL`; `C-AGENT-FAIL-CLOSED` | TODO (team) |
| 10:30-11:15 | Safety at the API | The dashboard's permission checks; the Recovery checks rows for restricted credentials and the stale-resource-version rejection | B1 | [Recovery checks](../results.md#recovery-checks) | TODO (team) |
| 11:15-12:15 | Distribution and security | The distributed model's tested scope (one host, `L-SINGLE-HOST-DISTRIBUTION`); 30 seconds of the security lab, which is a separate experiment | D2, D3 | `C-SECURITY-LAB` | TODO (team) |
| 12:15-13:15 | Results and limitations | The claims table in results and the limitations list | none | [Claims](../results.md#claims), [limitations](../limitations.md) | TODO (team) |
| 13:15-13:30 | Close | Repository and release | none | none | TODO (team) |

Chapters for the video description can be taken from the segment start times
above, adjusted to the final cut.

Not yet available: the dashboard does not show the run id or source revision on
screen. Until it does, show the run folder's name (not its full path) and the
release tag in the terminal at the start of each take.
