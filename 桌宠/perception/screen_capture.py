import io
import base64
from typing import Optional, Tuple, Dict, Any
from PIL import Image
import mss

def capture_screen_jpeg(
    max_width: int = 1440,
    quality: int = 82,
    crop_rect: Optional[Dict[str, int]] = None,
    *,
    strict_crop: bool = False
) -> Optional[Tuple[bytes, str]]:
    """
    Capture the current desktop screen (or the target window area) and compress it to JPEG.
    Higher resolution (1440px) and quality (82) preserve video subtitles and bullet chat (弹幕).
    Returns: (jpeg_bytes, base64_str) or None on failure.
    """
    try:
        with mss.mss() as sct:
            monitor = sct.monitors[0 if strict_crop else 1]

            # 严格目标区域使用虚拟桌面边界，不可见时拒绝截图，避免退回整屏。
            if strict_crop and (
                not crop_rect or crop_rect.get("width", 0) <= 200 or crop_rect.get("height", 0) <= 200
            ):
                return None

            # If crop_rect provided and valid, crop to the active video/window
            if crop_rect and crop_rect.get("width", 0) > 200 and crop_rect.get("height", 0) > 200:
                left = max(monitor["left"], crop_rect["left"])
                top = max(monitor["top"], crop_rect["top"])
                right = min(monitor["left"] + monitor["width"], crop_rect["left"] + crop_rect["width"])
                bottom = min(monitor["top"] + monitor["height"], crop_rect["top"] + crop_rect["height"])

                w = right - left
                h = bottom - top
                if w > 100 and h > 100:
                    capture_box = {"left": left, "top": top, "width": w, "height": h}
                    sct_img = sct.grab(capture_box)
                else:
                    if strict_crop:
                        return None
                    sct_img = sct.grab(monitor)
            else:
                sct_img = sct.grab(monitor)

            img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")

            # Downscale if wider than max_width to control latency
            if img.width > max_width:
                scale = max_width / float(img.width)
                new_size = (max_width, int(img.height * scale))
                img = img.resize(new_size, Image.Resampling.BILINEAR)

            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            jpeg_bytes = buf.getvalue()
            b64_str = base64.b64encode(jpeg_bytes).decode("utf-8")
            return jpeg_bytes, b64_str
    except Exception as e:
        print(f"[ScreenCapture] Error capturing screen: {e}", flush=True)
        return None
