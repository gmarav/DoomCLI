"""Terminal framebuffer: numpy RGB frames -> ANSI half-block console output.

The DoomCLI counterpart of DoomPaint's paint_out.py. Where Paint got each
frame via the Windows clipboard, here every frame is downsampled to the
terminal's character grid and drawn as 24-bit-color half-blocks (▀) — the same
technique as the reference super-mario-cli renderer.

Each character cell renders two pixel rows (top half -> foreground color,
bottom half -> background color). With a normal half-width terminal font that
makes one source pixel draw as one on-screen square, so the grid keeps the
source's aspect ratio: the picture is centered on the terminal and letterboxed
with black rather than stretched.
"""
import ctypes
import shutil
import sys
from ctypes import wintypes

import numpy as np

_IS_WINDOWS = sys.platform == "win32"

_BLOCK = "\u2580"  # upper half block: fg = top pixel, bg = bottom pixel
_RESET = "\033[0m"
_HOME = "\033[H"
_CLEAR = "\033[2J"
_HIDE_CURSOR = "\033[?25l"
_SHOW_CURSOR = "\033[?25h"
_ERASE_LINE = "\033[K"

_BLACK = (0, 0, 0)
# STD_OUTPUT_HANDLE / STD_ERROR_HANDLE / STD_INPUT_HANDLE
_STD_HANDLES = (-11, -12, -10)
_ENABLE_VT = 0x0004

_saved_tty_attrs = None  # POSIX raw-input mode restore


def enable_vt() -> None:
    """Turn on ANSI/VT escape processing for the attached console.

    Windows 10+ only (Windows Terminal and modern conhost do this by default;
    this makes it explicit for older attached consoles). POSIX terminals
    handle ANSI natively, so this is a no-op there.
    """
    if not _IS_WINDOWS:
        return
    for handle_id in _STD_HANDLES:
        try:
            handle = ctypes.windll.kernel32.GetStdHandle(handle_id)
            if not handle or handle == ctypes.c_void_p(-1).value:
                continue
            mode = wintypes.DWORD()
            if ctypes.windll.kernel32.GetConsoleMode(
                    handle, ctypes.byref(mode)):
                ctypes.windll.kernel32.SetConsoleMode(
                    handle, mode.value | _ENABLE_VT)
        except Exception:
            pass


def hide_cursor() -> None:
    sys_write(_HIDE_CURSOR)


def show_cursor() -> None:
    sys_write(_SHOW_CURSOR)


def clear_screen() -> None:
    sys_write(_CLEAR + _HOME)


def terminal_size(default=(120, 40)) -> tuple[int, int]:
    """(columns, rows) of the attached terminal."""
    try:
        size = shutil.get_terminal_size((default[0], default[1]))
        return size.columns, size.lines
    except OSError:
        return default


def is_foreground() -> bool | None:
    """True if the attached console is the foreground window.

    Returns None when it can't be determined (no attached console, or a POSIX
    host), so the caller can skip the focus check instead of pausing forever.
    """
    if not _IS_WINDOWS:
        return None
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if not hwnd:
            return None
        fg = ctypes.windll.user32.GetForegroundWindow()
        if not fg:
            return None
        root = ctypes.windll.user32.GetAncestor(fg, 2) or fg  # GA_ROOT
        return bool(root == hwnd)
    except Exception:
        return None


def set_raw_input() -> None:
    """POSIX: put the terminal into raw-ish input mode before playing.

    Clears echo (so typed game keys don't clutter the screen), canonical
    mode, and signal generation (so Ctrl — the fire key — never sends SIGINT
    and kills the game). The terminal only reports key presses, not releases,
    so key *state* is tracked by keys.py's stdin reader via auto-repeat.
    No-op on Windows.
    """
    global _saved_tty_attrs
    if _IS_WINDOWS:
        return
    try:
        import termios
        fd = sys.stdin.fileno()
        _saved_tty_attrs = termios.tcgetattr(fd)
        new = termios.tcgetattr(fd)
        new[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG)
        termios.tcsetattr(fd, termios.TCSANOW, new)
    except Exception:
        _saved_tty_attrs = None


def restore_input() -> None:
    """Restore the terminal mode saved by set_raw_input (POSIX only)."""
    global _saved_tty_attrs
    if _IS_WINDOWS or _saved_tty_attrs is None:
        return
    try:
        import termios
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW,
                          _saved_tty_attrs)
    except Exception:
        pass
    _saved_tty_attrs = None


def set_kitty_protocol() -> None:
    """Ask the terminal for the Kitty keyboard protocol (CSI > 31 u).

    Modern terminals (Windows Terminal, kitty, iTerm2 3.4+, foot, WezTerm,
    Konsole, …) then report every key — including bare Ctrl/Shift and explicit
    press/release events — as CSI u sequences, which keys.py parses. Terminals
    that don't support it ignore the request and keep sending plain text.
    POSIX only (Windows input already polls key state directly).
    """
    if _IS_WINDOWS:
        return
    sys_write("\033[>31u")


