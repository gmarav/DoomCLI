"""Console input: cross-platform key polling straight from the terminal.

Windows — plain GetAsyncKeyState polling, exactly like the reference
super-mario-cli. No low-level hook is needed: the console is the monitor, so
game keys have nowhere harmful to leak (DoomPaint needed a WH_KEYBOARD_LL hook
only to stop them reaching the focused Paint window).

Linux / macOS — reads key events from the terminal itself in raw-ish mode
(console_out.set_raw_input): no X server, no pynput, no accessibility
permission, so it works in WSL, over SSH, inside tmux/screen, anywhere a real
terminal is attached.

The terminal is asked to speak the Kitty keyboard protocol (CSI u,
`CSI > 31 u`), which modern terminals — Windows Terminal, kitty, iTerm2 3.4+,
foot, WezTerm, Konsole, … — honor. It reports *all* keys as escape codes,
including bare modifiers (a bare Ctrl press is `CSI 57442 ; 5 u`) and separate
press/repeat/release events. That gives:

* **bare Ctrl = fire** (the DoomPaint behavior), since a naked Ctrl press is
  now an event rather than an invisible modifier;
* exact key state — a real release event ends a hold, so there's no release
  lag (fallback terminals still use auto-repeat + grace, see below).

Terminals that don't speak the protocol ignore the enable and keep sending
plain text/escape sequences; those are parsed too. Because plain terminals
only report presses, never releases, held keys are then tracked via the
terminal's key auto-repeat with a ~0.1 s release grace, Shift-as-run is
detected via uppercase letters / CSI modifiers, and Ctrl+letter / Ctrl+arrow
chords still move while firing.
"""
import os
import select
import sys
import threading
import time

VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN = 0x25, 0x26, 0x27, 0x28
VK_SHIFT, VK_CONTROL = 0x10, 0x11
VK_SPACE = 0x20
VK_OEM_COMMA, VK_OEM_PERIOD = 0xBC, 0xBE
VK_F12 = 0x7B
VK_P = ord("P")
VK_F1 = 0x70
VK_LBUTTON = 0x01

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    import ctypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    def _active(vk: int) -> bool:
        """Key is down now, or was tapped since the last poll (0x0001 bit)."""
        return bool(_user32.GetAsyncKeyState(vk) & 0x8001)

