import io
import base64
import logging
import asyncio
import aiohttp
from PIL import Image, ImageFilter
from AryaPremium.database import db as arya_db

logger = logging.getLogger(__name__)

async def process_outpaint(image_bytes: bytes, title_position: str = "left") -> bytes:
    """
    Main outpainting pipeline.
    1. Loads configuration from database.
    2. Decides if image needs outpainting (aspect ratio < 1.4).
    3. Runs outpainting via Replicate, Fal.ai, or Stability AI with failover.
    4. Ensures final image is exactly 1184x556 px.
    5. Falls back to beautiful blurred-edge padding if AI is disabled or fails.
    """
    # 1. Determine if outpainting is configured
    try:
        cfg = await arya_db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
    except Exception as e:
        logger.error(f"Error fetching outpaint config: {e}")
        cfg = {}

    outpaint_enabled = cfg.get("outpaint_enabled", False)
    preferred_provider = cfg.get("outpaint_provider", "replicate").lower()
    replicate_key = cfg.get("replicate_api_key", "").strip()
    fal_key = cfg.get("fal_api_key", "").strip()
    stability_key = cfg.get("stability_api_key", "").strip()

    # Determine input aspect ratio
    try:
        img = Image.open(io.BytesIO(image_bytes))
        orig_w, orig_h = img.size
        aspect_ratio = orig_w / orig_h
    except Exception as e:
        logger.error(f"Failed to parse input image in outpainter: {e}")
        return image_bytes

    # If it is already horizontal, just crop/resize to 1184x556
    if aspect_ratio >= 1.4:
        logger.info(f"Image is already horizontal (aspect ratio {aspect_ratio:.2f}). Resizing/cropping to 1184x556 directly.")
        return crop_to_banner(image_bytes)

    # If outpainting is disabled or no keys are configured, use blurred fallback
    has_keys = bool(replicate_key or fal_key or stability_key)
    if not outpaint_enabled or not has_keys:
        logger.info("AI outpainting is disabled or no keys are set. Using premium blurred side padding.")
        return generate_blurred_fallback(image_bytes, title_position=title_position)

    # Prepare standard square to fit center/side (556x556)
    try:
        square_io = io.BytesIO()
        img_resized = img.resize((556, 556), Image.Resampling.LANCZOS)
        if img_resized.mode in ("RGBA", "P"):
            img_resized = img_resized.convert("RGB")
        img_resized.save(square_io, format="JPEG", quality=95)
        square_bytes = square_io.getvalue()
    except Exception as e:
        logger.error(f"Error preparing square image for outpaint: {e}")
        return generate_blurred_fallback(image_bytes, title_position=title_position)

    # Failover sequence based on user settings
    providers = []
    if preferred_provider == "replicate":
        providers = [("replicate", replicate_key), ("fal", fal_key), ("stability", stability_key)]
    elif preferred_provider == "fal":
        providers = [("fal", fal_key), ("replicate", replicate_key), ("stability", stability_key)]
    elif preferred_provider == "stability":
        providers = [("stability", stability_key), ("replicate", replicate_key), ("fal", fal_key)]
    else:
        providers = [("replicate", replicate_key), ("fal", fal_key), ("stability", stability_key)]

    # Filter out providers without active API keys
    active_providers = [p for p in providers if p[1]]
    if not active_providers:
        logger.info("No active API keys found for outpainting. Using blurred fallback.")
        return generate_blurred_fallback(image_bytes, title_position=title_position)

    # Attempt outpainting with sequential failover
    for provider_name, api_key in active_providers:
        logger.info(f"Attempting outpainting with provider: {provider_name} (Layout alignment: {title_position})")
        try:
            outpainted_bytes = None
            if provider_name == "replicate":
                outpainted_bytes = await call_replicate_outpaint(square_bytes, api_key, title_position)
            elif provider_name == "fal":
                outpainted_bytes = await call_fal_outpaint(square_bytes, api_key, title_position)
            elif provider_name == "stability":
                outpainted_bytes = await call_stability_outpaint(square_bytes, api_key, title_position)

            if outpainted_bytes:
                logger.info(f"Outpainting succeeded using {provider_name}!")
                # Ensure the final output is exactly 1184x556 px
                return crop_to_banner(outpainted_bytes)
        except Exception as ex:
            logger.warning(f"Outpainting failed on provider {provider_name}: {ex}. Trying next available provider...")

    # If all configured AI models fail, fall back gracefully to premium blurred padding
    logger.error("All outpainting APIs failed. Returning premium blurred fallback.")
    return generate_blurred_fallback(image_bytes, title_position=title_position)


