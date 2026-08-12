"""لوحة تحكم VAI — كل ما يشغّل المشروع في نافذة واحدة.

    python scripts/dashboard.py

تغطي ما يفعله التطبيق: تفحص كل ما يعتمد عليه المشروع وتشغّل الناقص منه، ثم
تختار تسجيلاً، تضبط المدة والنمط واللعبة، تشغّل المراحل الست عشرة، تتابعها
حية، تقرأ تقرير الجودة، وتفتح الفيديو.

Tkinter عن قصد: يأتي مع Python فلا شيء يُثبَّت، ويفتح نافذة اختيار ملفات
حقيقية تُمرِّر *مساراً*. الرفع إلى المتصفح كان سيعني نسخ تسجيل بحجم
جيجابايتات قبل أن يبدأ العمل.

المعالجة تجري في خيط منفصل: الواجهة تظل تستجيب، والتقدم يُقرأ من جدول
الوظائف الذي تكتب فيه المراحل أصلاً -- لا مسار ثانٍ لنقل الحالة، ولهذا يبقى
شريط التقدم صادقاً حتى لو تأخرت مرحلة.

فحص الجاهزية يستدعي `HealthService` نفسها التي يستدعيها `doctor.py`، فلا
تتباعد إجابة اللوحة عن إجابة سطر الأوامر.
"""

from __future__ import annotations

import contextlib
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, ttk
from tkinter import font as tkfont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, OSError):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from backend.config.loader import load_config
from backend.config.paths import build_paths
from backend.core.models.enums import HealthStatus, JobStage, JobStatus, VideoMode
from backend.core.models.media import SUPPORTED_CONTAINERS, MediaImport
from backend.core.models.project import ProjectCreate
from backend.database.connection import Database
from backend.database.migrator import migrate
from backend.database.repositories.jobs import JobRepository
from backend.pipeline.runner import PipelineRunner
from backend.services.health import HealthService
from backend.services.media_ingestion import MediaIngestionService
from backend.services.project_manager import ProjectManager

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# اللوحة اللونية: رمادات مائلة إلى الأزرق بأرضية داكنة، ولون إشارة واحد.
# ألوان الحالة (تمام/تنبيه/خطأ) منفصلة عن لون الإشارة حتى لا تتنافس معه.
# ---------------------------------------------------------------------------

BG = "#14171C"
PANEL = "#1B1F26"
RAISED = "#232830"
LINE = "#2C323B"
INK = "#E7EBEF"
MUTED = "#98A0AA"
FAINT = "#6B737D"
ACCENT = "#3FBFBD"
ACCENT_DIM = "#1E3A3C"
OK = "#5FBE70"
WARN = "#E0AC50"
ERR = "#E0736A"
RUN = "#54A9E8"

PAD = 12

#: اسم عربي لكل مرحلة. الترتيب هو ترتيب التنفيذ.
STAGE_NAMES: dict[JobStage, str] = {
    JobStage.IMPORT: "استيراد التسجيل",
    JobStage.PROBE: "فحص الملف",
    JobStage.PROXY: "نسخة معاينة",
    JobStage.AUDIO: "تحليل الصوت",
    JobStage.FRAMES: "استخراج الإطارات",
    JobStage.TRANSCRIPT: "تفريغ الكلام",
    JobStage.AUDIO_EVENTS: "أحداث صوتية",
    JobStage.SCENES: "تقسيم المشاهد",
    JobStage.VISION: "تحليل الصورة",
    JobStage.OCR: "قراءة نصوص الشاشة",
    JobStage.GAME_EVENTS: "أحداث اللعبة",
    JobStage.MOMENTS: "اختيار اللحظات",
    JobStage.STORY: "بناء القصة",
    JobStage.EDL: "التركيب الزمني",
    JobStage.RENDER: "التصدير",
    JobStage.QA: "فحص الجودة",
}

MODE_NAMES: dict[str, str] = {
    "story": "قصة",
    "best_moments": "أفضل اللحظات",
    "compilation": "تجميعة",
}

