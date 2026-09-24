"""The hub window.

Deliberately plain: a white window, black text, the system font, a checkbox per
gamemode, Install / Uninstall, and one status line.

    Lights Out

    Gamemodes
    Available                    | Installed
    Gun Game                     | Capture the Flag 10v10   update available
    Bodybomb 5v5                 |

    [ Install ]  [ Uninstall ]
    Game: C:\\...\\steamapps\\common\\Bodycam        [ Language… ] [ Browse… ]

Rules (from Sam, 2026-09-14 — third design):
  * Two columns: "Available" lists the gamemodes that are not installed, "Installed" the
    ones that are. An installed gamemode with a newer catalogue version shows an UPDATE
    button on its row; pressing it re-downloads that pack and rebuilds the pak.
  * A click on a gamemode row SELECTS it (the row turns blue); another click deselects.
    Several rows can be selected, in either column.
  * Every row carries a small NOTEPAD icon at its right end (Sam, 2026-09-14): hovering it
    says "Ruleset", clicking it opens a window with that gamemode's rules in the hub's
    language (catalogue entry "rulesets", written by the pack). The icon never selects.
  * Install   is enabled when a selected gamemode is not installed (or has an update).
              It applies desired = installed | selected.
  * Uninstall is enabled when a selected gamemode is installed.
              It applies desired = installed - selected.
  * The hub compares its version with the catalogue's hub.version on open AND every
    CATALOGUE_REFRESH_MS after it, so a release that goes out while the window is sitting
    in the tray is offered there and then rather than on the next launch. A newer hub
    puts a slim UPDATE STRIP across the top of the window (hub/update.py): it is packed
    ABOVE everything else rather than over it, so it covers nothing, and the rest of the
    hub carries on working while it sits there. Only a catalogue that marks the update
    `"required": true` still takes the whole window over with the old update screen.
    Updating mid-match is safe: the hub says what will happen, remembers where it was,
    lets the installer restart it, and the new one rejoins the match on its own.
  * The window's X hides the hub to the system tray; the tray icon opens it again;
    right-click → "Close Lights Out" really quits (hub/tray.py; falls back to a normal close
    where no tray is available).

Language: every visible string comes from i18n.t(). On the very first run (state.json has
no "language") a small modal dialog lists the seven languages Bodycam ships, preselecting
the Windows display language; the choice is saved and can be changed with "Language…".

Threading: the long jobs (catalogue fetch, ops.apply) run on daemon threads and talk to
the UI only through a queue.Queue that the main thread drains with root.after — no
tkinter call ever happens off the main thread.

Importing this module must not open a window; everything happens in main().
"""
import os
import queue
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, font as tkfont, messagebox

from . import catalogue as cat
from . import game as game_mod
from . import i18n
from . import ops
from . import state as state_mod
from . import singleton as singleton_mod
from . import tray as tray_mod
from . import update as update_mod
from .i18n import t
from . import paths
from .version import APP_NAME, CATALOGUE_URL, HUB_VERSION
from . import competitive as competitive_mod
from .theme import WHITE, BLACK, GREY, DARK_RED, SELECT_BG, LINE, ACCENT, ACCENT_DARK

# --local: catalogue + packs from the checkout this exe/package lives in (set in main())
LOCAL_REPO = None

# How often the catalogue is re-fetched while the window is open, so a release that goes out
# mid-session is offered without a restart. Kept in step with webui/shell.py's
# CATALOGUE_REFRESH_SECONDS - the two UIs must not disagree about when an update shows up.
#
# A MINUTE, not the fifteen this used to be. Fifteen was fine when the only thing that moved was a
# hub release, and became painful the moment a rules override could change what the game plays:
# on 2026-09-16 Sam changed a Railway variable, pressed Update, and got a pak built from the value
# he had just replaced - the hub had simply not looked again yet, and nothing on screen suggested
# it had anything to look for. An unchanged fetch is compared and dropped without touching
# state.json or pushing a snapshot (_use_catalogue), so the cost of asking more often is one
# conditional GET a minute.
CATALOGUE_REFRESH_MS = 60 * 1000

# The palette moved to hub/theme.py when the Competitive tab arrived (it needs the same
# colours). WHITE, BLACK, GREY, DARK_RED and SELECT_BG are imported above and stay
# importable from hub.app, because tests and older code get them from here.


def apply_window_icon(root):
    """The window's (and so the taskbar's) icon = hub/assets/hub.ico|png — the same 'LO' mark as the tray and the exe."""
    try:
        assets = paths.assets_dir()
        if os.name == "nt" and (assets / "hub.ico").is_file():
            root.iconbitmap(default=str(assets / "hub.ico"))
        png = assets / "hub.png"
        if png.is_file():
            root._hub_icon = tk.PhotoImage(file=str(png))   # keep a reference or Tk drops it
            root.iconphoto(True, root._hub_icon)
    except Exception:            # noqa: BLE001 — an icon problem must never stop the window
        pass


def apply_font_for_language(code: str):
    """Point Tk's default fonts at a family that has the glyphs (only matters for zh)."""
    fam = i18n.font_family(code)
    if not fam:
        return
    try:
        if fam not in tkfont.families():
            return
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            tkfont.nametofont(name).configure(family=fam)
    except Exception:            # noqa: BLE001 — a font problem must never stop the window
        pass


class LanguageDialog:
    """Modal "choose your language" prompt: one radio button per language (native names),
    a short explanation in the *selected* language, and OK. `LanguageDialog.ask(parent,
    initial)` blocks until OK/close and returns the chosen code (`initial` if just closed)."""

    def __init__(self, parent, initial: str):
        self.result = initial if initial in i18n.CODES else i18n.DEFAULT
        top = self.top = tk.Toplevel(parent)
        top.title("Language")
        top.configure(bg=WHITE)
        top.resizable(False, False)
        try:
            top.transient(parent)
        except Exception:        # noqa: BLE001
            pass

        base = tkfont.nametofont("TkDefaultFont")
        f_head = base.copy()
        f_head.configure(weight="bold")

        outer = tk.Frame(top, bg=WHITE, padx=16, pady=14)
        outer.pack(fill="both", expand=True)
        # "Language / Sprache / Idioma / Langue / Язык / 语言" (each word once, in list order)
        words = list(dict.fromkeys(i18n.tr(c, "lang_title") for c in i18n.CODES))
        tk.Label(outer, text=" / ".join(words),
                 bg=WHITE, fg=BLACK, font=f_head, anchor="w", justify="left").pack(fill="x")

        self.var = tk.StringVar(value=self.result)
        self.buttons = {}
        box = tk.Frame(outer, bg=WHITE)
        box.pack(fill="x", pady=(10, 6))
        for code, name in i18n.LANGUAGES:
            rb = tk.Radiobutton(box, text=name, value=code, variable=self.var,
                                bg=WHITE, fg=BLACK, activebackground=WHITE, activeforeground=BLACK,
                                selectcolor=WHITE, highlightthickness=0, bd=0, anchor="w", padx=0)
            rb.pack(fill="x", pady=1)
            self.buttons[code] = rb

        self.prompt = tk.Label(outer, text="", bg=WHITE, fg=BLACK, anchor="w", justify="left")
        self.prompt.pack(fill="x", pady=(4, 10))
        self.btn_ok = tk.Button(outer, text="OK", width=12, bg=WHITE, fg=BLACK,
                                activebackground=WHITE, activeforeground=BLACK, command=self._ok)
        self.btn_ok.pack(anchor="e")

        self.var.trace_add("write", lambda *_: self._preview())
        self._preview()
        top.protocol("WM_DELETE_WINDOW", self._ok)
        top.bind("<Return>", lambda _e: self._ok())

        # centre over the parent window when it is on screen, else on the screen
        top.update_idletasks()
        w, h = top.winfo_reqwidth(), top.winfo_reqheight()
        try:
            if parent.winfo_viewable() and parent.winfo_width() > 1:
                x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
                y = parent.winfo_rooty() + (parent.winfo_height() - h) // 3
            else:
                raise ValueError
        except Exception:        # noqa: BLE001
            x = (top.winfo_screenwidth() - w) // 2
            y = (top.winfo_screenheight() - h) // 3
        top.geometry(f"+{max(0, x)}+{max(0, y)}")
        top.deiconify()
        top.lift()
        try:
            top.update()             # mapped before grab_set, or Tk refuses the grab
            top.grab_set()
        except Exception:        # noqa: BLE001
            pass
        top.focus_set()

    def _preview(self):
        code = self.var.get()
        self.prompt.configure(text=i18n.tr(code, "lang_prompt"))
        self.btn_ok.configure(text=i18n.tr(code, "ok"))

    def _ok(self):
        self.result = self.var.get() if self.var.get() in i18n.CODES else self.result
        self.top.destroy()

    @classmethod
    def ask(cls, parent, initial: str) -> str:
        d = cls(parent, initial)
        parent.wait_window(d.top)
        return d.result


