"""GraphForge — editable, grounded knowledge graphs from uploaded artifacts."""

import os

# Windows blocks os.symlink without Developer Mode or admin rights, and the
# Hugging Face cache symlinks every snapshot file to its blob -- so loading the
# extraction model dies with "WinError 1314: A required privilege is not held".
# Disabling symlinks makes the hub copy instead. Must be set before anything
# imports huggingface_hub, which reads this at import time.
if os.name == "nt":
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")

__version__ = "0.1.0"
