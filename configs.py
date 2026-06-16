"""
GameNative config search space.
Each config is a dict of setting-name → value.
Add/remove entries to match what GameNative actually exposes on your device.
"""

# Ordered from "most likely to work" to "least likely"
CONFIGS = [
    # --- Baseline: low-res, DXVK, no extras ---
    {"renderer": "DXVK", "resolution": "800x600",  "framegen": "off", "async_shaders": "on",  "perf_mode": "Performance"},
    {"renderer": "DXVK", "resolution": "1280x720",  "framegen": "off", "async_shaders": "on",  "perf_mode": "Performance"},
    {"renderer": "DXVK", "resolution": "1920x1080", "framegen": "off", "async_shaders": "on",  "perf_mode": "Performance"},

    # --- VKD3D variants ---
    {"renderer": "VKD3D", "resolution": "800x600",  "framegen": "off", "async_shaders": "on",  "perf_mode": "Performance"},
    {"renderer": "VKD3D", "resolution": "1280x720",  "framegen": "off", "async_shaders": "on",  "perf_mode": "Performance"},

    # --- Frame generation ---
    {"renderer": "DXVK", "resolution": "800x600",   "framegen": "on",  "async_shaders": "on",  "perf_mode": "Performance"},
    {"renderer": "DXVK", "resolution": "1280x720",  "framegen": "on",  "async_shaders": "on",  "perf_mode": "Performance"},

    # --- Balanced power ---
    {"renderer": "DXVK", "resolution": "800x600",   "framegen": "off", "async_shaders": "on",  "perf_mode": "Balanced"},
    {"renderer": "DXVK", "resolution": "1280x720",  "framegen": "off", "async_shaders": "on",  "perf_mode": "Balanced"},

    # --- No async shaders (sometimes more stable) ---
    {"renderer": "DXVK", "resolution": "800x600",   "framegen": "off", "async_shaders": "off", "perf_mode": "Performance"},
    {"renderer": "DXVK", "resolution": "1280x720",  "framegen": "off", "async_shaders": "off", "perf_mode": "Performance"},
]

# How long (seconds) to observe the game running before calling it "stable"
STABILITY_WINDOW = 30

# Screenshot interval during observation (seconds)
OBSERVE_INTERVAL = 5

# Max actions Claude can take per config before giving up on that config
MAX_ACTIONS_PER_CONFIG = 40
