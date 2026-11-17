"""YuanWork: Local personal work assistant with wiki technology + memory engineering."""

import os

# LiteLLM fetches its model-cost map from GitHub the first time it is
# imported. When the network is slow or down, that fetch is retried
# three times and the import blocks for half a minute - which the user
# would pay on every cold start, and the test suite pays on every run.
# The bundled copy is enough: the cost map is only used for litellm's
# own cost accounting, and this project keeps its own audit.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

# Where the models this project downloads are kept. This has to happen
# here, in the package's own __init__, because huggingface_hub computes
# its cache location when it is *imported* - set the variable afterwards
# and it is silently ignored (the models land in the old directory
# anyway). Nothing imports huggingface_hub before this point: the
# embedding model is imported lazily, inside the function that loads it
# (ADR-045).
from .paths import apply_model_cache_env as _apply_model_cache_env

_apply_model_cache_env()

__version__ = "0.1.0"
__author__ = "YuanWork Contributors"
__license__ = "Apache-2.0"

# Package-level constants
APP_NAME = "yuanwork"
APP_DESCRIPTION = "Local personal work assistant with wiki technology + memory engineering"

# Paths (resolved at runtime in config.py)
DEFAULT_DATA_DIR = "~/.yuanwork"
DEFAULT_CONFIG_DIR = "configs"
