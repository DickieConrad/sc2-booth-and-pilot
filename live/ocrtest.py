"""OCR the resource panel from out/snap.png (or a live grab with --live) and print what it reads."""
import sys
import time
from pathlib import Path

import timing
from PIL import Image

if "--live" in sys.argv:
    reader = timing.PanelReader()
    for i in range(6):
        t0 = time.perf_counter()
        img = reader.grab()
        text = timing.ocr_text(img)
        print(repr(text), timing.parse_panel(text), f"{(time.perf_counter() - t0) * 1000:.0f} ms")
        img.save(Path(__file__).with_name("out") / f"panel{i}.png")
        time.sleep(1)
else:
    img = Image.open(Path(__file__).with_name("out") / "snap.png")
    crop = img.crop(timing.panel_box(img.width, img.height))
    print(timing.parse_panel(timing.ocr_text(crop)))
