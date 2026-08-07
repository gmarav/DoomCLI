"""Smoke test: engine boots headless, produces frames, console-encodes them."""
import os

import numpy as np
import vizdoom as vzd

from doomcli import console_out
from doomcli.engine_vzd import DoomEngine

engine = DoomEngine(wad=1, doom_map="E1M1", skill=3, sound=False,
                    resolution="320x240")
print("engine booted:", engine.wad_path)

first = None
prev = None
changed = False
for i in range(35):  # one second of game time
    first = engine.step([1, 0, 0, 0, 0, 0, 0, 0, 0], 1)  # walk forward
    if prev is not None and not np.array_equal(first, prev):
        changed = True
    prev = first
engine.close()

print("frame shape:", first.shape, first.dtype)
print("frames differ:", changed)

cols, rows = 100, 40
cw, ch = console_out.fit_grid(first.shape[1], first.shape[0], cols, rows, 2)
print(f"grid: {cw}x{ch} chars (source {first.shape[1]}x{first.shape[0]})")
ansi = console_out.frame_to_ansi(first, cols, rows,
                                 status=("WASD move | Ctrl fire", "E1M1 | 320x240"))
print(f"ansi length: {len(ansi)} bytes, lines: {ansi.count(chr(10)) + 1}")
line = ansi.split("\n")[1]
print("first data line:", repr(line[:80].encode("ascii", "replace").decode()))

console_out.enable_vt()
print("smoke test OK")
