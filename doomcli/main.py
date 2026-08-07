"""DOOM CLI: the real engine, the text console as the monitor.

The same engine as MS Paint Doom (ViZDoom + the real shareware DOOM1.WAD) runs
headlessly; instead of pasting each frame into Paint via the clipboard, every
frame is downsampled to the terminal's character grid and drawn as
24-bit-color half-blocks (▀) — the same console technique as super-mario-cli.

Usage:  python -m doomcli.main [--map E1M1] [--wad 1|2] [--skill 1-5]
                               [--res 320x200|320x240|640x400|640x480]
                               [--no-sound] [--no-music]
"""
import argparse
import io
import os
import re
import signal
import sys
import threading
import time

import numpy as np

from . import console_out, keys
from . import music as music_mod
from .engine_vzd import TICRATE, DoomEngine
from .music import MusicPlayer, PygameMusicPlayer

MAX_TICS_PER_FRAME = 4  # cap catch-up so slow consoles slow the game, not warp it

_IS_WINDOWS = sys.platform == "win32"

_STATUS_ROWS = 1  # bottom line: status/controls summary
_BOOT = "DOOM IS RUNNING IN THE TERMINAL."


class OnDemandRenderer:
    """Free-run the engine on a dedicated thread at the tic rate; the render
    thread reads the most recent finished frame.

    ViZDoom is not thread-safe, so the engine is only ever touched from this
    one thread. Input is polled here once per tic, so control latency tracks
    the 35 Hz tic rate rather than the console's render rate. The frame is
    copied before being shared, because ViZDoom reuses its own framebuffer.
    """

    def __init__(self, engine, max_tics, poll_fn, log_fn=lambda m: None):
        self._engine = engine
        self._max_tics = max_tics
        self._poll = poll_fn
        self._log = log_fn
        self._latest_frame = None
        self._last = time.perf_counter()
        self._paused = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._new_frame = threading.Event()
        try:
            self._latest_frame = self._step_encode()  # seed so the very first
            self._new_frame.set()                     # render is never empty
        except Exception:                             # (cold-start)
            pass
        self._thread = threading.Thread(target=self._push_loop, daemon=True,
                                        name="doomcli-engine")
        self._thread.start()

    def set_paused(self, paused: bool) -> None:
        if self._paused and not paused:
            self._last = time.perf_counter()  # don't bank time spent paused
        self._paused = paused

    def stop(self, timeout: float = 5.0) -> bool:
        """Signal the engine thread and wait for it to leave engine.step(), so
        the caller can close ViZDoom (which is not thread-safe) without racing
        an in-flight step. Returns whether the thread actually stopped."""
        self._stop.set()
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _step_encode(self) -> np.ndarray:
        now = time.perf_counter()
        tics = min(self._max_tics, max(1, round((now - self._last) * TICRATE)))
        self._last = now
        # Step one tic at a time, sampling input before each, so a catch-up
        # burst doesn't apply a single input sample to all N tics.
        frame = None
        for _ in range(tics):
            action = self._poll()
            frame = self._engine.step(action, 1)
        return np.array(frame)  # copy: the engine reuses its framebuffer

    def latest_frame(self) -> np.ndarray:
        with self._lock:
            return self._latest_frame

    def wait_new_frame(self, timeout: float) -> bool:
        """Block until the engine has produced a new frame (a tic advanced)."""
        got = self._new_frame.wait(timeout)
        self._new_frame.clear()
        return got

    def _push_loop(self) -> None:
        period = 1.0 / TICRATE
        nxt = time.perf_counter()
        errors = 0
        while not self._stop.is_set():
            if not self._paused:
                try:
                    frame = self._step_encode()
                    with self._lock:
                        self._latest_frame = frame
                    self._new_frame.set()  # a tic advanced; a fresh frame is up
                    errors = 0
                except Exception as e:
                    errors += 1
                    if errors == 1 or errors % 200 == 0:
                        self._log(f"renderer error #{errors}: {e!r}")
            nxt += period
            slack = nxt - time.perf_counter()
            if slack > 0:
                time.sleep(slack)
            else:
                nxt = time.perf_counter()