else:
    _held: set[int] = set()       # keys currently "held"
    _tapped: set[int] = set()     # pressed since the last drain (tap latch)
    _last_seen: dict[int, float] = {}  # stdin backend: last event time
    _repeated: set[int] = set()   # a repeat/release-confirmed the hold
    _kitty_vks: set[int] = set()  # keys observed via kitty events (releases reliable)

    _GRACE_REPEAT = 0.12  # legacy: drop a repeat-confirmed key this long after last repeat
    _GRACE_FIRST = 0.60   # legacy: ...and a fresh press before its first auto-repeat
    _GRACE_KITTY = 30.0   # kitty/mouse: releases are authoritative; long safety net

    def _on_key(vk: int) -> None:
        now = time.monotonic()
        if vk in _held:
            _repeated.add(vk)  # auto-repeat: the key is genuinely held
        else:
            _held.add(vk)
            _tapped.add(vk)
        _last_seen[vk] = now

    def _release_key(vk: int) -> None:
        """A real release event (Kitty protocol): end the hold, but keep any
        latched tap so a quick tap isn't lost between engine polls."""
        _held.discard(vk)
        _repeated.discard(vk)
        _last_seen.pop(vk, None)

    def _expire() -> None:
        """Release-lag heuristic for terminals without release events.

        A key whose releases are known to be authoritative (it arrived via a
        kitty CSI-u / release event, or it's the mouse whose SGR releases are
        always reported) is only dropped as a long safety net for a *missed*
        release. Other keys — which the terminal may still be sending as plain
        bytes even while other keys come through kitty — are tracked via key
        auto-repeat with a short grace after a repeat confirms the hold.
        """
        now = time.monotonic()
        for k in list(_held):
            if k in _kitty_vks or k == VK_LBUTTON:
                grace = _GRACE_KITTY
            else:
                grace = _GRACE_REPEAT if k in _repeated else _GRACE_FIRST
            if now - _last_seen.get(k, now) > grace:
                _held.discard(k)
                _repeated.discard(k)

    # --- terminal escape-sequence parsing -------------------------------
    _IDLE, _IN_ESC, _IN_CSI, _IN_SS3 = 0, 1, 2, 3
    _pstate = _IDLE
    _pfields: list[list[int]] = [[]]  # CSI params: ';' fields, ':' sub-values
    _pmouse = False                   # CSI <b;x;yM/m SGR mouse event

    def _parse_bytes(data: bytes) -> list:
        """Byte buffer -> [(kind, payload), ...]. kind: 'char' (byte), 'csi'
        ((fields, final)), 'mouse' ((button, x, y, pressed)), 'ss3' (final).
        fields is a list of list of ints: ';' starts a new field, ':' a new
        sub-value within a field. Parser state persists across calls, so
        sequences split between reads still decode.
        """
        global _pstate, _pfields, _pmouse
        events = []
        for b in data:
            if _pstate == _IDLE:
                if b == 0x1B:
                    _pstate = _IN_ESC
                else:
                    events.append(("char", b))
            elif _pstate == _IN_ESC:
                if b == 0x5B:          # '[' -> CSI
                    _pstate = _IN_CSI
                    _pfields = [[]]
                    _pmouse = False
                elif b == 0x4F:        # 'O' -> SS3
                    _pstate = _IN_SS3
                else:
                    events.append(("char", 0x1B))  # lone ESC, ignored
                    _pstate = _IN_ESC if b == 0x1B else _IDLE
            elif _pstate == _IN_CSI:
                if 0x40 <= b <= 0x7E:  # final byte
                    if _pmouse:
                        f = _pfields
                        events.append(("mouse", (
                            f[0][0] if f and f[0] else 0,
                            f[1][0] if len(f) > 1 and f[1] else 0,
                            f[2][0] if len(f) > 2 and f[2] else 0,
                            b == 0x4D)))  # 'M' press, 'm' release
                    else:
                        events.append(("csi", (list(_pfields), b)))
                    _pstate = _IDLE
                elif b == 0x3C and not _pfields[0]:
                    _pmouse = True     # SGR mouse prefix '<'
                elif b == 0x3B:        # ';' -> next field
                    _pfields.append([])
                elif b == 0x3A:        # ':' -> next sub-value (event type)
                    _pfields[-1].append(0)
                elif 0x30 <= b <= 0x3F:
                    if not _pfields[-1]:
                        _pfields[-1].append(0)
                    _pfields[-1][-1] = _pfields[-1][-1] * 10 + (b - 0x30)
            elif _pstate == _IN_SS3:
                events.append(("ss3", b))
                _pstate = _IDLE
        return events

    # CSI '~' final with the xterm function-key numbers.
    _FKEYS = {11: 1, 12: 2, 13: 3, 14: 4, 15: 5, 17: 6, 18: 7,
              19: 8, 20: 9, 21: 10, 23: 11, 24: 12}

    # Kitty-protocol keycodes for keys that aren't printable codepoints.
    _KITTY_KEYS = {
        32: VK_SPACE,
        44: VK_OEM_COMMA, 46: VK_OEM_PERIOD,
        57441: VK_SHIFT, 57447: VK_SHIFT,        # LEFT/RIGHT_SHIFT
        57442: VK_CONTROL, 57448: VK_CONTROL,    # LEFT/RIGHT_CONTROL
    }

    def _keycode_vk(kc: int) -> int | None:
        if 97 <= kc <= 122:                      # a-z (codepoint form)
            return ord(chr(kc - 32))
        if 65 <= kc <= 90:                       # A-Z (alternate keys)
            return ord(chr(kc))
        if kc in _KITTY_KEYS:
            return _KITTY_KEYS[kc]
        return None

    def _csi_vk(keycode: int, final: int) -> int | None:
        if final == 0x41:
            return VK_UP
        if final == 0x42:
            return VK_DOWN
        if final == 0x43:
            return VK_RIGHT
        if final == 0x44:
            return VK_LEFT
        if final == 0x50:                        # F1-F4 via CSI 1 P..S
            return VK_F1
        if final == 0x51:
            return VK_F1 + 1
        if final == 0x52:
            return VK_F1 + 2
        if final == 0x53:
            return VK_F1 + 3
        if final == 0x7E and keycode in _FKEYS:
            return VK_F1 + _FKEYS[keycode] - 1
        if final == 0x75:                        # 'u' -> kitty key event
            return _keycode_vk(keycode)
        return None

    def _decode_csi(payload) -> "tuple[int, int, bool, bool, int]":
        """(keycode, final, shift, ctrl, event) from a CSI event.

        Event: 1 = press, 2 = repeat, 3 = release. Kitty encodes modifiers as
        1 + bitmask (1=Shift, 4=Ctrl), which for single modifiers matches the
        legacy SGR values (2=Shift, 5=Ctrl) terminals send when the protocol
        is not supported.
        """
        fields, final = payload
        mod = 1
        event = 1
        if len(fields) > 1 and fields[1]:
            mod = fields[1][0]
            if len(fields[1]) > 1:
                event = fields[1][1]
        keycode = fields[0][0] if fields and fields[0] else 0
        shift = bool((mod - 1) & 1)
        ctrl = bool((mod - 1) & 4)
        return keycode, final, shift, ctrl, event

    def _event_to_vk(kind: str, payload) -> "tuple[int | None, bool, bool, int]":
        """(VK, shift_held, ctrl_held, event) for a parsed event."""
        if kind == "mouse":
            button, _x, _y, pressed = payload
            if not pressed:
                # Release events use 'm' and may report any button code
                # (xterm sends 3, some terminals send the button itself) —
                # any button up stops the fire.
                return VK_LBUTTON, False, False, 3
            if button in (0, 2):         # left / right button down
                return VK_LBUTTON, False, False, 1
            return None, False, False, 1
        if kind == "char":
            b = payload
            if b in (0x20, 0x00):        # Space / Ctrl+Space -> use
                return VK_SPACE, False, False, 1
            if 1 <= b <= 26:             # legacy Ctrl+A..Z (no kitty protocol)
                return ord("A") + b - 1, False, True, 1
            if b in (0x2C, 0x2E):        # , .
                return (VK_OEM_COMMA if b == 0x2C else VK_OEM_PERIOD), \
                    False, False, 1
            if 0x41 <= b <= 0x5A:
                return ord(chr(b)), True, False, 1
            if 0x61 <= b <= 0x7A:
                return ord(chr(b - 32)), False, False, 1
            return None, False, False, 1
        if kind == "csi":
            keycode, final, shift, ctrl, event = _decode_csi(payload)
            return _csi_vk(keycode, final), shift, ctrl, event
        return None, False, False, 1  # SS3 (F1-F4) aren't bound

    def _stdin_loop() -> None:
        global _kitty
        try:
            fd = sys.stdin.fileno()
        except (AttributeError, ValueError, OSError):
            return
        while True:
            try:
                ready, _, _ = select.select([fd], [], [], 0.05)
            except (OSError, ValueError):
                return
            if ready:
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    return
                if not data:
                    continue
                for kind, payload in _parse_bytes(data):
                    if kind == "csi" and payload[1] == 0x75:
                        _kitty = True  # a CSI-u key event: protocol is live
                    vk, shift, ctrl, event = _event_to_vk(kind, payload)
                    # A key seen via a kitty CSI-u event, or with a real
                    # release event, has authoritative releases — mark it so
                    # its hold isn't governed by the legacy auto-repeat grace.
                    if (kind == "csi" and payload[1] == 0x75) or event == 3:
                        if vk is not None:
                            _kitty_vks.add(vk)
                        if ctrl:
                            _kitty_vks.add(VK_CONTROL)
                        if shift:
                            _kitty_vks.add(VK_SHIFT)
                    if event == 3:       # real release (kitty protocol)
                        if vk is not None:
                            _release_key(vk)
                        if shift:
                            _release_key(VK_SHIFT)
                        if ctrl:
                            _release_key(VK_CONTROL)
                    else:                # press (1) or repeat (2)
                        if vk is not None:
                            _on_key(vk)
                        if shift:
                            _on_key(VK_SHIFT)
                        if ctrl:
                            _on_key(VK_CONTROL)
            _expire()

    def _active(vk: int) -> bool:
        return vk in _held or vk in _tapped


