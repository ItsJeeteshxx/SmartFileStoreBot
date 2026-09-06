"""
Cloudflare R2 & CDN Helper Bridge
Exposes upload_image_to_r2 and helper methods from AryaPremium.r2_helper.
"""
import sys
import os

_cur_dir = os.path.dirname(os.path.abspath(__file__))
_ap_dir = os.path.join(_cur_dir, "AryaPremium")
if _ap_dir not in sys.path:
    sys.path.insert(0, _ap_dir)

from AryaPremium.r2_helper import (
    upload_image_to_r2,
    upload_image_to_cdn,
    _get_r2_config,
    _inject_env_if_needed,
)

__all__ = [
    "upload_image_to_r2",
    "upload_image_to_cdn",
    "_get_r2_config",
    "_inject_env_if_needed",
]