def run() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--map", default="E1M1", help="E1M1.. (wad 1), MAP01.. (wad 2)")
    ap.add_argument("--wad", type=int, choices=(1, 2), default=1,
                    help="1=Freedoom Phase 1, 2=Phase 2")
    ap.add_argument("--res", choices=("320x200", "320x240", "640x400",
                                      "640x480"), default="320x240",
                    help="engine render resolution (default 320x240). The "
                         "console output is downsampled from this, so the "
                         "smaller sizes are plenty and cheap; 320x200 is "
                         "Doom's native square-pixel res, 320x240 the "
                         "aspect-correct 4:3 view")
    ap.add_argument("--skill", type=int, default=3, choices=range(1, 6))
    ap.add_argument("--no-sound", action="store_true",
                    help="disable all audio (effects and music)")
    ap.add_argument("--no-music", action="store_true",
                    help="disable the looping map music, keep sound effects")
    ap.add_argument("--music-volume", type=int, default=40, metavar="0-100",
                    help="music loudness relative to sound effects "
                         "(default 40; the MIDI synth runs hot)")
    ap.add_argument("--music-wad", default=None, metavar="PATH",
                    help="WAD to take the soundtrack from — point it at a "
                         "commercial doom.wad you own for the original "
                         "tracks (wad\\doom.wad / wad\\doom2.wad are "
                         "auto-detected). Game data stays Freedoom.")
    ap.add_argument("--width", type=int, default=0, metavar="COLS",
                    help="console output width in characters (default: the "
                         "terminal's current width)")
    ap.add_argument("--height", type=int, default=0, metavar="ROWS",
                    help="console output height in characters (default: the "
                         "terminal's current height)")
    ap.add_argument("--pause-on-focus-loss", action="store_true",
                    help="pause when the terminal isn't the foreground window "
                         "(note: unreliable in some terminal hosts, e.g. "
                         "Windows Terminal, where the conhost handle never "
                         "matches the host window — off by default)")
    args = ap.parse_args()

    # The half-block glyph (▀) isn't in every legacy Windows code page; force
    # UTF-8 on stdout so rendering works whether it's a console or a pipe.
    if sys.stdout and hasattr(sys.stdout, "buffer"):
        try:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
        except Exception:
            pass

    # Ctrl is the fire key; a stray Ctrl+C (or Ctrl+Break) must never kill
    # the game. Quit is F12.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.SIG_IGN)

    # Session log: what the game actually saw, for post-mortem diagnosis.
    log_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "last_run.log")
    session_log = open(log_path, "w", buffering=1, encoding="utf-8")

    def log(msg: str) -> None:
        session_log.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")

    log(f"boot args={vars(args)}")
    log("pacing: engine-tic gated (console rate = min(35 Hz, your terminal))")

    # Game data: prefer the real (shareware) DOOM WAD when it covers the
    # requested map — shareware is episode 1 only; Freedoom fills the rest.
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    shareware = os.path.join(repo, "wad", "doom1.wad")
    game_wad = None
    if args.wad == 1 and args.map.upper().startswith("E1") \
            and os.path.exists(shareware):
        game_wad = shareware
        print("Booting DOOM (the real shareware DOOM.WAD)...")
    else:
        print("Booting Doom (this is real Doom; the WAD is Freedoom)...")
    log(f"game wad: {game_wad or 'freedoom'}")
    engine = DoomEngine(wad=args.wad, doom_map=args.map, skill=args.skill,
                        sound=not args.no_sound, game_wad=game_wad,
                        resolution=args.res)
    device_changed_at = [0.0]
    if not args.no_sound:
        status = music_mod.audio_output_status()
        if status:
            print(f"Audio out: {status}")
            pct = re.search(r"at (\d+)%", status)
            if "MUTED" in status or (pct and int(pct.group(1)) == 0):
                print("  (that output is muted/zero — you won't hear a thing)")
            elif pct and int(pct.group(1)) < 25:
                print("  (heads up: master volume is quite low)")
        # Windows persists per-app volumes; repair the engine's SFX session
        # in case a past run (or a mixer tweak) left it silenced.
        music_mod.set_session_volume(1.0, exe="vizdoom.exe")
        def on_device_change():
            device_changed_at[0] = time.perf_counter()
            print("  (default audio device changed — audio should follow; "
                  "if sound effects vanish, F12 and relaunch run.bat)")
        music_mod.watch_default_device(on_device_change)

    if _IS_WINDOWS:
        music = MusicPlayer(volume=args.music_volume / 100)
    else:
        music = PygameMusicPlayer(volume=args.music_volume / 100)
    if not (args.no_sound or args.no_music):
        music_wad = args.music_wad
        if not music_wad:
            wad_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "wad")
            names = ("doom.wad", "doom1.wad") if args.wad == 1 \
                else ("doom2.wad",)
            music_wad = engine.wad_path
            for name in names:
                local = os.path.join(wad_dir, name)
                if os.path.exists(local):
                    music_wad = local
                    break

        # Opening the MIDI sequencer (or rendering via fluidsynth) can take
        # seconds; don't hold up boot.
        def music_boot():
            if music.start(music_wad, args.map):
                which = ("original soundtrack"
                         if music_wad != engine.wad_path else "map track")
                via = "Windows MIDI" if _IS_WINDOWS else "fluidsynth"
                print(f"  (music: looping the {which} via {via})")
            elif music_wad != engine.wad_path \
                    and music.start(engine.wad_path, args.map):
                print("  (music: track missing in the music WAD; using the "
                      "Freedoom one)")
            else:
                hint = (" — on Linux/macOS music needs fluidsynth + "
                        "fluid-soundfont-gm" if not _IS_WINDOWS else "")
                print(f"  (music: no track found for this map){hint}")
        threading.Thread(target=music_boot, daemon=True,
                         name="music-boot").start()

    console_out.enable_vt()
    console_out.clear_screen()
    console_out.hide_cursor()
    if not _IS_WINDOWS:
        console_out.set_raw_input()
        console_out.set_kitty_protocol()
        console_out.set_mouse()
        keys.start()
    cols = args.width or console_out.terminal_size()[0]
    # Leave a little slack at the bottom: some terminals report a height a row
    # or two taller than what is actually visible (window frame, scrollbar,
    # fractional line height), which would push the status line off screen.
    # Subtract a margin unless the user pinned an explicit --height.
    rows = args.height or max(1, console_out.terminal_size()[1] - 2)
    if not args.width or not args.height:
        print(f"Console: {cols}x{rows} chars "
              "(resize the window or pass --width/--height to change)")
    log(f"console grid {cols}x{rows}")

    frame0 = engine.step([0] * 9, 1)  # warm up one tic
    if not args.no_sound:
        # The engine's OpenAL init can come up silent (device race at boot);
        # rebuild it unconditionally now that a tic has run and device
        # enumeration has settled. Harmless if the init was fine.
        engine.reset_sound()
        log("proactive snd_reset after warm-up tic")

    print()
    print(f"  {_BOOT}")
    print(f"  Controls: {keys.CONTROLS_HELP}")
    print()
    print("  F12 quits; P pauses. Alt-tab away and the demons keep fighting "
          "without you.")

    renderer = OnDemandRenderer(engine, MAX_TICS_PER_FRAME, keys.poll_action,
                                log)
    frames = 0
    total = 0
    fps = 0.0
    fps_frames = 0
    fps_t0 = time.perf_counter()
    paused = False
    stat_t0 = time.perf_counter()
    try:
        while True:
            if keys.quit_requested():
                console_out.show_cursor()
                print("\nF12 — quitting.")
                return 0

            # Manual pause toggle (P). Focus-loss pause is opt-in
            # (--pause-on-focus-loss); see its help for why it's off by default.
            if keys.pause_requested():
                paused = not paused
                renderer.set_paused(paused)
                log(f"{'paused' if paused else 'resumed'} (P key)")
            if args.pause_on_focus_loss \
                    and console_out.is_foreground() is False:
                if not paused:
                    log("paused (console lost focus)")
                    paused = True
                    renderer.set_paused(True)
                time.sleep(0.10)
                continue
            if paused:
                time.sleep(0.05)
                continue

            if not renderer.wait_new_frame(0.25):
                continue  # engine produced nothing new (stalled)

            frame = renderer.latest_frame()
            if frame is None:
                continue
            fps_frames += 1
            now = time.perf_counter()
            if now - fps_t0 >= 1.0:
                fps = fps_frames / (now - fps_t0)
                fps_frames = 0
                fps_t0 = now
            # One status row: live stats first (never truncated), then the
            # control summary. Truncated to the terminal width so a narrow
            # window can't wrap the line and push the screen down.
            status_line = (
                f"{args.map.upper()} | {args.res} | {fps:4.1f} fps | "
                f"{keys.CONTROLS_HELP}"
            )
            console_out.render(frame, cols, rows, (status_line[:cols],))

            frames += 1
            total += 1
            if frames % 35 == 0:
                dt = time.perf_counter() - stat_t0
                log(f"{frames / dt:4.1f} fps rendered "
                    f"({total} frames total)")
                frames, stat_t0 = 0, time.perf_counter()
    except KeyboardInterrupt:
        return 0
    finally:
        console_out.show_cursor()
        console_out.restore_kitty_protocol()
        console_out.restore_mouse()
        console_out.restore_input()
        engine_idle = renderer.stop()
        music.stop()
        if engine_idle:
            engine.close()
        else:
            log("renderer thread didn't stop; skipping engine.close()")
        print("\nDoom has left the terminal.")


if __name__ == "__main__":
    sys.exit(run())