async def call_replicate_outpaint(square_bytes: bytes, api_key: str, title_position: str = "left") -> bytes:
    """Call Replicate SDXL Outpaint model (logerfo/sdxl-outpaint) with layout alignment control"""
    b64 = base64.b64encode(square_bytes).decode("utf-8")
    uri = f"data:image/jpeg;base64,{b64}"

    # Asymmetric canvas extension based on title position
    left_width = 314
    right_width = 314
    if title_position == "left":
        left_width = 0
        right_width = 628
    elif title_position == "right":
        left_width = 628
        right_width = 0

    payload = {
        "version": "209af00e28d447470659ee02f8319f37c768910b80e45c47fa18fb41f173167b",
        "input": {
            "image": uri,
            "prompt": "cinematic detailed scenery backdrop matching original art, seamless transition, high quality, 8k",
            "left_width": left_width,
            "right_width": right_width,
            "top_height": 0,
            "bottom_height": 0
        }
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    async with aiohttp.ClientSession() as session:
        async with session.post("https://api.replicate.com/v1/predictions", json=payload, headers=headers, timeout=20) as r:
            if r.status == 402:
                raise Exception("Billing issue or payment required on Replicate")
            if r.status not in (200, 201):
                err = await r.text()
                raise Exception(f"Replicate API returned status {r.status}: {err[:150]}")
            pred = await r.json()
            poll_url = pred["urls"]["get"]

        # Poll the prediction result
        for _ in range(30):
            await asyncio.sleep(2)
            async with session.get(poll_url, headers=headers) as r2:
                if r2.status == 200:
                    data = await r2.json()
                    status = data.get("status")
                    if status == "succeeded":
                        out_url = data.get("output")
                        if isinstance(out_url, list):
                            out_url = out_url[0]
                        async with session.get(out_url) as r3:
                            if r3.status == 200:
                                return await r3.read()
                            raise Exception("Failed to download generated image from Replicate storage")
                    elif status in ("failed", "canceled"):
                        raise Exception(f"Replicate prediction ended with status: {status}")
        raise Exception("Replicate prediction timed out after 60 seconds")


async def call_fal_outpaint(square_bytes: bytes, api_key: str, title_position: str = "left") -> bytes:
    """Call Fal.ai fooocus inpaint/outpaint model synchronously with layout alignment control"""
    b64 = base64.b64encode(square_bytes).decode("utf-8")
    uri = f"data:image/jpeg;base64,{b64}"

    # Asymmetric canvas selections based on title position
    selections = ["Left", "Right"]
    if title_position == "left":
        selections = ["Right"]
    elif title_position == "right":
        selections = ["Left"]

    payload = {
        "inpaint_image_url": uri,
        "prompt": "cinematic horizontal background, seamless transition, highly detailed poster art backdrop, match colors",
        "inpaint_mode": "Inpaint or Outpaint",
        "outpaint_selections": selections,
        "guidance_scale": 7.5
    }
    headers = {
        "Authorization": f"Key {api_key}",
        "Content-Type": "application/json"
    }

    async with aiohttp.ClientSession() as session:
        async with session.post("https://fal.run/fal-ai/fooocus/inpaint", json=payload, headers=headers, timeout=30) as r:
            if r.status != 200:
                err = await r.text()
                raise Exception(f"Fal.ai API returned status {r.status}: {err[:150]}")
            res = await r.json()
            out_img = res.get("image", {})
            out_url = out_img.get("url")
            if not out_url:
                raise Exception("Fal.ai response did not contain output image URL")
            
            async with session.get(out_url) as r2:
                if r2.status == 200:
                    return await r2.read()
                raise Exception("Failed to download generated image from Fal.ai storage")


async def call_stability_outpaint(square_bytes: bytes, api_key: str, title_position: str = "left") -> bytes:
    """Call Stability AI Stable Image Edit Uncrop/Outpaint API with layout alignment control"""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json"
    }

    # Asymmetric canvas dimensions based on title position
    left = 314
    right = 314
    if title_position == "left":
        left = 0
        right = 628
    elif title_position == "right":
        left = 628
        right = 0

    data = aiohttp.FormData()
    data.add_field("image", square_bytes, filename="square.jpg", content_type="image/jpeg")
    data.add_field("left", str(left))
    data.add_field("right", str(right))
    data.add_field("up", "0")
    data.add_field("down", "0")
    data.add_field("prompt", "cinematic detailed scenery backdrop matching original art, seamless transition, high quality, 8k")
    data.add_field("output_format", "webp")

    async with aiohttp.ClientSession() as session:
        async with session.post("https://api.stability.ai/v2beta/stable-image/edit/outpaint", data=data, headers=headers, timeout=25) as r:
            if r.status != 200:
                err = await r.text()
                raise Exception(f"Stability AI API returned status {r.status}: {err[:150]}")
            res = await r.json()
            b64_out = res.get("image")
            if not b64_out:
                raise Exception("Stability AI response did not contain base64 image data")
            return base64.b64decode(b64_out)


