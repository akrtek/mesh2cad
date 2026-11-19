# Changelog

## 0.2.0 (2026-11-10)

First version for outside testers.

- **Progress you can trust.** Two progress bars (this file / all files) are driven by the converter's real stages. A status line shows what it's doing, e.g. "Finding curved surfaces · 812 of 4,113 regions checked · time budget 3:12 left". A heartbeat shows the worker's CPU and memory, and it warns if the worker goes quiet for 45 s.
- **Per-stage timings** in the activity log after each body, so slow models show where the time went.
- **No more frozen-looking window in the background (macOS).** App Nap is disabled while a conversion runs.
- **New look:** lighter charcoal background, with the sections stacked into one column of translucent blue-to-violet tints that get a little lighter going down. Proper checkmarks replace the old ✕-style boxes.
- **Fits laptop screens:** on screens under 1050 px tall, Conversion options start folded into a one-line summary (click to change them). Summary and Activity log share the remaining space, each with a minimum size.
- **Feedback button:** copies version, settings and the log, then opens the feedback form.
- **Copy Log** button. The log is capped at 5,000 lines in long sessions.
- **Sturdier runs:**
  - Stop kills a stuck worker.
  - Closing the window during a conversion asks first and never leaves a conversion running in the background.
  - An error inside the app can no longer leave it stuck in "running".
- **Downloadable apps** for macOS (Apple Silicon and Intel) and Windows, built and checked automatically on GitHub.
- The STEP writer's internal messages no longer leak into the log.
- Windows support for Open STEP / Show File.

## 0.1.0 (2026-11-08)

- First version: mesh → STEP converter (prismatic / planar / faceted), command line and Tk desktop app.
