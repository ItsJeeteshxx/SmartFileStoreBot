import os
import io
import re
import sys
import uuid
import asyncio
import logging
from PIL import Image

logger = logging.getLogger("AryaR2Helper")

curr_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(curr_dir)
if curr_dir not in sys.path:
    sys.path.insert(0, curr_dir)
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)


def _inject_env_if_needed():
    """Ensures R2 credentials from any .env or config.env are active."""
    for p in [
        os.path.join(curr_dir, ".env"),
        os.path.join(parent_dir, ".env"),
        os.path.join(curr_dir, "config.env"),
        os.path.join(parent_dir, "config.env"),
        os.path.join(os.getcwd(), ".env")
    ]:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            k = k.strip()
                            v = v.strip().strip("'").strip('"')
                            if v and (not os.environ.get(k) or not os.environ[k].strip()):
                                os.environ[k] = v
            except Exception:
                pass

_inject_env_if_needed()


def _get_r2_config():
    _inject_env_if_needed()
    
    cfg_obj = None
    try:
        from AryaPremium.config import Config as cfg_obj
    except Exception:
        try:
            from config import Config as cfg_obj
        except Exception:
            pass

    r2_account_id = os.environ.get("R2_ACCOUNT_ID") or getattr(cfg_obj, "R2_ACCOUNT_ID", None) or "d738aa13a7944050a7edb60cc5cd91bb"
    r2_access_key = os.environ.get("R2_ACCESS_KEY_ID") or os.environ.get("R2_ACCESS_KEY") or getattr(cfg_obj, "R2_ACCESS_KEY_ID", None) or getattr(cfg_obj, "R2_ACCESS_KEY", None) or ""
    r2_secret_key = os.environ.get("R2_SECRET_ACCESS_KEY") or os.environ.get("R2_SECRET_KEY") or getattr(cfg_obj, "R2_SECRET_ACCESS_KEY", None) or getattr(cfg_obj, "R2_SECRET_KEY", None) or ""
    r2_bucket = os.environ.get("R2_BUCKET_NAME") or os.environ.get("R2_BUCKET") or getattr(cfg_obj, "R2_BUCKET_NAME", None) or getattr(cfg_obj, "R2_BUCKET", None) or "arya-images"
    r2_domain = os.environ.get("R2_CUSTOM_DOMAIN") or os.environ.get("R2_DOMAIN") or getattr(cfg_obj, "R2_CUSTOM_DOMAIN", None) or getattr(cfg_obj, "R2_DOMAIN", None) or "https://pub-d738aa13a7944050a7edb60cc5cd91bb.r2.dev"
    
    return r2_account_id, r2_access_key, r2_secret_key, r2_bucket, r2_domain


async def _fallback_catbox(raw_bytes: bytes, filename: str = "banner.webp") -> str:
    """Helper to upload to Catbox CDN if Cloudflare R2 is unavailable or fails."""
    try:
        try:
            from AryaPremium.utils import upload_to_catbox
        except ImportError:
            from utils import upload_to_catbox
        return await upload_to_catbox(raw_bytes, filename=filename) or ""
    except Exception as e:
        logger.debug(f"[R2] Catbox fallback error: {e}")
        return ""


async def upload_image_to_r2(
    img_input,
    width: int = None,
    height: int = None,
    format: str = "WEBP",
    quality: int = 80,
    clean_title: str = None,
    **kwargs
) -> str:
    """
    Optimizes image (accepts bytes, filepath str, PathLike, or BytesIO) to WebP and uploads to Cloudflare R2.
    If R2 credentials are not set or R2 upload fails, automatically falls back to Catbox CDN.
    Returns permanent public CDN URL on success, or empty string on failure.
    """
    if not img_input:
        return ""

    raw_bytes = None
    if isinstance(img_input, (bytes, bytearray)):
        raw_bytes = bytes(img_input)
    elif isinstance(img_input, io.BytesIO):
        raw_bytes = img_input.getvalue()
    elif isinstance(img_input, (str, os.PathLike)):
        p_str = str(img_input)
        if os.path.exists(p_str):
            try:
                with open(p_str, "rb") as f:
                    raw_bytes = f.read()
            except Exception as e:
                logger.error(f"[R2] Failed reading file '{p_str}': {e}")
                return ""
        else:
            logger.error(f"[R2] File not found '{p_str}'")
            return ""

    if not raw_bytes:
        return ""

    def process_data(data):
        img = Image.open(io.BytesIO(data))
        if img.mode == "CMYK":
            img = img.convert("RGB")
        elif img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")

        if width and height:
            img = img.resize((width, height), Image.Resampling.LANCZOS)
        elif width:
            img.thumbnail((width, width))

        output = io.BytesIO()
        img.save(output, format=format.upper(), quality=quality, optimize=True)
        return output.getvalue()

    try:
        processed_bytes = await asyncio.to_thread(process_data, raw_bytes)
    except Exception as e:
        logger.error(f"[R2] Image optimization error: {e}")
        processed_bytes = raw_bytes

    r2_account_id, r2_access_key, r2_secret_key, r2_bucket, r2_domain = _get_r2_config()
    ext = format.lower()
    fn_name = f"{uuid.uuid4().hex}.{ext}"

    # If Cloudflare R2 credentials are present, attempt R2 upload first
    if r2_account_id and r2_access_key and r2_secret_key and r2_bucket:
        import boto3
        def upload_r2():
            try:
                s3 = boto3.client(
                    "s3",
                    endpoint_url=f"https://{r2_account_id}.r2.cloudflarestorage.com",
                    aws_access_key_id=r2_access_key,
                    aws_secret_access_key=r2_secret_key,
                    region_name="auto"
                )
                content_type = f"image/{ext}"
                s3.put_object(
                    Bucket=r2_bucket,
                    Key=fn_name,
                    Body=processed_bytes,
                    ContentType=content_type
                )
                
                if r2_domain:
                    domain = r2_domain.strip("/")
                    if not domain.startswith("http"):
                        domain = "https://" + domain
                    res_url = f"{domain}/{fn_name}"
                else:
                    res_url = f"https://pub-{r2_account_id[:32]}.r2.dev/{fn_name}"

                logger.info(f"[R2] Successfully uploaded to Cloudflare R2 ➔ {res_url}")
                return res_url
            except Exception as e:
                logger.error(f"[R2] Cloudflare R2 put_object error: {e}")
                return ""

        r2_result = await asyncio.to_thread(upload_r2)
        if r2_result:
            return r2_result

    # Fallback to Catbox CDN so the banner is NEVER left without a public CDN URL!
    logger.info(f"[R2] Falling back to Catbox CDN for image upload...")
    catbox_result = await _fallback_catbox(processed_bytes, filename=fn_name)
    if catbox_result:
        logger.info(f"[R2] Uploaded image to Catbox CDN ➔ {catbox_result}")
        return catbox_result

    return ""


# Alias for backward compatibility
upload_image_to_cdn = upload_image_to_r2
