"""``python -m yggdrasil.browser``: start Xvfb (unless a display exists) and serve the browser API.

Environment: ``YGG_BROWSER_PORT`` (8080), ``YGG_BROWSER_CHROME`` (``/usr/bin/google-chrome``),
``YGG_BROWSER_PROFILE`` (``/tmp/chrome-profile``, the warm patchright profile), ``YGG_BROWSER_MAX_PAGES`` (4).
"""

import os
import shutil
import subprocess
from pathlib import Path

import uvicorn

from yggdrasil.browser.service import NodriverEngine, PatchrightEngine, Verdicts, create_app

_DISPLAY = ":99"


def _start_xvfb() -> subprocess.Popen[bytes] | None:
    if os.environ.get("DISPLAY") or shutil.which("Xvfb") is None:
        return None
    # /tmp is an emptyDir that outlives a container restart: the previous Xvfb's lock and socket would make the new
    # one exit at once ("server already active"), leaving Chrome without a display.
    for stale in (Path("/tmp/.X99-lock"), Path("/tmp/.X11-unix/X99")):
        stale.unlink(missing_ok=True)
    proc = subprocess.Popen(["Xvfb", _DISPLAY, "-screen", "0", "1920x1080x24", "-nolisten", "tcp"])
    os.environ["DISPLAY"] = _DISPLAY
    return proc


def main() -> None:
    xvfb = _start_xvfb()
    verdicts = Verdicts()
    profile = Path(os.environ.get("YGG_BROWSER_PROFILE", "/tmp/chrome-profile"))
    # The warm profile outlives a container restart too; its singleton lock names a Chrome that no longer runs.
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        (profile / name).unlink(missing_ok=True)
    engines = {
        "patchright": PatchrightEngine(
            verdicts, str(profile), max_pages=int(os.environ.get("YGG_BROWSER_MAX_PAGES", "4"))
        ),
        "nodriver": NodriverEngine(verdicts, os.environ.get("YGG_BROWSER_CHROME", "/usr/bin/google-chrome")),
    }
    healthy = (lambda: xvfb.poll() is None) if xvfb is not None else (lambda: True)
    try:
        uvicorn.run(
            create_app(engines, healthy=healthy), host="0.0.0.0", port=int(os.environ.get("YGG_BROWSER_PORT", "8080"))
        )
    finally:
        if xvfb is not None:
            xvfb.terminate()


if __name__ == "__main__":
    main()
