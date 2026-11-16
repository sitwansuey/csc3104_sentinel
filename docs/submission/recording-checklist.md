# Recording checklist

Use with the [storyboard](video-storyboard.md). Tick each item for every take.

## Nothing private on screen

A recording cannot be redacted after upload. Before recording, and again when
reviewing the final cut (`M-SCREEN-REVIEW` in [deliverables.json](deliverables.json)):

- [ ] Clone the release into a short neutral folder such as `C:\sentinel`, so
      printed paths contain no user name. Never show a path under the home folder.
- [ ] Use a neutral terminal prompt for the session, for example
      `function prompt { 'PS> ' }`, and a terminal window without a user name or
      machine name in its title.
- [ ] Never open or print credentials: no kubeconfig files (including the
      `operator-kubeconfig.yaml` the packaged runner writes), no
      `kubectl config view --raw`, no Executor credential files from the run
      folder, no `Get-ChildItem env:` or `docker inspect` output, no
      `.env` files, no browser developer tools showing request headers.
- [ ] No access tokens, session cookies or API keys in any window, including
      GitHub, the course system, a password manager or a chat client.
- [ ] Desktop notifications off; close mail, chat and unrelated browser tabs.
- [ ] The dashboard shows no kubeconfig or database paths by design; still check
      the incident drawer and event list before each take.

## Before recording

- [ ] Check out the release tag and build the dashboard (steps S1-S5).
- [ ] Do one complete unrecorded run (step B2) to fill the image caches.
- [ ] For agent mode, confirm `qwen3:14b` is installed (step C1). The harness
      warms the model before the incident; allow for the slow first load.
- [ ] Ports 8765 and 8080 are free (step T1).
- [ ] Screen at 1920x1080; browser zoom large enough to read the dashboard.
- [ ] Note the release tag and keep the terminal line that names the run folder.

## During recording

- [ ] Record each storyboard segment as a separate take, so a failed live run
      only costs one segment. Keep spare takes of the live scenarios.
- [ ] Do not change the cluster by hand during a recovery.
- [ ] Where waiting is cut, show the real duration on screen.

## After recording

- [ ] Keep the run folder of every take that is used, and collect it as an
      evidence bundle (`scripts/evidence/collect.py`); cite that bundle.
- [ ] A second person watches the whole cut for anything private (see above).
- [ ] Check the file: 10-15 minutes, audio present, at least 1080 lines:
      `python scripts/submission/check.py --video-file <file>`.
- [ ] Upload through the course system or an unlisted link, never into the
      repository, and record the link as `video_link` in `deliverables.json`.
