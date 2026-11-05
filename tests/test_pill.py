"""The pill's pixels. Run: python tests/test_pill.py"""
import numpy as np

from voicely.app import PillArt

art = PillArt(1.0)
art.label("Listening")
red = (1.0, 0.27, 0.23)


def blue_pixels(frame):
    b, g, r = (frame[..., i].astype(int) for i in range(3))
    return int(((b - r > 40) & (frame[..., 3] > 200)).sum())


quiet, loud = art.frame(1.0, 0.0, red), art.frame(1.0, 1.0, red)
assert quiet.shape == (art.fh, art.fw, 4) and quiet.dtype == np.uint8
assert quiet.flags.c_contiguous  # handed to SetBitmapBits as raw memory
assert blue_pixels(quiet) > 300  # water is visible even in silence: that is the recording indicator
assert blue_pixels(loud) > 1.5 * blue_pixels(quiet)  # and it rises with the voice
assert (quiet[..., :3].astype(int) <= quiet[..., 3:].astype(int) + 1).all()  # premultiplied, or layered windows show halos
assert quiet[0, 0, 3] == 0  # corners stay transparent
assert (art.frame(1.0, 0.0, red) != art.frame(1.5, 0.0, red)).any()  # the water moves
glass = quiet[art.fh // 2 - 14, art.fw // 2, 3]
assert 120 < glass < 230  # the capsule is see-through, not solid
art.label("Microphone unavailable")  # the longest status: dot and text must stay inside the capsule
assert art.dot_x - 6 > art.m + art.h / 4
print("ok")
