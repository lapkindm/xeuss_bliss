"""newfile(): change the data file, with spec-like scan numbers.

From xrd3003_bliss (github.com/lapkindm/xrd3003_bliss, commit 23e6e6d), included here 2026-10-21.

Bliss (without a data policy) numbers scans with one counter per session,
so changing the sample does not restart the numbering. newfile() sets the
experimenter and sample (the file is /data_local/bliss/<experimenter>/<sample>.h5) and
sets the counter so that the next scan gets the number after the last scan
already in that file: 1 for a new file.

    from xeuss_bliss.saving import newfile
    newfile("Dima", "251002_PEN_4P_grad")
"""

from __future__ import annotations

import os
import re

_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


def last_scan_number(filename: str) -> int:
    """Highest scan number in a Bliss HDF5 file (entries "N.1", "N.2", ...);
    0 if the file does not exist or has no scans."""
    if not os.path.exists(filename):
        return 0
    import h5py

    # locking=False: the Bliss writer of this session may have the file open
    with h5py.File(filename, "r", locking=False) as f:
        numbers = [int(k.split(".")[0]) for k in f.keys() if re.match(r"^\d+\.\d+$", k)]
    return max(numbers, default=0)


def newfile(experimenter: str, sample: str, scan_saving=None) -> int:
    """Save the next scans in <SCAN_SAVING.base_path>/<experimenter>/<sample>.h5 (/data_local/bliss/... here).

    The next scan number follows the last scan in that file (1 for a new
    file). Returns the next scan number.
    """
    for what, value in (("experimenter", experimenter), ("sample", sample)):
        if not isinstance(value, str) or not _NAME.match(value):
            raise ValueError(f"{what} {value!r}: use only letters, digits, '_', '-' and '.'")
    if scan_saving is None:
        from bliss import current_session

        scan_saving = current_session.scan_saving
    scan_saving.experimenter = experimenter
    scan_saving.sample = sample
    filename = scan_saving.filename
    if "{" in filename:
        raise RuntimeError(f"data file name {filename!r} is not resolved: SCAN_SAVING needs "
                           "the fields used in its template (session setup: SCAN_SAVING.add(...))")
    last = last_scan_number(filename)
    scan_saving.reset_scan_number(next=last + 1)
    state = f"existing file, last scan {last}" if last else (
        "existing file, no scans yet" if os.path.exists(filename) else "new file")
    print(f"Data file: {filename} ({state}); next scan: {last + 1}")
    return last + 1
