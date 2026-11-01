#!/usr/bin/env python3
"""Remove the kill-matrix job dir on the VM so each cell starts clean."""

import shutil

shutil.rmtree("/content/km-job", ignore_errors=True)
print("reset")
