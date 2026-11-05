import os

# Must precede numpy: its BLAS otherwise commits ~750 MB of per-thread buffers we never use.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