class Tooltip:
    """A plain one-line tooltip (a borderless Toplevel) shown 400 ms after the pointer rests on a widget."""

    def __init__(self, widget, text: str):
        self.widget, self.text, self.tip, self.after_id = widget, text, None, None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e=None):
        self._cancel()
        self.after_id = self.widget.after(400, self._show)

    def _cancel(self):
        if self.after_id is not None:
            try:
                self.widget.after_cancel(self.after_id)
            except Exception:        # noqa: BLE001
                pass
            self.after_id = None

    def _show(self):
        self.after_id = None
        if self.tip is not None or not self.widget.winfo_exists():
            return
        x = self.widget.winfo_rootx() + self.widget.winfo_width() // 2
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        try:
            self.tip.wm_attributes("-topmost", True)
        except Exception:            # noqa: BLE001
            pass
        tk.Label(self.tip, text=self.text, bg="#FFFFE1", fg=BLACK, relief="solid", borderwidth=1,
                 padx=6, pady=2).pack()
        self.tip.wm_geometry(f"+{x}+{y}")

    def _hide(self, _e=None):
        self._cancel()
        if self.tip is not None:
            try:
                self.tip.destroy()
            except Exception:        # noqa: BLE001
                pass
            self.tip = None


def notepad_icon(parent, bg: str, size: int = 18) -> tk.Canvas:
    """A small notepad drawn with canvas lines: a page with a spiral binding at the top and three ruled lines."""
    c = tk.Canvas(parent, width=size, height=size, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
    s = size / 18.0
    def P(x, y):
        return x * s, y * s
    c.create_rectangle(*P(3, 4), *P(15, 16), outline=BLACK, fill=WHITE, width=1)          # the page
    for x in (5.5, 9, 12.5):                                                                # the binding rings
        c.create_oval(*P(x - 1, 2), *P(x + 1, 5), outline=BLACK, fill=WHITE, width=1)
    for y in (8, 10.5, 13):                                                                 # ruled lines
        c.create_line(*P(5, y), *P(13, y), fill=BLACK, width=1)
    return c


class HubApp:
    """All of the UI. Construct with a Tk root, then root.mainloop().
    `language` skips the first-run prompt (tests, --lang); otherwise state.json decides."""

    # the first-run prompt, as a hook so tests (or a future CLI) can answer it without a window
    ask_language = staticmethod(LanguageDialog.ask)
    # tray support, as a hook so tests can force it on/off
    tray_available = staticmethod(tray_mod.available)

    def __init__(self, root: tk.Tk, language: str = None):
        self.root = root
        self.q = queue.Queue()          # worker -> UI messages
        self.busy = False               # an install/uninstall is running

        self.state = state_mod.load()
        self.catalogue = None           # dict once loaded (network or cache)
        self.rows = {}                  # gamemode id -> dict of row widgets
        self.selected = set()           # gamemode ids whose row is highlighted
        self._status_job = None         # pending "back to the idle line" timer
        self.outer = None               # the one frame everything lives in (rebuilt on language change)
        self.tray = None                # tray_mod.Tray once started
        self._tray_hinted = False       # the "still running in the tray" balloon, shown once
        self._updating = None           # gamemode id whose Update button started the running job
        self.update_screen = None       # the mandatory-update frame, once shown
        self.update_bar = None          # the slim update strip above everything, once shown
        self._update_dismissed = False  # "Later": no strip again until the hub is restarted
        self._update_busy = False       # a download is running
        # A catalogue that says "required": true, held back because a match is running. _pump
        # puts the screen up the moment the match is over. See _check_hub_update.
        self._update_required_held = False
        self._banner_grown = 0          # pixels the window was grown to make room for the strip
        # Come back where we left off. Closing the hub mid-match and opening it again has to
        # land on Competitive, not on the gamemode list, or the rejoin happens off screen.
        self.tab = self.state.get("tab") or "gamemodes"
        self.tab_frames = {}            # tab name -> the frame holding that tab's content
        self.tab_buttons = {}           # tab name -> (label, underline rule)
        self.comp = None                # CompetitivePanel, built with the window
        self.ruleset_window = None      # the last "Ruleset" pop-up (tests look here)
        self._update_info = None        # the catalogue's hub object that offered/forced it
        self._update_file = None        # the downloaded installer, once complete

        # language: argument > state.json > first-run dialog
        lang = language or self.state.get("language")
        if lang not in i18n.CODES:
            # The main window must stay VISIBLE here: on Windows a transient dialog of a
            # withdrawn window is withdrawn with it, and the hub would sit invisible
            # forever waiting for a click nobody can make. So show the empty white
            # window first and put the dialog on top of it.
            root.title(APP_NAME)
            root.configure(bg=WHITE)
            root.geometry("620x400")
            apply_window_icon(root)
            root.deiconify()
            root.update()
            lang = self.ask_language(root, i18n.detect_system_language())
        i18n.set_language(lang)
        apply_font_for_language(lang)
        apply_window_icon(root)
        self.state["language"] = lang

        # game folder: remembered one first (if it still looks right), else auto-detect
        remembered = self.state.get("game_dir")
        self.game_dir = remembered if game_mod.is_game_dir(remembered) else game_mod.find_game_dir()

        note = state_mod.reconcile(self.state, self.game_dir)
        if self.game_dir and self.state.get("game_dir") != self.game_dir:
            self.state["game_dir"] = self.game_dir
        state_mod.save(self.state)

        self._build_ui()
        if note:
            self._status(note)
        self._refresh_rows()
        self._load_catalogue_async(repeat=True)
        self._start_tray()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._pump)

    # ------------------------------------------------------------ layout
    def _build_ui(self):
        r = self.root
        r.title(APP_NAME + (" (local test)" if LOCAL_REPO else ""))
        r.configure(bg=WHITE)
        if self.outer is None:
            r.geometry("620x400")
            r.minsize(480, 320)

        base = tkfont.nametofont("TkDefaultFont")
        self.f_title = base.copy()
        self.f_title.configure(size=max(base.cget("size"), 9) + 3, weight="bold")
        self.f_head = base.copy()
        self.f_head.configure(weight="bold")
        self.f_body = base.copy()

        outer = self.outer = tk.Frame(r, bg=WHITE, padx=14, pady=12)
        outer.pack(fill="both", expand=True)

        self.title_label = tk.Label(outer, text=APP_NAME, bg=WHITE, fg=BLACK,
                                    font=self.f_title, anchor="w")
        self.title_label.pack(fill="x")

        # Gamemodes | Competitive (Sam, 2026-09-14). The tab label replaces the old
        # "Gamemodes" heading that used to sit here.
        self._build_tabstrip(outer)

        # One frame per tab; both are built now and _show_tab packs exactly one of them,
        # so self.rows / btn_install stay valid whichever tab is up.
        content = tk.Frame(outer, bg=WHITE)
        content.pack(fill="both", expand=True)
        self.tab_content = content
        gamemodes = tk.Frame(content, bg=WHITE)
        self.tab_frames["gamemodes"] = gamemodes

        # one row per gamemode, rebuilt by _refresh_rows()
        self.rows_frame = tk.Frame(gamemodes, bg=WHITE)
        self.rows_frame.pack(fill="both", expand=True)
        self.rows_frame.columnconfigure(0, weight=1)
        self.empty_label = tk.Label(self.rows_frame, text=t("loading"),
                                    bg=WHITE, fg=GREY, anchor="w")
        self.empty_label.grid(row=0, column=0, sticky="w")

        # buttons
        buttons = tk.Frame(gamemodes, bg=WHITE)
        buttons.pack(fill="x", pady=(10, 6))
        self.btn_install = tk.Button(buttons, text=t("install"), width=12, bg=WHITE, fg=BLACK,
                                     activebackground=WHITE, activeforeground=BLACK,
                                     command=self._on_install, state="disabled")
        self.btn_install.pack(side="left")
        self.btn_uninstall = tk.Button(buttons, text=t("uninstall"), width=12, bg=WHITE, fg=BLACK,
                                       activebackground=WHITE, activeforeground=BLACK,
                                       command=self._on_uninstall, state="disabled")
        self.btn_uninstall.pack(side="left", padx=(8, 0))

        competitive = tk.Frame(content, bg=WHITE)
        self.tab_frames["competitive"] = competitive
        self.comp = competitive_mod.CompetitivePanel(self, competitive)
        self.comp.frame.pack(fill="both", expand=True)

        # status line + Language… + Browse…
        bottom = tk.Frame(outer, bg=WHITE)
        bottom.pack(fill="x")
        self.btn_browse = tk.Button(bottom, text=t("browse"), bg=WHITE, fg=BLACK,
                                    activebackground=WHITE, activeforeground=BLACK,
                                    command=self._on_browse)
        self.btn_browse.pack(side="right", padx=(8, 0))
        self.btn_language = tk.Button(bottom, text=t("language"), bg=WHITE, fg=BLACK,
                                      activebackground=WHITE, activeforeground=BLACK,
                                      command=self._on_language)
        self.btn_language.pack(side="right", padx=(8, 0))
        self.status = tk.Label(bottom, text="", bg=WHITE, fg=BLACK, anchor="w", justify="left")
        self.status.pack(side="left", fill="x", expand=True)
        self._status(self._game_line())
        self._show_tab(self.tab)
        # A language change throws every widget away; the strip is one of them, and it has to
        # come back in the new language. It is a child of the ROOT, not of `outer`, so it is
        # rebuilt here rather than inside it.
        if self._update_info is not None and self.update_screen is None and not self._update_dismissed:
            self._show_update_banner(self._update_info)

    def _build_tabstrip(self, parent):
        """Gamemodes | Competitive. The active tab is black with a blue underline."""
        strip = tk.Frame(parent, bg=WHITE)
        strip.pack(fill="x", pady=(12, 0))
        self.tab_buttons = {}
        for name, key in (("gamemodes", "tab_gamemodes"), ("competitive", "tab_competitive")):
            holder = tk.Frame(strip, bg=WHITE)
            holder.pack(side="left", padx=(0, 18))
            label = tk.Label(holder, text=t(key), bg=WHITE, fg=GREY, cursor="hand2",
                             font=self.f_head, pady=2)
            label.pack()
            rule = tk.Frame(holder, bg=WHITE, height=2)
            rule.pack(fill="x")
            label.bind("<Button-1>", lambda _e, n=name: self._show_tab(n))
            self.tab_buttons[name] = (label, rule)
        tk.Frame(parent, bg=LINE, height=1).pack(fill="x", pady=(0, 10))

    def _show_tab(self, name: str):
        """Swap the visible tab, paint the strip, and give Competitive room to breathe."""
        if name not in self.tab_frames:
            name = "gamemodes"
        self.tab = name
        for other, frame in self.tab_frames.items():
            if other == name:
                frame.pack(fill="both", expand=True)
            else:
                frame.pack_forget()
        for other, (label, rule) in self.tab_buttons.items():
            active = other == name
            label.configure(fg=BLACK if active else GREY)
            rule.configure(bg=ACCENT if active else WHITE)
        # Remember it for the next start: a hub closed (or restarted by the updater) during a
        # match must come back to Competitive so the rejoin happens where the player can see it.
        if self.state.get("tab") != name:
            self.state["tab"] = name
            try:
                state_mod.update_fields({"tab": name})
            except Exception:    # noqa: BLE001 — a read-only state dir must not break tabbing
                pass
        # the gamemode list is happy at 620x400; the competitive screens are not. Every size
        # here is the size of the CONTENT, so the update strip's height is added on top of it
        # rather than taken out of it.
        extra = self._banner_grown
        if name == "competitive":
            self.root.minsize(760, 560 + extra)
            if self.root.winfo_width() < 880 or self.root.winfo_height() < 620 + extra:
                self.root.geometry("880x%d" % (620 + extra))
            self._refresh_competitive()
        else:
            self.root.minsize(480, 320 + extra)

    def _refresh_competitive(self):
        """Redraw the Competitive tab. It must never be able to break the main window."""
        if self.comp is None or self.update_screen is not None:
            return
        try:
            self.comp.refresh()
        except Exception:        # noqa: BLE001
            pass

    def _rebuild_ui(self):
        """Throw the widgets away and build them again in the current language."""
        if self._status_job is not None:
            try:
                self.root.after_cancel(self._status_job)
            except Exception:    # noqa: BLE001
                pass
            self._status_job = None
        keep = set(self.selected)
        keep_tab = self.tab
        # Which of the two update surfaces is up has to be read BEFORE the widgets go: in the
        # mandatory case update_screen IS outer, so after the destroy there is nothing to ask.
        was_mandatory = self.update_screen is not None
        if self.comp is not None:
            self.comp.destroy()
            self.comp = None
        # The strip is a child of the ROOT, so outer.destroy() does not take it with it and it
        # would be left behind in the old language. _build_ui puts an identical one back, which
        # is why the window keeps the height it already made for it.
        self._hide_update_banner(restore_size=False)
        self.outer.destroy()
        self.rows.clear()
        self.selected.clear()
        if was_mandatory:                            # the update screen, in the new language
            self.outer = self.update_screen = None
            self._show_update_screen(self._update_info or {})
            return
        self._build_ui()
        self._show_tab(keep_tab)
        self._refresh_rows()
        for mode_id in keep:
            if mode_id in self.rows:
                self._set_selected(mode_id, True)
        self._update_buttons()
        if self.busy:
            self._set_busy(True)
            self._status(t("working"))

    # ------------------------------------------------------------ status line
    def _game_line(self) -> str:
        if self.game_dir:
            return t("game_line", path=self.game_dir)
        return t("game_not_found")

    def _status(self, text, error: bool = False, revert_after=None):
        """Set the one-line status. revert_after = ms before the game line comes back."""
        if self.update_screen is not None:
            return                                    # no status line on the update screen
        if self._status_job is not None:
            try:
                self.root.after_cancel(self._status_job)
            except Exception:
                pass
            self._status_job = None
        self.status.configure(text=text, fg=DARK_RED if error else BLACK)
        if revert_after:
            self._status_job = self.root.after(revert_after, lambda: self._status(self._game_line()))

    # ------------------------------------------------------------ rows
    def _installed(self) -> dict:
        return self.state.get("installed") or {}

    def _entries(self) -> list:
        """Catalogue gamemodes, plus any installed id the catalogue no longer lists (so the
        player can still select and uninstall it)."""
        entries = list((self.catalogue or {}).get("gamemodes", []))
        known = {e.get("id") for e in entries}
        for mode_id, info in self._installed().items():
            if mode_id not in known:
                entries.append({"id": mode_id, "title": info.get("title", mode_id),
                                "titles": info.get("titles") or {},
                                "version": info.get("version", ""), "_orphan": True})
        return entries

    def _refresh_rows(self, keep_selection: bool = False):
        """(Re)build the two columns. Nothing is selected afterwards unless keep_selection."""
        if self.update_screen is not None:
            return                                    # the update screen owns the window
        previous = set(self.selected) if keep_selection else set()
        for child in self.rows_frame.winfo_children():
            child.destroy()
        self.rows.clear()
        self.selected.clear()

        entries = self._entries()
        installed = self._installed()
        if not entries:
            msg = t("loading") if self.catalogue is None else t("none_available")
            self.empty_label = tk.Label(self.rows_frame, text=msg, bg=WHITE, fg=GREY, anchor="w")
            self.empty_label.grid(row=0, column=0, sticky="w")
            self._update_buttons()
            return

        # two equal columns with a thin grey rule between them
        self.rows_frame.columnconfigure(0, weight=1, uniform="cols")
        self.rows_frame.columnconfigure(1, weight=0)
        self.rows_frame.columnconfigure(2, weight=1, uniform="cols")
        col_avail = tk.Frame(self.rows_frame, bg=WHITE)
        col_avail.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        tk.Frame(self.rows_frame, bg="#DDDDDD", width=1).grid(row=0, column=1, sticky="ns")
        col_inst = tk.Frame(self.rows_frame, bg=WHITE)
        col_inst.grid(row=0, column=2, sticky="nsew", padx=(8, 0))
        for col, header in ((col_avail, t("available_header")), (col_inst, t("installed_header"))):
            col.columnconfigure(0, weight=1)
            tk.Label(col, text=header, bg=WHITE, fg=BLACK, font=self.f_head, anchor="w",
                     padx=6).grid(row=0, column=0, sticky="ew", pady=(0, 3))
        self.columns = {"available": col_avail, "installed": col_inst}
        counts = {"available": 0, "installed": 0}

        for entry in entries:
            mode_id = entry["id"]
            is_installed = mode_id in installed
            which = "installed" if is_installed else "available"
            col = self.columns[which]
            counts[which] += 1

            row = tk.Frame(col, bg=WHITE, cursor="hand2")
            row.grid(row=counts[which], column=0, sticky="ew", pady=1)
            row.columnconfigure(0, weight=1)

            name = tk.Label(row, text=cat.display_title(entry), bg=WHITE, fg=BLACK,
                            anchor="w", padx=6, pady=3)
            name.grid(row=0, column=0, sticky="ew")

            # the notepad: hover -> "Ruleset", click -> the gamemode's rules (Sam, 2026-09-14); only when the catalogue
            # entry carries any (an installed gamemode the catalogue no longer lists has none)
            icon = None
            if self._ruleset_text(entry):
                icon = notepad_icon(row, WHITE)
                icon.grid(row=0, column=1, sticky="e", padx=(6, 6))
                Tooltip(icon, t("ruleset"))
                icon.bind("<Button-1>", lambda _e, e=entry: (self._show_ruleset(e), "break")[1])   # "break": never a row click

            # a newer version in the catalogue than the installed one: an Update BUTTON on the row (Sam, 2026-09-14);
            # pressing it re-downloads that gamemode's pack and rebuilds the pak with everything that is installed
            upd = None
            if is_installed:
                if cat.mode_update_available(entry, installed[mode_id]):
                    upd = tk.Button(row, text=t("update_button"), bg=WHITE, fg=BLACK, activebackground=WHITE,
                                    activeforeground=BLACK, padx=8, cursor="hand2",
                                    command=lambda m=mode_id: self._on_update(m))
                    upd.grid(row=0, column=2, sticky="e", padx=(2, 6))

            for w in (row, name):
                w.bind("<Button-1>", lambda _e, m=mode_id: self._toggle_selected(m))

            self.rows[mode_id] = {"frame": row, "name": name, "update": upd, "ruleset": icon,
                                  "installed": is_installed, "column": which}
            if mode_id in previous:
                self._set_selected(mode_id, True)

        self._update_buttons()

    def _set_selected(self, mode_id: str, on: bool):
        """Paint one row blue (selected) or white, and keep self.selected in step."""
        r = self.rows.get(mode_id)
        if not r:
            return
        bg = SELECT_BG if on else WHITE
        for key in ("frame", "name"):
            r[key].configure(bg=bg)
        if r.get("update") is not None:
            r["update"].configure(bg=bg, activebackground=bg)
        if r.get("ruleset") is not None:
            r["ruleset"].configure(bg=bg)
        if on:
            self.selected.add(mode_id)
        else:
            self.selected.discard(mode_id)

    def _toggle_selected(self, mode_id: str):
        if self.busy or mode_id not in self.rows:
            return
        self._set_selected(mode_id, mode_id not in self.selected)
        self._update_buttons()

    @staticmethod
    def _ruleset_text(entry: dict) -> str:
        """The gamemode's rules in the hub's language, else English, else the first one there is ("" when none)."""
        rs = entry.get("rulesets") or {}
        if not isinstance(rs, dict):
            return ""
        for code in (i18n.get_language(), "en"):
            if rs.get(code):
                return str(rs[code])
        return str(next((v for v in rs.values() if v), ""))

    def _show_ruleset(self, entry: dict):
        """A small window with the gamemode's rules (read-only text, wrapped, scrollable) and a Close button."""
        text = self._ruleset_text(entry)
        if not text:
            return
        win = tk.Toplevel(self.root)
        win.title(t("ruleset_title", name=cat.display_title(entry)))
        win.configure(bg=WHITE)
        win.transient(self.root)
        apply_window_icon(win)
        body = tk.Frame(win, bg=WHITE)
        body.pack(fill="both", expand=True, padx=12, pady=(12, 6))
        box = tk.Text(body, wrap="word", bg=WHITE, fg=BLACK, relief="flat", padx=4, pady=4,
                      width=64, height=16, font=self.f_body)
        sb = tk.Scrollbar(body, command=box.yview)
        box.configure(yscrollcommand=sb.set)
        box.insert("1.0", text)
        box.configure(state="disabled")
        box.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        tk.Button(win, text=t("close"), bg=WHITE, fg=BLACK, activebackground=WHITE, activeforeground=BLACK,
                  padx=14, command=win.destroy).pack(pady=(0, 12))
        win.bind("<Escape>", lambda _e: win.destroy())
        win.update_idletasks()
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - win.winfo_width()) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - win.winfo_height()) // 3)
        win.geometry(f"+{x}+{y}")
        self.ruleset_window = win
        return win

    def _plan(self) -> ops.Plan:
        return ops.plan(self.selected, set(self._installed()))

    def _updatable(self) -> set:
        """Installed gamemodes for which the catalogue lists a newer version."""
        installed = self._installed()
        out = set()
        for e in (self.catalogue or {}).get("gamemodes", []):
            info = installed.get(e.get("id"))
            if info is not None and cat.mode_update_available(e, info):
                out.add(e["id"])
        return out

    def _update_buttons(self):
        if self.busy or self.update_screen is not None:
            return
        p = self._plan()
        # Install: a selected gamemode that is not installed — or one whose installed
        # version is older than the catalogue's (that version is not installed either),
        # which is what makes the grey "update available" line actionable.
        can_install = p.can_install or bool(self._updatable() & self.selected)
        ready = bool(self.game_dir) and self.catalogue is not None
        self.btn_install.configure(state="normal" if (ready and can_install) else "disabled")
        self.btn_uninstall.configure(
            state="normal" if (bool(self.game_dir) and p.can_uninstall) else "disabled")

    def _set_busy(self, busy: bool):
        self.busy = busy
        if self.update_screen is not None:
            return
        widget_state = "disabled" if busy else "normal"
        for row in self.rows.values():
            row["frame"].configure(cursor="arrow" if busy else "hand2")
            if row.get("update") is not None:
                row["update"].configure(state=widget_state)
        self.btn_browse.configure(state=widget_state)
        self.btn_language.configure(state=widget_state)
        if busy:
            self.btn_install.configure(state="disabled")
            self.btn_uninstall.configure(state="disabled")
        else:
            self._update_buttons()
        self._refresh_competitive()

    # ------------------------------------------------------------ catalogue
    def _load_catalogue_async(self, repeat: bool = False):
        """Fetch the catalogue on a worker thread; the answer comes back through _pump.

        `repeat` re-arms the fetch every CATALOGUE_REFRESH_MS so a release that goes out while the
        hub is open is noticed without a restart - the update strip is derived from the catalogue
        (_check_hub_update), so a catalogue fetched once on start means the strip can only ever
        appear on the NEXT launch. The web UI does the same (webui/shell.py load_catalogue_async).
        A refresh that lands unchanged redraws nothing (see _use_catalogue)."""
        if repeat:
            self.root.after(CATALOGUE_REFRESH_MS, lambda: self._load_catalogue_async(repeat=True))
        def work():
            try:
                if LOCAL_REPO:
                    from urllib.request import pathname2url
                    local_cat = os.path.join(str(LOCAL_REPO), "server", "public", "catalogue.json")
                    data = cat.fetch_catalogue("file:" + pathname2url(os.path.abspath(local_cat)))
                    cat.localize_to_repo(data, LOCAL_REPO)
                else:
                    data = cat.fetch_catalogue(CATALOGUE_URL)
                self.q.put(("catalogue", data))
            except Exception as e:      # offline, 404, bad JSON…
                self.q.put(("catalogue_error", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _use_catalogue(self, data, from_cache: bool = False) -> bool:
        """Adopt a catalogue. Returns False (and does nothing at all) when it is the one already
        held, which is what every periodic refresh normally brings back: an unchanged refresh must
        not redraw the list, rewrite state.json or replace whatever the status line is saying."""
        if data == self.catalogue:
            return False
        self.catalogue = data
        if not from_cache:
            self.state["catalogue_cache"] = data
            state_mod.update_fields({"catalogue_cache": data})
        if self._check_hub_update():
            return True                               # the update screen is up; no gamemode list
        self._refresh_rows(keep_selection=True)
        self._refresh_competitive()
        if from_cache:
            self._status(t("offline_cached"))
        return True

    def _check_hub_update(self) -> bool:
        """Offer — or, when the catalogue insists, force — a newer hub.

        Returns True ONLY when the mandatory screen has taken the whole window over, because
        that is what tells the caller there is no gamemode list left to draw.

        The ordinary case is the strip along the top and a False: an update is an offer, and
        nothing about the hub stops working while it sits there (Sam, 2026-09-14). A catalogue
        that sets `"required": true` on its hub object still gets the old all-or-nothing
        screen, which is the escape hatch for a release that genuinely cannot interoperate.

        ...EXCEPT DURING A MATCH (Sam, 2026-09-15). The mandatory screen replaces the whole
        window and destroys the Competitive tab with it, and a player who is in a match needs
        that tab: it is where accept, the veto, the connect window and "I am in the game" are.
        Taking it away does not make them update any sooner - it makes them a no-show, and costs
        them Elo and a queue ban for a release THEY did not push. So a required update waits: it
        shows as the ordinary strip, `_update_required_held` remembers it, and _pump puts the
        screen up the moment the match ends. What stops them queueing again on the old build in
        the meantime is the version gate (hub/competitive.py update_needed), not this screen."""
        info = (self.catalogue or {}).get("hub") or {}
        if not (cat.version_newer(info.get("version", ""), HUB_VERSION) and info.get("download_url")):
            return False
        if info.get("required") and not self._match_in_progress():
            self._update_required_held = False
            if self.update_screen is None:
                self._show_update_screen(info)
            return True
        self._update_required_held = bool(info.get("required"))
        self._update_info = info
        # A held-back required update ignores "Later": the player dismissed an OFFER, and this
        # is not one - it is a screen waiting for the match to end.
        if self.update_bar is None and (self._update_required_held or not self._update_dismissed):
            self._show_update_banner(info)
        return False

    # ------------------------------------------------------------ the update strip
    def _show_update_banner(self, info: dict):
        """A slim strip across the very top of the window: what is available, Update, Later.

        It is PACKED above `outer`, not placed over it, so it covers nothing — and the window
        grows by exactly its height the first time it appears, so everything that was on
        screen stays on screen. The hub below it carries on working, a match included."""
        self._update_info = info
        if self.update_bar is not None or self.outer is None:
            return
        bar = self.update_bar = tk.Frame(self.root, bg=ACCENT)
        bar.pack(side="top", fill="x", before=self.outer)
        inner = tk.Frame(bar, bg=ACCENT, padx=12, pady=6)
        inner.pack(fill="x")
        # right-hand end first, so the message keeps whatever width is left over
        self.btn_update_later = tk.Label(inner, text=t("update_banner_later"), bg=ACCENT, fg=WHITE,
                                         cursor="hand2", padx=4)
        self.btn_update_later.pack(side="right")
        self.btn_update_later.bind("<Button-1>", lambda _e: self._dismiss_update_banner())
        self.btn_update_bar = tk.Button(inner, bg=WHITE, fg=ACCENT, activebackground=WHITE,
                                        activeforeground=ACCENT_DARK, relief="flat", padx=10,
                                        command=self._on_banner_update)
        self.btn_update_bar.pack(side="right", padx=(10, 14))
        self.upd_bar_label = tk.Label(inner, bg=ACCENT, fg=WHITE, anchor="w", justify="left")
        self.upd_bar_label.pack(side="left", fill="x", expand=True)
        self._paint_update_banner()
        self._grow_for_banner()

    def _paint_update_banner(self):
        """The strip says one thing at a time: available -> downloading -> ready to install."""
        if self.update_bar is None:
            return
        new = (self._update_info or {}).get("version", "?")
        if self._update_file:
            self.upd_bar_label.configure(text=t("update_banner_ready", new=new))
            self.btn_update_bar.configure(text=t("update_banner_install"), state="normal")
        elif self._update_busy:
            self.btn_update_bar.configure(text=t("update_banner_action"), state="disabled")
        else:
            self.upd_bar_label.configure(text=t("update_banner", new=new, old=HUB_VERSION))
            self.btn_update_bar.configure(text=t("update_banner_action"), state="normal")

    def _grow_for_banner(self):
        """Give the strip its own height rather than taking it off the content. Once, ever:
        `_banner_grown` is both the flag and the number every later minsize adds back on."""
        if self._banner_grown or self.update_bar is None:
            return
        try:
            self.root.update_idletasks()
            extra = int(self.update_bar.winfo_reqheight())
            if extra <= 1:
                return
            self._banner_grown = extra
            min_w, min_h = self.root.minsize()
            self.root.minsize(min_w, min_h + extra)
            w, h = self.root.winfo_width(), self.root.winfo_height()
            if w > 1 and h > 1:
                self.root.geometry("%dx%d" % (w, h + extra))
        except Exception:        # noqa: BLE001 — a strip that cannot measure itself still works
            pass

    def _dismiss_update_banner(self):
        """"Later": the strip goes away for this run, and the next start offers it again."""
        self._update_dismissed = True
        self._hide_update_banner()

    def _hide_update_banner(self, restore_size: bool = True):
        """Take the strip down. `restore_size=False` is the language rebuild, which puts an
        identical strip straight back and so must not hand the height back and take it again."""
        bar, self.update_bar = self.update_bar, None
        if bar is not None:
            try:
                bar.destroy()
            except Exception:    # noqa: BLE001
                pass
        if not restore_size or not self._banner_grown:
            return
        extra, self._banner_grown = self._banner_grown, 0
        try:
            min_w, min_h = self.root.minsize()
            self.root.minsize(min_w, max(1, min_h - extra))
            w, h = self.root.winfo_width(), self.root.winfo_height()
            if w > 1 and h > 1:
                self.root.geometry("%dx%d" % (w, max(1, h - extra)))
        except Exception:        # noqa: BLE001
            pass

    def _on_banner_update(self):
        """One button, two phases — the same pair as the mandatory screen's."""
        if self._update_file:
            self._launch_update()
            return
        self._start_update_download()

    # ------------------------------------------------------------ mandatory update
    def _show_update_screen(self, info: dict):
        """Replace the whole window content: version line, Download update / Start, Close Lights Out."""
        self._update_info = info
        self._hide_update_banner()       # the two never share the window
        if self.comp is not None:
            self.comp.destroy()
            self.comp = None
        if self.outer is not None:
            try:
                self.outer.destroy()
            except Exception:    # noqa: BLE001
                pass
        self.rows.clear()
        self.selected.clear()
        r = self.root
        r.title(APP_NAME)
        base = tkfont.nametofont("TkDefaultFont")
        f_title = base.copy()
        f_title.configure(size=max(base.cget("size"), 9) + 3, weight="bold")
        f_head = base.copy()
        f_head.configure(weight="bold")

        outer = self.outer = self.update_screen = tk.Frame(r, bg=WHITE, padx=14, pady=12)
        outer.pack(fill="both", expand=True)
        tk.Label(outer, text=APP_NAME, bg=WHITE, fg=BLACK, font=f_title, anchor="w").pack(fill="x")
        tk.Label(outer, text=t("update_title"), bg=WHITE, fg=BLACK, font=f_head, anchor="w").pack(fill="x", pady=(12, 4))
        tk.Label(outer, text=t("update_body", new=info.get("version", "?"), old=HUB_VERSION),
                 bg=WHITE, fg=BLACK, anchor="w", justify="left").pack(fill="x")

        self.upd_status = tk.Label(outer, text="", bg=WHITE, fg=BLACK, anchor="w", justify="left")
        self.upd_status.pack(fill="x", pady=(12, 0))

        buttons = tk.Frame(outer, bg=WHITE)
        buttons.pack(fill="x", pady=(14, 6))
        self.btn_update = tk.Button(buttons, text=t("update_download"), bg=WHITE, fg=BLACK,
                                    activebackground=WHITE, activeforeground=BLACK, command=self._on_update_button)
        self.btn_update.pack(side="left")
        self.btn_page = tk.Button(buttons, text=t("update_page"), bg=WHITE, fg=BLACK,
                                  activebackground=WHITE, activeforeground=BLACK,
                                  command=lambda: webbrowser.open(info.get("page_url") or info.get("download_url")))
        self.btn_page.pack(side="left", padx=(8, 0))
        self.btn_quit = tk.Button(buttons, text=t("tray_close"), bg=WHITE, fg=BLACK,
                                  activebackground=WHITE, activeforeground=BLACK, command=self._quit)
        self.btn_quit.pack(side="right")
        if self._update_file:
            self._update_downloaded(self._update_file)

    def _on_update_button(self):
        """The mandatory screen's one button, in its two phases. Both halves are shared with
        the strip, which is the same offer without the window taken away."""
        if self._update_file:
            self._launch_update()
            return
        self._start_update_download()

    def _upd_say(self, text, error: bool = False):
        """Say something about the update on whichever surface is up. Never both: the strip
        comes down when the mandatory screen goes up."""
        if self.update_screen is not None:
            self.upd_status.configure(text=text, fg=DARK_RED if error else BLACK)
        elif self.update_bar is not None:
            self.upd_bar_label.configure(text=text)

    def _start_update_download(self):
        """Fetch the installer on a worker thread.

        Safe at any moment, a match included: it writes one file into <state>/updates and
        touches nothing else. The disruptive half is _launch_update, and that is a second
        deliberate click away."""
        if self._update_busy:
            return
        info = self._update_info or {}
        if not info.get("download_url"):
            return
        self._update_busy = True
        if self.update_screen is not None:
            self.btn_update.configure(state="disabled")
            self._upd_say(t("update_downloading", pct=0))
        else:
            self._upd_say(t("update_banner_progress", pct=0))
        self._paint_update_banner()

        def work():
            try:
                dest = update_mod.dest_path(info["download_url"], info.get("version", ""))
                path = update_mod.download(
                    info["download_url"], dest, expect_sha=info.get("sha256"),
                    progress=lambda done, total: self.q.put(("upd_progress", done * 100 // total if total else None)))
                self.q.put(("upd_done", path))
            except Exception as e:                     # noqa: BLE001
                self.q.put(("upd_error", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _launch_update(self):
        """Hand the installer over and get out of its way: it closes this hub, replaces the
        installed files and starts the new one (/LAUNCHHUB=1).

        Mid-match that is a real interruption, so it is spelled out first and only happens on
        a yes. What makes the yes safe is that the MATCH does not live in this process: the
        service holds it, keeps the player in it across a dropped stream, and replays whichever
        phase it is in the moment the new hub reconnects. The tab is already remembered in
        state.json (see _show_tab), so the new hub opens on Competitive and rejoins in view."""
        info = self._update_info or {}
        if self._match_in_progress():
            if not messagebox.askokcancel(t("update_match_title"),
                                          t("update_match_body", new=info.get("version", "?")),
                                          parent=self.root):
                return
        try:
            update_mod.launch(self._update_file, info.get("kind"))
        except Exception as e:                     # noqa: BLE001
            self._upd_say(t("update_launch_failed", reason=str(e)), error=True)
            if not os.path.isfile(self._update_file):   # Defender quarantined it — let the user re-download
                self._update_file = None
                self._update_busy = False
                if self.update_screen is not None:
                    self.btn_update.configure(text=t("update_download"))
                self._paint_update_banner()
            return
        self._quit(force=True)

    def _update_downloaded(self, path):
        self._update_busy = False
        self._update_file = path
        if self.update_screen is not None:
            self.upd_status.configure(text=t("update_ready", path=path), fg=BLACK)
            self.btn_update.configure(text=t("update_start"), state="normal")
        self._paint_update_banner()

    def _match_in_progress(self) -> bool:
        """True while Competitive is in a match the player cannot simply walk away from.

        Exactly the session's own LOCKED_PHASES, so "is this a bad moment" has one definition
        in the hub rather than two that can drift apart."""
        session = getattr(self.comp, "session", None)
        try:
            return bool(session is not None and session.locked_in())
        except Exception:        # noqa: BLE001 — never let this be what stops an update
            return False

    # ------------------------------------------------------------ tray / close
    def _start_tray(self):
        if not self.tray_available():
            return
        try:
            self.tray = tray_mod.Tray(APP_NAME, t("tray_open"), t("tray_close"),
                                      on_open=lambda: self.q.put(("tray_open",)),
                                      on_quit=lambda: self.q.put(("tray_quit",)))
            self.tray.start()
        except Exception:        # noqa: BLE001 — no tray, the X closes normally
            self.tray = None

    def _on_close(self):
        """The window's X: hide to the tray when there is one, else quit."""
        if self.tray is None:
            self._quit()
            return
        self.root.withdraw()
        if not self._tray_hinted:
            self._tray_hinted = True
            self.tray.notify(t("tray_hint"), APP_NAME)

    def _show(self):
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except Exception:        # noqa: BLE001
            pass

    def _quit(self, force: bool = False):
        """Really close the hub.

        Mid-match this is worth one question. The hub is the player's end of the match: the
        service replays the phase when it comes back, so reopening really does rejoin — but
        while it is shut nothing answers the connect window, and that is what costs Elo and a
        queue ban. `force` is the updater, which is closing in order to come straight back."""
        if not force and self._match_in_progress():
            # Bring the window back first. This is reachable from the TRAY, and on Windows a
            # modal parented to a withdrawn window is simply invisible: the player would pick
            # "Close Lights Out", see nothing happen, and click it again. Raising it is also what
            # they asked for here, so unlike the rejoin this may take the screen.
            try:
                if self.root.state() != "normal":
                    self.root.deiconify()
                    self.root.lift()
            except Exception:    # noqa: BLE001
                pass
            if not messagebox.askokcancel(t("quit_match_title"), t("quit_match_body"),
                                          parent=self.root):
                return
        from . import recording
        if not recording.ensure_exit():
            return
        if self.tray is not None:
            self.tray.stop()
            self.tray = None
        try:
            self.root.destroy()
        except Exception:        # noqa: BLE001
            pass

    # ------------------------------------------------------------ buttons
    def _known_ids(self) -> set:
        return {e.get("id") for e in (self.catalogue or {}).get("gamemodes", [])}

    def _apply(self, desired):
        """Run ops.apply on a worker thread. `desired` = the set that must end up installed."""
        from .activity import files_locked
        if self.busy or files_locked(getattr(self, "comp", None)):
            return
        # Only gamemodes the catalogue still describes can be (re)built into the pak.
        known = self._known_ids()
        desired = {i for i in desired if i in known}
        dropped = {i for i in self._installed() if i not in known}
        if dropped:
            self._status(t("removing_unlisted", ids=", ".join(sorted(dropped))))

        self._set_busy(True)
        self._status(t("working"))

        def work():
            try:
                st = state_mod.load()
                res = ops.apply(
                    desired, self.catalogue, st, self.game_dir,
                    log=lambda m: self.q.put(("log", m)),
                    progress=lambda f, m: self.q.put(("progress", f, m)),
                )
                self.q.put(("done", res))
            except Exception as e:
                self.q.put(("error", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _on_install(self):
        self._apply(set(self._installed()) | set(self.selected))

    def _on_update(self, mode_id: str):
        """The row's Update button: keep everything installed, fetch this gamemode at the catalogue's version."""
        if self.busy or mode_id not in self._installed():
            return
        self._updating = mode_id
        self._apply(set(self._installed()))

    def _on_uninstall(self):
        self._apply(set(self._installed()) - set(self.selected))

    def _on_language(self):
        if self.busy:
            return
        current = i18n.get_language()
        chosen = LanguageDialog.ask(self.root, current)
        if chosen == current:
            return
        i18n.set_language(chosen)
        apply_font_for_language(chosen)
        self.state["language"] = chosen
        state_mod.update_fields({"language": chosen})
        self._rebuild_ui()

    def _on_browse(self):
        from .activity import files_locked
        if self.busy or files_locked(getattr(self, "comp", None)):
            return
        picked = filedialog.askdirectory(title=t("browse_title"))
        if self.busy or files_locked(getattr(self, "comp", None)):
            return
        if not picked:
            return
        picked = os.path.normpath(picked)
        # accept the game folder itself, or steamapps\common (we look one level down)
        for cand in (picked, os.path.join(picked, "Bodycam")):
            if game_mod.is_game_dir(cand):
                self.game_dir = os.path.normpath(cand)
                self.state["game_dir"] = self.game_dir
                note = state_mod.reconcile(self.state, self.game_dir)
                state_mod.update_fields({key: self.state[key] for key in state_mod.INSTALL_FIELDS})
                self._refresh_rows()
                self._status(note or self._game_line(), error=False)
                return
        self._status(t("not_game_folder"), error=True)

    # ------------------------------------------------------------ worker -> UI pump
    def _pump(self):
        """Drain the worker queue on the main thread; scheduled every 100 ms."""
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "catalogue":
                    if self._use_catalogue(msg[1]):
                        self._status(self._game_line())
                elif kind == "catalogue_error":
                    cached = self.state.get("catalogue_cache")
                    if cached:
                        self._use_catalogue(cached, from_cache=True)
                    else:
                        self.catalogue = None
                        self._refresh_rows()
                        self._status(t("catalogue_error"), error=True)
                elif kind == "progress":
                    _frac, text = msg[1], msg[2]
                    if text:
                        self._status(text)
                elif kind == "log":
                    pass                      # the full log goes to logs/last-run.log
                elif kind == "done":
                    self.state = state_mod.load()
                    self._set_busy(False)
                    self._refresh_rows()      # selection cleared: the job is done
                    res = msg[1] or {}
                    was_update = getattr(self, "_updating", None) is not None
                    self._updating = None
                    if res.get("removed"):
                        self._status(t("done_uninstalled"), revert_after=5000)
                    elif was_update:
                        self._status(t("done_updated"), revert_after=8000)
                    else:
                        self._status(t("done_installed"), revert_after=8000)
                elif kind == "error":
                    self._updating = None
                    self.state = state_mod.load()
                    self._set_busy(False)
                    self._refresh_rows(keep_selection=True)
                    self._status(msg[1], error=True)
                    messagebox.showerror(APP_NAME, msg[1])
                elif kind == "upd_progress":
                    if msg[1] is not None:
                        self._upd_say(t("update_downloading", pct=msg[1])
                                      if self.update_screen is not None
                                      else t("update_banner_progress", pct=msg[1]))
                elif kind == "upd_done":
                    self._update_downloaded(msg[1])
                elif kind == "upd_error":
                    self._update_busy = False
                    if self.update_screen is not None:
                        self.upd_status.configure(text=t("update_failed", reason=msg[1]), fg=DARK_RED)
                        self.btn_update.configure(state="normal")
                    elif self.update_bar is not None:
                        self.upd_bar_label.configure(text=t("update_banner_failed", reason=msg[1]))
                        self.btn_update_bar.configure(text=t("update_banner_action"), state="normal")
                elif kind == "comp":
                    # the Competitive tab hopping back to the main thread from a worker
                    try:
                        msg[1]()
                    except Exception:     # noqa: BLE001 — never let the tab kill the pump
                        # ...but never swallow it silently either. An IndexError in here once
                        # froze the Competitive tab on "Match found" with nothing at all to go
                        # on (2026-09-14). The pump survives; the traceback goes to the log.
                        try:
                            import traceback
                            with open(paths.log_file(), "a", encoding="utf-8") as f:
                                f.write("\n[competitive] " + traceback.format_exc())
                        except Exception:  # noqa: BLE001
                            pass
                elif kind == "tray_open":
                    self._show()
                elif kind == "tray_quit":
                    self._quit()
                    return                    # no more pumping: the root is gone
        except queue.Empty:
            pass
        # A required update held back by a live match (see _check_hub_update). The phase is a
        # plain string read, so asking every 100 ms costs nothing - and the alternative, a hook
        # in the session, would put knowledge of the update screen somewhere that should not
        # have any.
        if self._update_required_held and not self._match_in_progress():
            self._update_required_held = False
            self._check_hub_update()

        self.root.after(100, self._pump)


def selfcheck(show: bool = True, verify_own_signature: bool = False) -> int:
    """`--selfcheck`: prove the bundled pak builder and its Oodle module import. Exit 0 = OK.

    The Windows exe is a windowed program, so a console never sees its prints: the result
    is also written to <state>/logs/selfcheck.txt and shown in a message box."""
    import sys as _sys
    from . import paths, version
    try:
        if verify_own_signature:
            from .update_trust import verify_signature
            report = verify_signature(_sys.executable)
            if report["product"] != version.PRODUCT_NAME or report["version"] != list(version.version_tuple()):
                raise RuntimeError("signed executable identity does not match the frozen app")
        d = paths.ensure_builder_on_path()
        import build_gamemode  # noqa: F401  (the bundled tools/pak)
        import ooz             # noqa: F401  (pyooz, needed by the builder)
        # Web UI screens must REGISTER in the frozen build. They are imported explicitly
        # (hub/webui/screens/__init__.py) precisely because a frozen bundle drops dynamically
        # discovered modules — which once shipped an app with zero screens (raw i18n keys, dead
        # verbs). Assert here, inside the FROZEN exe, so a build that lost the screens fails the
        # release instead of the user's download.
        from .webui import screens as _screens
        _expected = set(_screens._SCREEN_MODULES)
        _got = set(_screens.SCREEN_SNAPSHOTS.keys())
        if _got != _expected:
            raise RuntimeError(f"web UI screens did not register (frozen-bundle regression): "
                               f"missing={_expected - _got}, extra={_got - _expected}")
        for _v in ("find_match", "sign_in", "create_party"):
            if _v not in _screens.SCREEN_VERBS:
                raise RuntimeError(f"web UI verb {_v!r} not registered (frozen-bundle regression)")
        # The per-match lobby pak travels as DATA (tools/pak/*.py loaded by path, plus our cooked
        # GameMode), so PyInstaller cannot see either dependency and dropping one is silent: the hub
        # builds, installs, and then quietly tells every host to open the map by hand. 2.0.4 shipped
        # exactly that way. Assert both here, inside the frozen exe, so it fails the release instead.
        from . import lobbypak as _lobbypak
        _lobbypak._pak_module("retarget_lobby")
        _lobbypak._pak_module("build_lobby_override")
        if not _lobbypak.seed_dir():
            raise RuntimeError("hub/lobbyseed not bundled: the hub cannot build a lobby pak, so "
                               "auto-host would silently never happen (frozen-bundle regression)")
        msg = (f"{version.APP_NAME} {version.HUB_VERSION}: builder OK ({d}), ooz OK, "
               f"webui {len(_got)} screens + {len(_screens.SCREEN_VERBS)} verbs OK, "
               f"state dir {paths.state_dir()}, languages {','.join(i18n.CODES)}"
               + (", native signature verification OK" if verify_own_signature else ""))
        code = 0
    except Exception as e:                      # noqa: BLE001
        msg = f"SELFCHECK FAILED: {e!r}"
        code = 1
    print(msg, file=_sys.stdout if code == 0 else _sys.stderr)
    try:
        log_path = os.path.join(str(paths.logs_dir()), "selfcheck.txt")
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(msg + "\n")
        msg += f"\n\n(written to {log_path})"
    except Exception:                           # noqa: BLE001
        pass
    if show:
        try:
            root = tk.Tk()
            root.withdraw()
            (messagebox.showinfo if code == 0 else messagebox.showerror)(f"{version.APP_NAME}: selfcheck", msg)
            root.destroy()
        except Exception:                       # noqa: BLE001 — no display: the print/file is enough
            pass
    return code


def main():
    import sys as _sys
    if "--selfcheck" in _sys.argv:
        raise SystemExit(selfcheck(show="--quiet" not in _sys.argv,
                                   verify_own_signature="--verify-signature" in _sys.argv))
    # --local  (testing: catalogue + packs from this checkout's server/public, no server needed)
    global LOCAL_REPO
    if "--local" in _sys.argv:
        LOCAL_REPO = paths.repo_root()
        if not LOCAL_REPO:
            root = tk.Tk(); root.withdraw()
            messagebox.showerror(APP_NAME, "--local needs the exe to live in <Gamemode Project>\\dist\\ "
                                 "(or the package to run from the checkout): server\\public was not found.")
            raise SystemExit(2)
    # ONE HUB AT A TIME (hub/singleton.py). The X button hides to the tray instead of quitting,
    # so the ordinary way to come back is the shortcut — and without this every one of those
    # relaunches started a whole second hub, exe plus a six-process WebView2 stack, each polling
    # /state every 300 ms and compositing against whatever game is running. They only cleared on a
    # reboot. Placed here because the lock name depends on --local (a checkout run alongside the
    # installed hub is legitimate) and because nothing has been drawn yet, so the exit is silent.
    if not singleton_mod.claim(LOCAL_REPO):
        from . import recording
        if recording.enabled():
            recording.notify('Close Lights Out first', 'Quit the other Lights Out client from its tray menu, then open Lights Out Recording.')
        singleton_mod.focus_existing()           # best effort: make the running hub reappear
        raise SystemExit(0)                      # 0, not an error: this is the hub working

    from . import recording
    if not recording.ensure_start():
        raise SystemExit(1)

    from . import match_cleanup
    from . import telemetry
    telemetry.start()
    match_cleanup.resume_pending_jobs()
    # Capture the directory before scheduling; only the app lifecycle retries revocations.
    from . import auth as auth_mod
    import threading
    signout_folder = paths.state_dir() / "pending-signouts"
    threading.Thread(target=auth_mod.retry_signouts, args=(signout_folder,), daemon=True).start()

    # --lang xx  (testing: skip/override the saved language for this run)
    language = None
    if "--lang" in _sys.argv:
        i = _sys.argv.index("--lang")
        if i + 1 < len(_sys.argv):
            language = _sys.argv[i + 1]
    try:
        update_mod.clean_old_versions()          # a freshly started newer version tidies the old exes beside it
    except Exception:                            # noqa: BLE001
        pass
    # The redesigned web UI (docs/ui-redesign-plan.md) is now the DEFAULT: a native pywebview
    # window rendering the HTML/CSS/JS design. `--classic` forces the old Tkinter UI, and if the
    # webview cannot start (no WebView2 runtime, an import/launch error) we fall back to Tk
    # automatically so the app is never dead. pywebview is imported ONLY inside the webui path,
    # so the Tk fallback never needs it.
    want_webui = "--classic" not in _sys.argv
    if want_webui:
        try:
            from .webui import shell as webui_shell
            webui_shell.run(language=language, local_repo=LOCAL_REPO)
            return
        except Exception as exc:                 # noqa: BLE001 — any webview failure falls back to Tk
            telemetry.emit("app.error", severity="error", code="webview_fallback", error_class=type(exc).__name__)
            print("Lights Out: the web UI could not start (%s); falling back to the classic UI." % exc)
    root = tk.Tk()
    HubApp(root, language=language)
    root.mainloop()


if __name__ == "__main__":
    main()