#: أسماء عربية لفحوص `HealthService`، بالترتيب الذي تُعرض به.
SERVICE_NAMES: dict[str, str] = {
    "ffmpeg": "FFmpeg",
    "gpu": "كرت الشاشة",
    "nvenc": "التسريع",
    "speech": "تفريغ الكلام",
    "ollama": "النماذج",
    "ocr": "قراءة النصوص",
    "remotion": "الترجمات",
    "scenes": "المشاهد",
}

STATUS_COLOURS: dict[JobStatus, str] = {
    JobStatus.QUEUED: FAINT,
    JobStatus.RUNNING: RUN,
    JobStatus.COMPLETED: OK,
    JobStatus.FAILED: ERR,
    JobStatus.CANCELLED: WARN,
}

HEALTH_STATES: dict[HealthStatus, str] = {
    HealthStatus.OK: "ok",
    HealthStatus.WARNING: "warn",
    HealthStatus.FAILED: "err",
    HealthStatus.SKIPPED: "warn",
}

STATE_COLOURS = {"ok": OK, "warn": WARN, "err": ERR}

OLLAMA_URL = "http://127.0.0.1:11434/api/tags"
API_URL = "http://127.0.0.1:8765/api/health"


@dataclass
class Message:
    """خبر واحد من خيط خلفي إلى الواجهة."""

    kind: str
    text: str = ""
    payload: object = None


@dataclass
class Chip:
    """شارة خدمة في شريط الجاهزية."""

    dot: tk.Canvas
    circle: int
    label: tk.Label
    detail: str = ""
    extras: dict = field(default_factory=dict)


def _reachable(url: str, timeout: float = 2.0) -> bool:
    """هل الخدمة تستجيب فعلاً؟ وجود الأمر لا يعني أنه يعمل."""
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True
    except (urllib.error.URLError, OSError):
        return False


