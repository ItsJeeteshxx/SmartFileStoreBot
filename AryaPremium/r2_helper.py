import os
import io
import uuid
import asyncio
import logging
from PIL import Image

logger = logging.getLogger(__name__)

def _get_r2_config():
    r2_account_id = os.environ.get("R2_ACCOUNT_ID") or "d738aa13a7944050a7edb60cc5cd91bb"
    r2_access_key = os.environ.get("R2_ACCESS_KEY_ID") or os.environ.get("R2_ACCESS_KEY") or ""
    r2_secret_key = os.environ.get("R2_SECRET_ACCESS_KEY") or os.environ.get("R2_SECRET_KEY") or ""
    r2_bucket = os.environ.get("R2_BUCKET_NAME") or os.environ.get("R2_BUCKET") or "arya-images"
    r2_domain = os.environ.get("R2_CUSTOM_DOMAIN") or os.environ.get("R2_DOMAIN") or "https://pub-d738aa13a7944050a7edb60cc5cd91bb.r2.dev"
    return r2_account_id, r2_access_key, r2_secret_key, r2_bucket, r2_domain


async def upload_image_to_r2(img_bytes: bytes, width: int = None, height: int = None, format: str = "WEBP", quality: int = 80) -> str:
    """Optimizes image bytes to WebP and uploads to Cloudflare R2, returning permanent URL."""
    if not img_bytes:
        return ""

    r2_account_id, r2_access_key, r2_secret_key, r2_bucket, r2_domain = _get_r2_config()
    if not (r2_account_id and r2_access_key and r2_secret_key and r2_bucket):
        logger.warning("Cloudflare R2 credentials missing in environment.")
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
        img.save(output, format=format, quality=quality)
        return output.getvalue()

    try:
        processed_bytes = await asyncio.to_thread(process_data, img_bytes)
    except Exception as e:
        logger.error(f"Image optimization error: {e}")
        return ""

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
            ext = format.lower()
            content_type = f"image/{ext}"
            filename = f"{uuid.uuid4().hex}.{ext}"
            s3.put_object(
                Bucket=r2_bucket,
                Key=filename,
                Body=processed_bytes,
                ContentType=content_type
            )
            if r2_domain:
                domain = r2_domain.strip("/")
                if not domain.startswith("http"):
                    domain = "https://" + domain
                return f"{domain}/{filename}"
            else:
                return f"https://pub-{r2_account_id[:32]}.r2.dev/{filename}"
        except Exception as e:
            logger.error(f"Cloudflare R2 upload error: {e}")
            return ""

    return await asyncio.to_thread(upload_r2)
