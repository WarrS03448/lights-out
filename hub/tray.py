"""System-tray icon for the hub (Sam, 2026-09-14):

  * the window's X does not quit — it hides the window and the hub keeps running in the tray;
  * a click on the tray icon opens the window again;
  * right-click → "Open" / "Close Lights Out"; "Close Lights Out" ends the exe for real.

Built on pystray (Windows backend = ctypes only, so it freezes fine) with a Pillow-drawn icon.
Everything is optional: if pystray/Pillow are missing, or we are not on Windows (and no
PYSTRAY_BACKEND is set for a test), `available()` is False and the X simply closes the hub.

Thread rules: pystray runs its own thread (`run_detached`), and its menu callbacks run
there — they never touch tkinter, they only call the two callbacks the app passed in, and
the app turns those into queue messages that its main-thread pump handles.
"""
import os
import sys


def available() -> bool:
    if sys.platform != "win32" and not os.environ.get("PYSTRAY_BACKEND"):
        return False
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception:            # noqa: BLE001 — ImportError, or a backend that cannot start
        return False
    return True


def icon_image(size: int = 64):
    """The hub's icon: near-black tile, brand-red hairline, 'LO' in light ink. Loaded from
    hub/assets (the same files the exe and the window use), drawn from scratch only if those
    files are missing."""
    from PIL import Image, ImageDraw, ImageFont
    try:
        from . import paths
        src = paths.assets_dir() / ("hub-tray.png" if size <= 64 else "hub.png")
        if src.is_file():
            img = Image.open(str(src)).convert("RGBA")
            return img if img.size == (size, size) else img.resize((size, size), Image.LANCZOS)
    except Exception:            # noqa: BLE001 — fall through to drawing
        pass
    # Colours straight from the artwork (docs/icon.md): --ground #0b0b0d, --ink #f4f2f0, and the
    # --accent hairline #c8102e at 70% already flattened onto the ground, because ImageDraw
    # replaces pixels rather than blending them.
    GROUND, INK, EDGE = (11, 11, 13, 255), (244, 242, 240, 255), (143, 14, 36, 255)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    edge = max(1, round(size * 7 / 256))
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=max(1, round(size * 6 / 256)),
                        fill=GROUND, outline=EDGE, width=edge)
    text = "LO"
    # The real mark is Oswald Semibold, but we only ship it as .woff2 and Pillow cannot read that
    # (hub/webui/static/fonts), so this path settles for whatever condensed-ish bold is on the box.
    font = None
    for name in ("oswald.ttf", "BebasNeue-Regular.ttf", "segoeuib.ttf", "arialbd.ttf",
                 "DejaVuSans-Bold.ttf"):
        try:
            font = ImageFont.truetype(name, int(size * 0.52))
            break
        except Exception:        # noqa: BLE001
            font = None
    if font is None:
        try:
            font = ImageFont.load_default(size=int(size * 0.52))
        except Exception:        # noqa: BLE001
            font = ImageFont.load_default()
    # Centre on the INK box, not the em box: the em box carries descender space that L and O
    # never use, which sits the mark visibly low (same reasoning as the real artwork).
    box = d.textbbox((0, 0), text, font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    d.text(((size - w) / 2 - box[0], (size - h) / 2 - box[1]), text, fill=INK, font=font)
    return img


class Tray:
    """One tray icon. `on_open` / `on_quit` are called from pystray's thread."""

    def __init__(self, title: str, open_label: str, quit_label: str, on_open, on_quit):
        import pystray
        self._pystray = pystray
        self._on_open = on_open
        self._on_quit = on_quit
        menu = pystray.Menu(
            pystray.MenuItem(open_label, lambda icon, item: self._on_open(), default=True),
            pystray.MenuItem(quit_label, lambda icon, item: self._on_quit()),
        )
        self.icon = pystray.Icon("LightsOut", icon_image(64), title, menu)
        self._started = False

    def start(self):
        if not self._started:
            self.icon.run_detached()
            self._started = True

    def notify(self, text: str, title: str = None):
        try:
            self.icon.notify(text, title)
        except Exception:        # noqa: BLE001 — notifications are a nicety
            pass

    def stop(self):
        try:
            self.icon.visible = False
            self.icon.stop()
        except Exception:        # noqa: BLE001
            pass
