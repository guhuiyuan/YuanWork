"""The build benchmark.

Three pieces:

* ``corpus`` - the synthetic corpus: a known shape, a fixed seed, the
  same bytes every time
* ``profile`` - the shape of the real workspace, counted without
  reading a word of it
* ``run`` - the benchmark itself: what a build costs, and whether it is
  slower than the last one
"""

from __future__ import annotations