_kitty = False


def start() -> bool:
    """Start the input backend. Windows polling needs nothing; POSIX enables
    the Kitty protocol (console_out.set_kitty_protocol) and starts a reader
    thread on stdin. Whether the terminal actually speaks CSI u is detected at
    runtime from the events it sends (see _stdin_loop)."""
    if _IS_WINDOWS:
        return True
    threading.Thread(target=_stdin_loop, daemon=True,
                     name="doomcli-stdin").start()
    return True


def kitty_active() -> bool:
    """True once the terminal has been observed emitting CSI-u key events."""
    return _kitty


# Each Doom button maps to one or more physical keys (any of them triggers it).
# Order must match the buttons registered in engine_vzd.BUTTONS.
_ACTION_BINDINGS = (
    (VK_UP, ord("W")),          # MOVE_FORWARD
    (VK_DOWN, ord("S")),        # MOVE_BACKWARD
    (VK_LEFT, ord("A")),        # TURN_LEFT
    (VK_RIGHT, ord("D")),       # TURN_RIGHT
    (VK_CONTROL, ord("F"), VK_LBUTTON),  # ATTACK (Ctrl, F, or left mouse)
    (VK_SPACE,),                # USE / open doors
    (VK_SHIFT,),                # SPEED (run)
    (ord("Q"), VK_OEM_COMMA),   # MOVE_LEFT (strafe)
    (ord("E"), VK_OEM_PERIOD),  # MOVE_RIGHT (strafe)
)

_BINDING_VKS = frozenset(vk for binding in _ACTION_BINDINGS for vk in binding)

CONTROLS_HELP = (
    "WASD / arrows move+turn | Q E (or , .) strafe | Ctrl, F or LMB fire | "
    "Space use/open | Shift run | P pause | F12 quit"
)


def poll_action() -> list[int]:
    """Current action vector in engine button order (down OR tapped)."""
    if _IS_WINDOWS:
        return [1 if any(_active(vk) for vk in binding) else 0
                for binding in _ACTION_BINDINGS]
    # Drain only the taps that belong to a bound key, so a P / F12 tap stays
    # latched for pause_requested() / quit_requested() on the game thread.
    taps = {vk for vk in _tapped if vk in _BINDING_VKS}
    _tapped.difference_update(taps)
    return [1 if any(vk in _held or vk in taps for vk in binding) else 0
            for binding in _ACTION_BINDINGS]


def quit_requested() -> bool:
    if _IS_WINDOWS:
        return _active(VK_F12)
    return VK_F12 in _held or VK_F12 in _tapped


def pause_requested() -> bool:
    if _IS_WINDOWS:
        return _active(VK_P)
    hit = VK_P in _tapped
    _tapped.discard(VK_P)  # edge-trigger: holding P must not retoggle
    return hit
