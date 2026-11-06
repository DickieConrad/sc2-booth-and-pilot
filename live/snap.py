"""Save a screenshot of the primary monitor to out/snap.png (to check the capture and the panel region)."""
from pathlib import Path

import mss
import mss.tools

out = Path(__file__).with_name("out")
out.mkdir(exist_ok=True)
with mss.mss() as sct:
    mon = sct.monitors[1]
    img = sct.grab(mon)
    mss.tools.to_png(img.rgb, img.size, output=str(out / "snap.png"))
    print(f"monitor {mon} -> {out / 'snap.png'}")