def _spawn(argv: list[str]) -> None:
    """يشغّل خدمة في الخلفية بلا نافذة سوداء تظهر وتختفي."""
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    subprocess.Popen(
        argv, cwd=str(ROOT), creationflags=flags,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


# ---------------------------------------------------------------------------
# عناصر واجهة صغيرة
# ---------------------------------------------------------------------------


class Card(tk.Frame):
    """لوحة معنونة. الحدود بلون واحد لأن الظلال في Tk تبدو مرسومة باليد."""

    def __init__(self, parent: tk.Widget, title: str, fonts: dict) -> None:
        super().__init__(parent, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        self.header = tk.Frame(self, bg=PANEL)
        self.header.pack(fill="x", padx=PAD, pady=(PAD, 0))
        tk.Label(
            self.header, text=title, font=fonts["h2"], bg=PANEL, fg=INK, anchor="e"
        ).pack(side="right")
        self.body = tk.Frame(self, bg=PANEL)
        self.body.pack(fill="both", expand=True, padx=PAD, pady=PAD)


class StageRow(tk.Frame):
    """سطر مرحلة: نقطة حالة، اسم، ونسبة أو زمن."""

    def __init__(self, parent: tk.Widget, stage: JobStage, fonts: dict) -> None:
        super().__init__(parent, bg=PANEL)
        self.detail = tk.Label(
            self, text="", font=fonts["mono"], bg=PANEL, fg=FAINT, anchor="w", width=9
        )
        self.detail.pack(side="left")

        self.dot = tk.Canvas(self, width=13, height=13, bg=PANEL, highlightthickness=0, bd=0)
        self.dot.pack(side="right", padx=(0, 8))
        self._circle = self.dot.create_oval(3, 3, 10, 10, fill=FAINT, outline="")

        self.name = tk.Label(
            self, text=STAGE_NAMES[stage], font=fonts["body"], bg=PANEL, fg=MUTED, anchor="e"
        )
        self.name.pack(side="right")

    def set(self, status: JobStatus | None, progress: float, seconds: float | None) -> None:
        self.dot.itemconfig(self._circle, fill=STATUS_COLOURS.get(status, FAINT))
        self.name.configure(fg=INK if status and status is not JobStatus.QUEUED else MUTED)

        if status is JobStatus.RUNNING:
            self.detail.configure(text=f"{progress * 100:>3.0f}%", fg=RUN)
        elif status is JobStatus.COMPLETED:
            self.detail.configure(text=f"{seconds or 0:.1f}s", fg=FAINT)
        elif status is JobStatus.FAILED:
            self.detail.configure(text="فشل", fg=ERR)
        elif status is JobStatus.CANCELLED:
            self.detail.configure(text="أُلغي", fg=WARN)
        else:
            self.detail.configure(text="", fg=FAINT)


# ---------------------------------------------------------------------------
# التطبيق
# ---------------------------------------------------------------------------


class Dashboard(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("VAI — لوحة التحكم")
        self.geometry("1220x910")
        self.minsize(1020, 730)
        self.configure(bg=BG)

        self.fonts = {
            "h1": tkfont.Font(family="Segoe UI", size=17, weight="bold"),
            "h2": tkfont.Font(family="Segoe UI", size=11, weight="bold"),
            "body": tkfont.Font(family="Segoe UI", size=10),
            "small": tkfont.Font(family="Segoe UI", size=9),
            "mono": tkfont.Font(family="Consolas", size=9),
        }

        self.config_obj = load_config()
        self.paths = build_paths(self.config_obj).create()
        self.database = Database(self.paths.database_path, self.config_obj.application.database)
        migrate(self.database)
        self.jobs = JobRepository(self.database)

        self.sources: list[Path] = []
        self.project_id: str | None = None
        self.worker: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self.queue: queue.Queue[Message] = queue.Queue()
        self.rows: dict[JobStage, StageRow] = {}
        self.chips: dict[str, Chip] = {}
        self.last_output: Path | None = None
        self.started_at = 0.0

        self._style()
        self._build()
        self._load_inbox()
        self.after(120, self._drain)
        self.after(400, self._poll)
        self.refresh_services()
        self.protocol("WM_DELETE_WINDOW", self._close)

    # -- المظهر ---------------------------------------------------------

    def _style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(
            "VAI.Horizontal.TProgressbar",
            troughcolor=RAISED, background=ACCENT,
            bordercolor=RAISED, lightcolor=ACCENT, darkcolor=ACCENT, thickness=6,
        )
        style.configure(
            "VAI.TCombobox",
            fieldbackground=RAISED, background=RAISED, foreground=INK,
            arrowcolor=ACCENT, bordercolor=LINE, lightcolor=LINE, darkcolor=LINE,
            selectbackground=RAISED, selectforeground=INK, padding=5,
        )
        style.map(
            "VAI.TCombobox",
            fieldbackground=[("readonly", RAISED)],
            foreground=[("readonly", INK)],
            selectbackground=[("readonly", RAISED)],
            selectforeground=[("readonly", INK)],
        )
        style.configure("VAI.TNotebook", background=PANEL, borderwidth=0, tabmargins=0)
        style.configure(
            "VAI.TNotebook.Tab", background=BG, foreground=FAINT,
            borderwidth=0, padding=(16, 7), font=self.fonts["small"],
        )
        style.map(
            "VAI.TNotebook.Tab",
            background=[("selected", PANEL)], foreground=[("selected", ACCENT)],
        )
        self.option_add("*TCombobox*Listbox.background", RAISED)
        self.option_add("*TCombobox*Listbox.foreground", INK)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT_DIM)
        self.option_add("*TCombobox*Listbox.selectForeground", INK)

    # -- التخطيط --------------------------------------------------------

    def _build(self) -> None:
        self._header()
        self._services()

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=PAD, pady=(0, PAD))
        body.columnconfigure(0, weight=3, uniform="col")   # يسار: المتابعة
        body.columnconfigure(1, weight=2, uniform="col")   # يمين: التحكم
        body.rowconfigure(0, weight=1)

        left = tk.Frame(body, bg=BG)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, PAD))
        right = tk.Frame(body, bg=BG)
        right.grid(row=0, column=1, sticky="nsew")

        self._controls(right)
        self._monitor(left)

    def _header(self) -> None:
        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=PAD, pady=(PAD, 8))
        tk.Label(bar, text="VAI", font=self.fonts["h1"], bg=BG, fg=ACCENT).pack(side="right")
        tk.Label(
            bar, text="محرر فيديوهات الألعاب", font=self.fonts["body"], bg=BG, fg=MUTED
        ).pack(side="right", padx=(0, 10))
        self.summary = tk.Label(bar, text="", font=self.fonts["small"], bg=BG, fg=MUTED)
        self.summary.pack(side="left")

    def _services(self) -> None:
        """شريط الجاهزية: كل ما يشغّل المشروع، وزر يشغّل الناقص."""
        card = Card(self, "جاهزية التشغيل", self.fonts)
        card.pack(fill="x", padx=PAD, pady=(0, PAD))

        actions = tk.Frame(card.header, bg=PANEL)
        actions.pack(side="left")
        self._button(actions, "شغّل الناقص", self.start_services, primary=True).pack(side="left")
        self._button(actions, "أعد الفحص", self.refresh_services).pack(side="left", padx=(0, 8))

        strip = tk.Frame(card.body, bg=PANEL)
        strip.pack(fill="x")
        # الخادم وOllama أولاً: هما وحدهما ما تستطيع اللوحة تشغيله.
        for key, label in (("api", "الخادم"), ("ollama_up", "Ollama")):
            self.chips[key] = self._chip(strip, label)
        for key, label in SERVICE_NAMES.items():
            self.chips[key] = self._chip(strip, label)

    def _chip(self, parent: tk.Widget, label: str) -> Chip:
        holder = tk.Frame(parent, bg=RAISED, highlightbackground=LINE, highlightthickness=1)
        holder.pack(side="right", padx=(0, 6), pady=2)
        inner = tk.Frame(holder, bg=RAISED)
        inner.pack(padx=9, pady=4)

        text = tk.Label(inner, text=label, font=self.fonts["small"], bg=RAISED, fg=MUTED)
        text.pack(side="right")
        dot = tk.Canvas(inner, width=11, height=11, bg=RAISED, highlightthickness=0, bd=0)
        dot.pack(side="right", padx=(0, 6))

        chip = Chip(dot=dot, circle=dot.create_oval(2, 2, 9, 9, fill=FAINT, outline=""), label=text)
        for widget in (holder, inner, text, dot):
            widget.bind("<Enter>", lambda _event, c=chip: self._chip_detail(c))
        return chip

    def _chip_detail(self, chip: Chip) -> None:
        """تفصيل الفحص عند المرور بالفأرة -- الشارة وحدها لا تكفي للتشخيص."""
        if chip.detail:
            self.summary.configure(text=chip.detail, fg=MUTED)

    def _controls(self, parent: tk.Widget) -> None:
        files = Card(parent, "التسجيل", self.fonts)
        files.pack(fill="x")

        buttons = tk.Frame(files.body, bg=PANEL)
        buttons.pack(fill="x")
        self._button(buttons, "اختر ملفاً…", self._pick, primary=True).pack(side="right")
        self._button(buttons, "من مجلد input", self._load_inbox).pack(side="right", padx=(0, 8))

        self.file_list = tk.Listbox(
            files.body, height=3, bg=RAISED, fg=INK, font=self.fonts["small"],
            selectbackground=ACCENT_DIM, selectforeground=INK,
            highlightthickness=0, bd=0, activestyle="none", justify="right",
        )
        self.file_list.pack(fill="x", pady=(10, 0))

        options = Card(parent, "الإعدادات", self.fonts)
        options.pack(fill="x", pady=(PAD, 0))

        self.minutes = tk.IntVar(value=20)
        self.minutes_label = self._field(options.body, "مدة الفيديو: 20 دقيقة")
        self.minutes.trace_add(
            "write",
            lambda *_: self.minutes_label.configure(
                text=f"مدة الفيديو: {self.minutes.get()} دقيقة"
            ),
        )
        tk.Scale(
            options.body, from_=3, to=60, orient="horizontal", variable=self.minutes,
            bg=PANEL, fg=INK, troughcolor=RAISED, highlightthickness=0, bd=0,
            activebackground=ACCENT, sliderrelief="flat", font=self.fonts["small"],
            sliderlength=24, width=8, showvalue=False,
        ).pack(fill="x")

        self.mode = tk.StringVar(value="story")
        self._field(options.body, "النمط")
        modes = tk.Frame(options.body, bg=PANEL)
        modes.pack(fill="x")
        for value in ("story", "best_moments", "compilation"):
            tk.Radiobutton(
                modes, text=MODE_NAMES[value], value=value, variable=self.mode,
                bg=PANEL, fg=MUTED, selectcolor=RAISED, font=self.fonts["small"],
                activebackground=PANEL, activeforeground=ACCENT,
                highlightthickness=0, bd=0,
            ).pack(side="right", padx=(0, 4))

        self.game = tk.StringVar(value="auto")
        self._field(options.body, "اللعبة")
        games = ["auto", *sorted(p.name for p in self.paths.profiles_dir.iterdir() if p.is_dir())]
        ttk.Combobox(
            options.body, values=games, textvariable=self.game, state="readonly",
            style="VAI.TCombobox", font=self.fonts["small"], justify="right",
        ).pack(fill="x")

        run = Card(parent, "التشغيل", self.fonts)
        run.pack(fill="x", pady=(PAD, 0))
        row = tk.Frame(run.body, bg=PANEL)
        row.pack(fill="x")
        self.start_button = self._button(row, "ابدأ", self._start, primary=True)
        self.start_button.pack(side="right", fill="x", expand=True)
        self.stop_button = self._button(row, "إيقاف", self._stop, danger=True)
        self.stop_button.pack(side="right", padx=(0, 8))
        self._enable(self.stop_button, False)

        self.overall = ttk.Progressbar(
            run.body, style="VAI.Horizontal.TProgressbar", mode="determinate", maximum=100
        )
        self.overall.pack(fill="x", pady=(12, 6))
        self.state_label = tk.Label(
            run.body, text="جاهز", font=self.fonts["small"], bg=PANEL, fg=MUTED, anchor="e"
        )
        self.state_label.pack(fill="x")

        out = Card(parent, "الناتج", self.fonts)
        out.pack(fill="x", pady=(PAD, 0))
        row = tk.Frame(out.body, bg=PANEL)
        row.pack(fill="x")
        self.play_button = self._button(row, "شغّل الفيديو", self._play, primary=True)
        self.play_button.pack(side="right")
        self._button(row, "افتح مجلد المخرجات", self._open_output).pack(side="right", padx=(0, 8))
        self._enable(self.play_button, False)
        tk.Label(
            out.body, text=str(self.paths.output_dir), font=self.fonts["mono"],
            bg=PANEL, fg=FAINT, anchor="w",
        ).pack(fill="x", pady=(8, 0))

    def _monitor(self, parent: tk.Widget) -> None:
        stages = Card(parent, "المراحل", self.fonts)
        stages.pack(fill="x")
        for stage in STAGE_NAMES:
            row = StageRow(stages.body, stage, self.fonts)
            row.pack(fill="x", pady=1)
            self.rows[stage] = row

        tabs = ttk.Notebook(parent, style="VAI.TNotebook")
        tabs.pack(fill="both", expand=True, pady=(PAD, 0))
        self.qa_box = self._text_tab(tabs, "تقرير الجودة", INK)
        self.log_box = self._text_tab(tabs, "السجل", MUTED, mono=True)
        for tag, colour in (("ok", OK), ("warn", WARN), ("err", ERR), ("muted", MUTED)):
            self.qa_box.tag_configure(tag, foreground=colour, justify="right")

    def _text_tab(
        self, tabs: ttk.Notebook, title: str, colour: str, *, mono: bool = False
    ) -> tk.Text:
        frame = tk.Frame(tabs, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        tabs.add(frame, text=title)
        box = tk.Text(
            frame, height=7, bg=PANEL, fg=colour,
            font=self.fonts["mono"] if mono else self.fonts["small"],
            highlightthickness=0, bd=0, wrap="word", state="disabled", padx=12, pady=10,
        )
        box.pack(fill="both", expand=True)
        return box

    # -- عناصر مساعدة ---------------------------------------------------

    def _field(self, parent: tk.Widget, text: str) -> tk.Label:
        label = tk.Label(
            parent, text=text, font=self.fonts["small"], bg=PANEL, fg=MUTED, anchor="e"
        )
        label.pack(fill="x", pady=(8, 3))
        return label

    def _button(
        self, parent: tk.Widget, text: str, command, *,
        primary: bool = False, danger: bool = False,
    ) -> tk.Button:
        colour = ACCENT if primary else (ERR if danger else MUTED)
        button = tk.Button(
            parent, text=text, command=command, font=self.fonts["body"],
            bg=RAISED, fg=colour, activebackground=LINE, activeforeground=colour,
            relief="flat", bd=0, padx=15, pady=6, cursor="hand2",
            highlightthickness=1, highlightbackground=LINE,
        )
        button.vai_colour = colour  # type: ignore[attr-defined]
        return button

    def _enable(self, button: tk.Button, enabled: bool) -> None:
        button.configure(
            state="normal" if enabled else "disabled",
            fg=button.vai_colour if enabled else FAINT,  # type: ignore[attr-defined]
        )

    def _log(self, text: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"{text}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    # -- الجاهزية -------------------------------------------------------

    def refresh_services(self) -> None:
        """يفحص كل ما يشغّل المشروع، في خيط خلفي حتى لا تتجمد النافذة."""
        self.summary.configure(text="يفحص الجاهزية…", fg=MUTED)
        threading.Thread(target=self._probe_services, daemon=True).start()

    def _probe_services(self) -> None:
        found: dict[str, tuple[str, str]] = {
            "api": (
                ("ok", "الخادم يستجيب على 8765") if _reachable(API_URL)
                else ("warn", "الخادم متوقف — اضغط «شغّل الناقص»")
            ),
            "ollama_up": (
                ("ok", "Ollama يستجيب على 11434") if _reachable(OLLAMA_URL)
                else ("warn", "Ollama متوقف — اضغط «شغّل الناقص»")
            ),
        }
        try:
            for check in HealthService(self.config_obj).report().checks:
                if check.name in SERVICE_NAMES:
                    found[check.name] = (
                        HEALTH_STATES.get(check.status, "warn"),
                        check.detail or check.name,
                    )
        except Exception as error:
            self.queue.put(Message("log", f"تعذّر فحص الجاهزية: {error}"))
        self.queue.put(Message("services", payload=found))

    def _apply_services(self, found: dict[str, tuple[str, str]]) -> None:
        problems = 0
        for key, chip in self.chips.items():
            state, detail = found.get(key, ("warn", "غير معروف"))
            chip.dot.itemconfig(chip.circle, fill=STATE_COLOURS.get(state, FAINT))
            chip.label.configure(fg=INK if state == "ok" else STATE_COLOURS.get(state, MUTED))
            chip.detail = detail
            problems += 0 if state == "ok" else 1

        if problems:
            self.summary.configure(text=f"● {problems} بحاجة انتباه", fg=WARN)
        else:
            self.summary.configure(text="● كل شيء جاهز", fg=OK)

    def start_services(self) -> None:
        """يشغّل ما تستطيع اللوحة تشغيله: Ollama وخادم التطبيق.

        الباقي -- FFmpeg، النماذج، الترجمات -- تثبيت لا تشغيل، فتُعرض حالته
        ولا يُدَّعى أن زراً يصلحه.
        """
        started: list[str] = []
        if not _reachable(OLLAMA_URL):
            if shutil.which("ollama"):
                _spawn(["ollama", "serve"])
                started.append("Ollama")
            else:
                self._log("Ollama غير مثبّت على هذا الجهاز.")
        if not _reachable(API_URL):
            _spawn([sys.executable, str(ROOT / "scripts" / "serve.py")])
            started.append("الخادم")

        if started:
            self._log(f"جارٍ تشغيل: {'، '.join(started)}")
            # لا شيء يستجيب فوراً؛ نفحص بعد مهلة بدل أن نعلن نجاحاً لم يحدث.
            self.after(6000, self.refresh_services)
        else:
            self._log("لا شيء ناقص مما تستطيع اللوحة تشغيله.")
            self.refresh_services()

    # -- اختيار الملفات -------------------------------------------------

    def _pick(self) -> None:
        patterns = " ".join(f"*{ext}" for ext in sorted(SUPPORTED_CONTAINERS))
        chosen = filedialog.askopenfilenames(
            title="اختر تسجيلاً",
            initialdir=str(self.paths.input_dir),
            filetypes=[("تسجيلات", patterns), ("كل الملفات", "*.*")],
        )
        if chosen:
            self.sources = [Path(item) for item in chosen]
            self._show_files()

    def _load_inbox(self) -> None:
        self.sources = sorted(
            path for path in self.paths.input_dir.iterdir()
            if path.is_file() and path.suffix.lower() in SUPPORTED_CONTAINERS
        )
        self._show_files()

    def _show_files(self) -> None:
        self.file_list.delete(0, "end")
        for path in self.sources:
            self.file_list.insert(
                "end", f"  {path.name}   ({path.stat().st_size / 1e9:.2f} GB)"
            )
        if not self.sources:
            self.file_list.insert("end", "  لا يوجد — اضغط «اختر ملفاً»")

    # -- التشغيل --------------------------------------------------------

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not self.sources:
            self._log("اختر تسجيلاً أولاً.")
            return

        for row in self.rows.values():
            row.set(None, 0.0, None)
        self.qa_box.configure(state="normal")
        self.qa_box.delete("1.0", "end")
        self.qa_box.configure(state="disabled")

        self.stop_flag.clear()
        self.last_output = None
        self.started_at = time.monotonic()
        self._enable(self.start_button, False)
        self._enable(self.stop_button, True)
        self._enable(self.play_button, False)
        self.state_label.configure(text="يعمل…", fg=RUN)

        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

    def _stop(self) -> None:
        self.stop_flag.set()
        if self.project_id:
            self.jobs.request_cancel(self.project_id)
        self.state_label.configure(text="يُلغى…", fg=WARN)
        self._log("طُلب الإيقاف؛ ستتوقف المرحلة الحالية عند أقرب نقطة آمنة.")

    def _run(self) -> None:
        """خيط المعالجة. لا يلمس الواجهة -- يرسل رسائل فقط."""
        database = Database(self.paths.database_path, self.config_obj.application.database)
        try:
            projects = ProjectManager(database, self.paths, self.config_obj)
            media = MediaIngestionService(database, self.paths, self.config_obj)
            runner = PipelineRunner(database, self.paths, self.config_obj)
            for index, source in enumerate(self.sources, start=1):
                if self.stop_flag.is_set():
                    break
                self.queue.put(Message("log", f"[{index}/{len(self.sources)}] {source.name}"))
                self._run_one(source, projects, media, runner)
        except Exception as error:
            self.queue.put(Message("failed", f"خطأ غير متوقع: {error}"))
        finally:
            self.queue.put(Message("done"))

    def _run_one(self, source: Path, projects, media, runner) -> None:
        project = projects.create(
            ProjectCreate(
                name=source.stem,
                target_duration_seconds=self.minutes.get() * 60,
                mode=VideoMode(self.mode.get()),
                **({} if self.game.get() == "auto" else {"game": self.game.get()}),
            )
        )
        self.project_id = project.id
        self.queue.put(Message("log", f"مشروع {project.id}"))
        media.import_media(project.id, MediaImport(path=str(source)))

        rendered: str | None = None
        while not self.stop_flag.is_set():
            outcome = runner.run_next(project.id)
            if outcome is None:
                break
            job = outcome.job
            if not outcome.succeeded:
                name = STAGE_NAMES.get(job.stage, job.stage.value)
                self.queue.put(Message(
                    "failed", f"{name}: {job.error_message or job.error_code or 'فشل'}"
                ))
                return

            result = job.result or {}
            if job.stage is JobStage.RENDER:
                if result.get("skipped"):
                    self.queue.put(Message(
                        "log", f"لا شيء يستحق التحرير — {result.get('reason', '')}"
                    ))
                else:
                    rendered = result.get("output_path")
            elif job.stage is JobStage.QA:
                self.queue.put(Message("qa", payload=result))

        if rendered:
            target = self.paths.output_dir / f"{source.stem}.mp4"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(rendered, target)
            self.queue.put(Message("log", f"→ {target.name}"))
            self.queue.put(Message("delivered", payload=str(target)))

    # -- حلقات الواجهة --------------------------------------------------

    def _drain(self) -> None:
        """يفرغ رسائل الخيوط الخلفية. الواجهة تُحدَّث من هنا وحدها."""
        try:
            while True:
                self._apply(self.queue.get_nowait())
        except queue.Empty:
            pass
        self.after(120, self._drain)

    def _apply(self, message: Message) -> None:
        if message.kind == "log":
            self._log(message.text)
        elif message.kind == "services":
            self._apply_services(dict(message.payload or {}))
        elif message.kind == "delivered":
            self.last_output = Path(str(message.payload))
            self._enable(self.play_button, True)
        elif message.kind == "qa":
            self._show_qa(dict(message.payload or {}))
        elif message.kind == "failed":
            self._log(message.text)
            self.state_label.configure(text="فشل", fg=ERR)
        elif message.kind == "done":
            self._enable(self.start_button, True)
            self._enable(self.stop_button, False)
            if self.state_label.cget("fg") != ERR:
                elapsed = (time.monotonic() - self.started_at) / 60
                self.state_label.configure(text=f"انتهى في {elapsed:.1f} دقيقة", fg=OK)

    def _poll(self) -> None:
        """يقرأ التقدم من جدول الوظائف -- المصدر نفسه الذي تكتب فيه المراحل."""
        if self.project_id:
            try:
                jobs = self.jobs.list_for_project(self.project_id)
            except Exception:
                jobs = []
            latest = {job.stage: job for job in jobs}
            done = 0
            for stage, row in self.rows.items():
                job = latest.get(stage)
                if job is None:
                    row.set(None, 0.0, None)
                    continue
                row.set(job.status, job.progress, job.duration_seconds)
                done += 1 if job.status is JobStatus.COMPLETED else 0
            self.overall["value"] = done / len(self.rows) * 100
        self.after(400, self._poll)

    def _show_qa(self, result: dict) -> None:
        self.qa_box.configure(state="normal")
        self.qa_box.delete("1.0", "end")

        failures = result.get("failures") or []
        warnings = result.get("warnings") or []
        if result.get("blocks_export"):
            self.qa_box.insert("end", "التصدير موقوف — يوجد خلل تقني\n", "err")
        elif warnings:
            self.qa_box.insert("end", "مرّ مع ملاحظات\n", "warn")
        else:
            self.qa_box.insert("end", "كل الفحوصات سليمة\n", "ok")

        for line in failures:
            self.qa_box.insert("end", f"✗ {line}\n", "err")
        for line in warnings:
            self.qa_box.insert("end", f"⚠ {line}\n", "warn")
        for line in result.get("explanation") or []:
            if line.strip().startswith("→"):
                self.qa_box.insert("end", f"   {line.strip()}\n", "muted")
        self.qa_box.configure(state="disabled")

    # -- المخرجات -------------------------------------------------------

    def _open_output(self) -> None:
        self._reveal(self.paths.output_dir)

    def _play(self) -> None:
        if self.last_output and self.last_output.exists():
            self._reveal(self.last_output)

    def _reveal(self, path: Path) -> None:
        try:
            if sys.platform == "win32":
                os.startfile(str(path))
            else:
                subprocess.run(["xdg-open", str(path)], check=False)
        except OSError as error:
            self._log(f"تعذّر الفتح: {error}")

    def _close(self) -> None:
        self.stop_flag.set()
        if self.project_id and self.worker and self.worker.is_alive():
            with contextlib.suppress(Exception):
                self.jobs.request_cancel(self.project_id)
        self.destroy()


def main() -> int:
    Dashboard().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
