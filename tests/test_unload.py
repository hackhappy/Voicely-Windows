"""When the speech model leaves RAM. Run: python tests/test_unload.py"""
import time

from voicely import engine


def should_unload(idle, busy):
    e = engine.Engine()
    e.last_used = time.monotonic() - idle
    engine.system_busy = lambda: busy
    return e._should_unload()


assert not should_unload(10, busy=True)  # just used: keep it for the next sentence
assert should_unload(engine.BUSY_UNLOAD_SECONDS + 1, busy=True)
assert not should_unload(engine.BUSY_UNLOAD_SECONDS + 1, busy=False)
assert should_unload(engine.IDLE_UNLOAD_SECONDS + 1, busy=False)
print("ok")