def crop_to_banner(image_bytes: bytes, target_width: int = 1184, target_height: int = 556) -> bytes:
    """
    Resizes and crops an image to exactly target_width x target_height.
    Maintains center focus.
    """
    img = Image.open(io.BytesIO(image_bytes))
    orig_w, orig_h = img.size
    
    # Target aspect ratio
    target_aspect = target_width / target_height
    orig_aspect = orig_w / orig_h
    
    if orig_aspect > target_aspect:
        # Image is wider than target aspect ratio - crop sides
        new_h = target_height
        new_w = int(target_height * orig_aspect)
        img_resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        left = (new_w - target_width) // 2
        img_final = img_resized.crop((left, 0, left + target_width, target_height))
    else:
        # Image is taller than target aspect ratio - crop top/bottom
        new_w = target_width
        new_h = int(target_width / orig_aspect)
        img_resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        top = (new_h - target_height) // 2
        img_final = img_resized.crop((0, top, target_width, top + target_height))

    output = io.BytesIO()
    if img_final.mode in ("RGBA", "P"):
        img_final = img_final.convert("RGB")
    img_final.save(output, format="JPEG", quality=85, optimize=True)
    return output.getvalue()


def generate_blurred_fallback(image_bytes: bytes, target_width: int = 1184, target_height: int = 556, title_position: str = "left") -> bytes:
    """
    Premium fallback: centers or offsets the original cover over a heavily-blurred widescreen stretch
    of itself based on layout alignment preferences. Zero third-party dependency, fast, and extremely neat.
    """
    try:
        orig_img = Image.open(io.BytesIO(image_bytes))
        
        # 1. Create a heavily blurred background scaled to target size
        bg = orig_img.resize((target_width, target_height), Image.Resampling.BOX)
        bg = bg.filter(ImageFilter.GaussianBlur(radius=40))
        
        # Add a subtle dark vignette overlay to make text highly readable
        vignette = Image.new("RGBA", (target_width, target_height), (0, 0, 0, 0))
        # Draw soft gradient overlay manually or simply paste a dark translucent layer
        overlay = Image.new("RGBA", (target_width, target_height), (0, 0, 0, 110)) # translucent dark
        bg = Image.alpha_composite(bg.convert("RGBA"), overlay)
        
        # 2. Fit the original image as a centered or side-offset square card
        # Center square height is target_height, so its size is target_height x target_height
        card_size = target_height
        card = orig_img.resize((card_size, card_size), Image.Resampling.LANCZOS)
        
        # Determine offset_x dynamically based on alignment
        if title_position == "left":
            offset_x = 0
        elif title_position == "right":
            offset_x = target_width - card_size
        else:
            offset_x = (target_width - card_size) // 2
        offset_y = 0
        
        # Paste card on the blurred background
        bg.paste(card, (offset_x, offset_y))
        
        # Save output
        output = io.BytesIO()
        final_rgb = bg.convert("RGB")
        final_rgb.save(output, format="JPEG", quality=85, optimize=True)
        return output.getvalue()
    except Exception as e:
        logger.error(f"Error generating premium blurred fallback: {e}")
        return image_bytes
