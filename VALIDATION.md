# Publication validation

Validated on Windows on 2026-10-20 using the project's existing Python environment.

| Check | Result |
| --- | --- |
| Core Python suite, excluding native Tk layout tests | 112 passed |
| Minimum-window layout, separate process | 1 passed |
| Read-only selectable Chinese message rendering, separate process | 1 passed |
| Existing/new conversation selector, separate process | 1 passed |
| WorkBuddy Node extension fixture | Passed |

The three Tk tests are run in separate processes to avoid cross-thread cleanup of native Tk objects. See the exact commands in README.md.

The public snapshot includes the 2026-10-20 desktop source updates. A small minimum-window-height adjustment was made in this publication copy so the message area stays usable with all eight detected Agent cards.

These checks cover local code and controlled adapter fixtures. No live model calls were made for this publication, no third-party account compatibility is implied, and a new EXE was not built or released as part of the source upload.