def restore_kitty_protocol() -> None:
    """Pop the protocol request pushed by set_kitty_protocol (CSI < u)."""
    if _IS_WINDOWS:
        return
    sys_write("\033[<u")


def set_mouse() -> None:
    """Ask the terminal to report button press/release (SGR mouse, 1006).

    Holding the left mouse button then works as fire in any terminal — a real
    fallback for terminals that never report bare modifier keys (Windows
    Terminal suppresses Ctrl/Shift/Alt key events entirely). POSIX only.
    """
    if _IS_WINDOWS:
        return
    sys_write("\033[?1000h\033[?1006h")


def restore_mouse() -> None:
    """Turn SGR mouse reporting back off."""
    if _IS_WINDOWS:
        return
    sys_write("\033[?1000l\033[?1006l")


def fit_grid(frame_w: int, frame_h: int, cols: int, rows: int,
             status_rows: int = 0) -> tuple[int, int]:
    """Character grid (cw, ch) that preserves the source pixel aspect ratio.

    Each cell holds 2 pixel rows, and a cell is ~twice as tall as wide, so a
    source pixel renders square on screen when cw/ch = 2/(h/w). The grid is
    the largest such rectangle that fits `rows` (minus status rows) x `cols`.
    """
    rows = max(1, rows - status_rows)
    a = frame_h / max(1, frame_w)  # source height/width
    cw = min(cols, int(rows * 2 / a))
    ch = max(1, min(rows, int(cw * a / 2)))
    if ch >= rows:  # height-bound: recompute width from the row budget
        cw = min(cols, max(1, int(rows * 2 / a)))
        ch = rows
    return cw, ch


def frame_grid(frame, cw: int, ch: int) -> "tuple[np.ndarray, np.ndarray]":
    """Nearest-neighbor downsample to a (ch, cw) cell grid.

    Returns (top, bottom) arrays of shape (ch, cw, 3): the pixel colors for
    the upper and lower halves of each character cell.
    """
    ih, iw = frame.shape[:2]
    ys = (np.arange(ch * 2) * ih) // (ch * 2)
    xs = (np.arange(cw) * iw) // cw
    img = frame[ys][:, xs]  # (ch*2, cw, 3)
    return img[0::2], img[1::2]


def frame_to_ansi(frame, cols: int, rows: int,
                  status: "tuple[str, ...] = ()") -> str:
    """Encode a frame into an ANSI string that redraws the whole screen.

    The game view is centered in the terminal and letterboxed with black to
    preserve the source aspect; `status` lines render below it.
    """
    status_rows = max(1, len(status))
    cw, ch = fit_grid(frame.shape[1], frame.shape[0], cols, rows, status_rows)
    top, bot = frame_grid(frame, cw, ch)
    x0 = max(0, (cols - cw) // 2)
    rw = max(0, cols - x0 - cw)
    black_key = _BLACK * 2  # (0,0,0,0,0,0) as a tuple, for the run cache

    out = [_HOME]
    for y in range(ch):
        parts = []
        last = None
        if x0 > 0:
            parts.append(f"\033[38;2;0;0;0m\033[48;2;0;0;0m" + " " * x0)
            last = black_key
        for x in range(cw):
            fg = (int(top[y, x, 0]), int(top[y, x, 1]), int(top[y, x, 2]))
            bg = (int(bot[y, x, 0]), int(bot[y, x, 1]), int(bot[y, x, 2]))
            key = fg + bg
            if key != last:
                parts.append(f"\033[38;2;{fg[0]};{fg[1]};{fg[2]}m"
                             f"\033[48;2;{bg[0]};{bg[1]};{bg[2]}m")
                last = key
            parts.append(_BLOCK)
        if rw > 0:
            if last != black_key:
                parts.append("\033[38;2;0;0;0m\033[48;2;0;0;0m")
            parts.append(" " * rw)
        parts.append(_RESET + _ERASE_LINE)
        out.append("".join(parts))
    for _ in range(ch, rows - status_rows):
        out.append(f"\033[48;2;0;0;0m" + " " * cols + _RESET + _ERASE_LINE)
    for line in status:
        out.append(_ERASE_LINE + line + _RESET)
    # No trailing reset as its own line: the last status row already ends in
    # RESET, and a bare trailing line would push the cursor one row below the
    # bottom of the terminal, scrolling the screen one line every frame.
    return "\n".join(out)


def render(frame, cols: int, rows: int,
           status: "tuple[str, ...] = ()") -> None:
    """Encode and write one frame to stdout."""
    sys_write(frame_to_ansi(frame, cols, rows, status))


def sys_write(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()
