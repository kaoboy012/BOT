"""
🖼️ IMAGE CODE EXTRACTOR - OCR MODULE (RAPIDOCR / ONNXRUNTIME)
Trích xuất mã code từ hình ảnh bằng RapidOCR (ONNXRuntime, không cần cài
Tesseract-OCR ngoài — chỉ `pip install rapidocr onnxruntime`).

extract_code_from_image(image_path, lang, crop_box) trả về 1 chuỗi text
(các dòng nối bằng "\n"), main_script.py tách từng dòng ra để validate.
extract_frames_from_video(...) chỉ dùng OpenCV, không liên quan OCR.
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

from config import Config
from logger_setup import logger

try:
    import cv2

    _CV2_AVAILABLE = True
except ImportError:
    cv2 = None
    _CV2_AVAILABLE = False


VIDEO_FORMATS = [".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"]


# ============================================================
# OCR EXECUTOR RIÊNG — TÁCH TỪ DEFAULT EXECUTOR CỦA ASYNCIO
# ============================================================
# Dùng executor riêng cho OCR để không khiến event loop bị choke khi nhiều
# ảnh/video cùng xử lý, đặc biệt khi bot nhận tin từ nhiều channel đồng lúc.
_OCR_EXECUTOR = ThreadPoolExecutor(
    max_workers=max(1, int(getattr(Config, "MAX_CONCURRENT_OCR", 2))),
    thread_name_prefix="ocr-worker",
)
_OCR_EXECUTOR_STOPPED = False


def shutdown_ocr_executor(wait: bool = True, cancel_futures: bool = True) -> None:
    """Stop OCR workers once during application shutdown."""
    global _OCR_EXECUTOR_STOPPED
    if _OCR_EXECUTOR_STOPPED:
        return
    _OCR_EXECUTOR_STOPPED = True
    _OCR_EXECUTOR.shutdown(wait=wait, cancel_futures=cancel_futures)


# ============================================================
# RAPIDOCR ENGINE — SINGLETON (load model 1 lần, dùng lại mãi)
# ============================================================
_rapid_engine = None
_rapid_engine_lock = threading.Lock()
_OCR_SUPPORTS_NO_CLS = bool(getattr(Config, "OCR_SKIP_CLS", True))


def _disable_no_cls() -> None:
    global _OCR_SUPPORTS_NO_CLS
    _OCR_SUPPORTS_NO_CLS = False


def _get_rapid_engine():
    """Load model RapidOCR (ONNXRuntime) 1 lần duy nhất — dùng chung cho
    mọi lượt OCR sau đó. Load lần đầu mất khoảng 1-2s (đọc model ONNX từ
    đĩa), các lần sau tái sử dụng engine đã có sẵn trong RAM."""
    global _rapid_engine
    if _rapid_engine is None:
        with _rapid_engine_lock:
            if _rapid_engine is None:
                from rapidocr import RapidOCR

                # Mặc định RapidOCR dùng Det.limit_type="min", limit_side_len=736:
                # cạnh NGẮN của ảnh bị phóng lên >= 736px, nên khung 1024x576 bị
                # phóng thành ~1308x736 trước khi detect (tốn thêm ~20% thời gian).
                # Chuyển sang limit_type="max" để chỉ thu nhỏ khi ảnh quá lớn.
                # Đặt OCR_DET_LIMIT_SIDE=0 trong .env để dùng lại mặc định.
                det_side = int(getattr(Config, "OCR_DET_LIMIT_SIDE", 640) or 0)
                _rapid_engine = None
                if det_side > 0:
                    try:
                        _rapid_engine = RapidOCR(params={
                            "Det.limit_side_len": det_side,
                            "Det.limit_type": "max",
                        })
                        logger.info(
                            "⚙️ RapidOCR Det.limit_type=max limit_side_len=%s", det_side
                        )
                    except Exception as exc:
                        logger.warning(
                            "⚠️ RapidOCR không nhận params tối ưu (%s) — dùng mặc định", exc
                        )
                        _rapid_engine = None
                if _rapid_engine is None:
                    _rapid_engine = RapidOCR()
                logger.info("✅ RapidOCR engine đã load (ONNXRuntime, CPU)")
    return _rapid_engine


def _crop_pil_image(pil_img: Image.Image, crop_box) -> Image.Image:
    """Crop 1 ảnh PIL theo crop_box=(top,bottom,left,right) tỉ lệ 0.0-1.0.
    Trả về ảnh gốc nếu crop_box=None hoặc vùng crop không hợp lệ."""
    if crop_box is None:
        return pil_img
    w, h = pil_img.size
    top, bottom, left, right = crop_box
    box_px = (
        max(0, int(w * left)),
        max(0, int(h * top)),
        min(w, int(w * right)),
        min(h, int(h * bottom)),
    )
    if box_px[2] <= box_px[0] or box_px[3] <= box_px[1]:
        logger.warning(f"⚠️ [OCR] crop_box không hợp lệ {crop_box} → dùng toàn ảnh")
        return pil_img
    return pil_img.crop(box_px)


def _resize_for_ocr(pil_img: Image.Image) -> Image.Image:
    """Giảm ảnh quá lớn trước inference, vẫn giữ nguyên tỷ lệ khung hình.

    OCR thường chậm theo số pixel đầu vào. Giới hạn cạnh dài giúp ảnh chụp
    màn hình 2K/4K không làm RapidOCR mất nhiều giây, trong khi vùng code
    vẫn được phóng tối thiểu ở bước xử lý crop bên dưới.
    """
    max_side = max(320, int(getattr(Config, "OCR_MAX_IMAGE_SIDE", 1280)))
    width, height = pil_img.size
    current_side = max(width, height)
    if current_side <= max_side:
        return pil_img

    scale = max_side / current_side
    resized = pil_img.resize(
        (max(1, int(width * scale)), max(1, int(height * scale))),
        Image.Resampling.BILINEAR,
    )
    logger.debug(
        "🖼️ [OCR] resize preprocessing %sx%s → %sx%s",
        width,
        height,
        resized.width,
        resized.height,
    )
    return resized


def _crop_frame_bgr(frame, crop_box):
    """Crop 1 frame BGR (numpy array, dùng cho video) theo
    crop_box=(top,bottom,left,right) tỉ lệ 0-1. Trả về frame gốc nếu
    crop_box=None/không hợp lệ."""
    if crop_box is None:
        return frame
    h, w = frame.shape[:2]
    top, bottom, left, right = crop_box
    y0, y1 = max(0, int(h * top)), min(h, int(h * bottom))
    x0, x1 = max(0, int(w * left)), min(w, int(w * right))
    if y1 <= y0 or x1 <= x0:
        return frame
    return frame[y0:y1, x0:x1]


def _is_blank_frame(frame_bgr, std_threshold: float = 12.0) -> bool:
    """Kiểm tra nhanh xem vùng frame có gần như trống/mờ hay không."""
    try:
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY) if frame_bgr.ndim == 3 else frame_bgr
        small = cv2.resize(gray, (48, 48), interpolation=cv2.INTER_NEAREST)
        return float(np.std(small)) < std_threshold
    except Exception:
        return False


def extract_frames_from_video(
    video_path: str,
    out_dir: str,
    max_frames: int = 6,
    crop_box: tuple | None = None,
    frame_seconds: list | None = None,
) -> list:
    """
    Trích các frame rải đều (hoặc tại mốc giây chỉ định) trong video ra file
    .jpg để đưa vào pipeline OCR ảnh — giữ nguyên như bản gốc, chỉ dùng
    OpenCV, không liên quan tới việc đổi engine OCR nên không thay đổi hành
    vi hay hiệu năng phần này.
    """
    if not _CV2_AVAILABLE:
        logger.error(
            "❌ [Video-OCR] Chưa cài opencv-python. Chạy: "
            "pip install opencv-python --break-system-packages"
        )
        return []

    frame_paths = []
    cap = None
    try:
        video_path = Path(video_path)
        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            logger.warning(f"⚠️ [Video-OCR] Không mở được video: {video_path.name}")
            return []

        if frame_seconds:
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
            frame_count = float(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            duration = frame_count / fps if fps > 0 and frame_count > 0 else 0
            requested_seconds = list(frame_seconds)
            frame_seconds = [float(sec) for sec in requested_seconds
                             if float(sec) >= 0 and (duration <= 0 or float(sec) < duration)]
            if not frame_seconds:
                logger.info("⏭️ [Video-OCR] Bỏ mốc %s ngoài thời lượng %.2fs",
                            requested_seconds, duration)
                return []

            def _read_and_crop_at_sec(sec: float):
                if sec < 0 or (duration > 0 and sec >= duration):
                    return None
                cap.set(cv2.CAP_PROP_POS_MSEC, sec * 1000.0)
                ok, frame = cap.read()
                if not ok:
                    return None
                return _crop_frame_bgr(frame, crop_box)

            chosen_frames = []
            for sec in frame_seconds:
                cropped = _read_and_crop_at_sec(sec)
                if cropped is None:
                    logger.debug(f"⏭️ [Video-OCR] Không đọc được frame tại giây {sec}s")
                    continue

                if crop_box is not None and _is_blank_frame(cropped):
                    found = False
                    for offset in (0.3, 0.6, 1.0, 1.5):
                        for cand_sec in (sec + offset, sec - offset):
                            if cand_sec < 0:
                                continue
                            cand = _read_and_crop_at_sec(cand_sec)
                            if cand is not None and not _is_blank_frame(cand):
                                cropped = cand
                                found = True
                                break
                        if found:
                            break
                    if not found:
                        logger.debug(
                            f"⏭️ [Video-OCR] Frame tại giây {sec}s trống, thử mốc kế tiếp"
                        )
                        continue

                chosen_frames.append((sec, cropped))

            cap.release()

            if not chosen_frames:
                logger.warning(
                    f"⚠️ [Video-OCR] Không tìm được frame rõ ở bất kỳ mốc nào trong "
                    f"{list(frame_seconds)} — thử lại KHÔNG crop (full frame)"
                )
                if crop_box is not None:
                    return extract_frames_from_video(
                        str(video_path),
                        out_dir,
                        max_frames=max_frames,
                        crop_box=None,
                        frame_seconds=frame_seconds,
                    )
                return frame_paths

            for idx, (chosen_sec, chosen) in enumerate(chosen_frames[:max_frames]):
                frame_path = str(Path(out_dir) / f"vidframe_{idx:02d}.jpg")
                cv2.imwrite(frame_path, chosen, [cv2.IMWRITE_JPEG_QUALITY, 85])
                frame_paths.append(frame_path)

            logger.info(
                f"🎞️ [Video-OCR] Đã chụp {len(frame_paths)} frame tại các giây "
                f"{[sec for sec, _ in chosen_frames[:max_frames]]} từ {video_path.name}"
            )
            return frame_paths

        BLANK_SEARCH_RADIUS = 4

        def _read_and_crop_at(pos: int):
            cap.set(cv2.CAP_PROP_POS_FRAMES, pos)
            ok, frame = cap.read()
            if not ok:
                return None
            return _crop_frame_bgr(frame, crop_box)

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total_frames <= 0:
            logger.debug("⚠️ [Video-OCR] Không lấy được FRAME_COUNT, dùng chế độ đọc tuần tự")
            idx = 0
            saved = 0
            step = 5
            while saved < max_frames:
                ok, frame = cap.read()
                if not ok:
                    break
                if idx % step == 0:
                    cropped = _crop_frame_bgr(frame, crop_box)
                    if crop_box is not None and _is_blank_frame(cropped):
                        idx += 1
                        continue
                    frame_path = str(Path(out_dir) / f"vidframe_{saved:02d}.jpg")
                    cv2.imwrite(frame_path, cropped, [cv2.IMWRITE_JPEG_QUALITY, 85])
                    frame_paths.append(frame_path)
                    saved += 1
                idx += 1
        else:
            start = int(total_frames * 0.05)
            end = int(total_frames * 0.95)
            end = max(end, start + 1)
            step = max((end - start) // max_frames, 1)
            positions = list(range(start, end, step))[:max_frames]

            for i, pos in enumerate(positions):
                cropped = _read_and_crop_at(pos)
                if cropped is None:
                    continue

                if crop_box is not None and _is_blank_frame(cropped):
                    found = False
                    for offset in range(1, BLANK_SEARCH_RADIUS + 1):
                        for cand_pos in (pos + offset, pos - offset):
                            if cand_pos < start or cand_pos >= end:
                                continue
                            cand = _read_and_crop_at(cand_pos)
                            if cand is not None and not _is_blank_frame(cand):
                                cropped = cand
                                found = True
                                break
                        if found:
                            break
                    if not found:
                        logger.debug(f"⏭️ [Video-OCR] Frame #{i} (pos={pos}) trống, không tìm được frame lân cận thay thế")
                        continue

                frame_path = str(Path(out_dir) / f"vidframe_{i:02d}.jpg")
                cv2.imwrite(frame_path, cropped)
                frame_paths.append(frame_path)

        cap.release()

        if crop_box is not None and not frame_paths:
            logger.warning(
                "⚠️ [Video-OCR] Không trích được frame nào với crop_box hiện tại "
                "(nghi vùng crop sai) — thử lại KHÔNG crop (full frame)"
            )
            return extract_frames_from_video(
                str(video_path),
                out_dir,
                max_frames=max_frames,
                crop_box=None,
            )

        crop_note = " (đã crop)" if crop_box is not None else ""
        logger.info(f"🎞️ [Video-OCR] Trích {len(frame_paths)} frame từ {video_path.name}{crop_note}")
        return frame_paths

    except Exception as e:
        logger.error(f"❌ [Video-OCR] Lỗi trích frame: {e}")
        return frame_paths
    finally:
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass


class ImageCodeExtractor:
    """Trích xuất code từ hình ảnh — dùng RapidOCR (ONNXRuntime), 1 lần
    inference/ảnh, không cần nhiều biến thể tiền xử lý như bản Tesseract cũ."""

    def __init__(self):
        self.supported_formats = [".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tiff", ".webp"]
        try:
            _get_rapid_engine()
            logger.info("✅ ImageCodeExtractor ready (RapidOCR/ONNXRuntime)")
        except Exception as e:
            logger.error(
                f"❌ Không load được RapidOCR! ({e})\n"
                f"   Chạy: pip install rapidocr onnxruntime --break-system-packages"
            )
            raise

    def _run_rapid_ocr(self, pil_img: Image.Image) -> str:
        """Chạy 1 lượt inference RapidOCR trên ảnh PIL (RGB), trả về các
        dòng text nhận diện được, nối bằng '\n' — giữ đúng định dạng mà
        main_script.py đang parse (split theo dòng)."""
        try:
            engine = _get_rapid_engine()
            started = time.perf_counter()
            arr = np.array(pil_img)
            result = None
            if _OCR_SUPPORTS_NO_CLS:
                try:
                    # Mã nằm ngang, không cần bước nhận diện hướng chữ (cls).
                    result = engine(arr, use_cls=False)
                except TypeError:
                    _disable_no_cls()
            if result is None:
                result = engine(arr)
            if bool(getattr(Config, "OCR_DEBUG_TIMING", False)):
                logger.info(
                    "⏱️ [OCR-INFER] %.1fms input=%sx%s",
                    (time.perf_counter() - started) * 1000.0,
                    pil_img.width,
                    pil_img.height,
                )
            txts = list(getattr(result, "txts", None) or [])
            scores = list(getattr(result, "scores", None) or [])
            threshold = float(getattr(Config, "OCR_CONFIDENCE_THRESHOLD", 0.70))
            kept = []
            for idx, text in enumerate(txts):
                if not text or not str(text).strip():
                    continue
                # RapidOCR normally returns one score per text box. If a
                # backend/version omits scores, keep the text and let the
                # stricter XX88 candidate validator decide.
                if scores and idx < len(scores):
                    try:
                        if float(scores[idx]) < threshold:
                            continue
                    except (TypeError, ValueError):
                        pass
                kept.append(str(text).strip())
            return "\n".join(kept)
        except Exception as e:
            logger.debug(f"⚠️ [OCR] RapidOCR inference lỗi: {e}")
            return ""

    def extract_code_from_image(
        self,
        image_path: str,
        lang: str = "eng",
        crop_box: tuple | None = None,
    ) -> str:
        """
        Trích xuất code từ ảnh bằng RapidOCR.

        Args:
            image_path: Đường dẫn file ảnh
            lang: giữ tham số để tương thích chữ ký cũ
            crop_box: (top, bottom, left, right) tính theo tỉ lệ 0.0–1.0

        Returns:
            Text đã OCR, mỗi dòng nhận diện cách nhau bằng "\\n" (chuỗi rỗng
            nếu không có).
        """
        base_img = None
        img_for_ocr = None
        try:
            image_path = Path(image_path)

            if not image_path.exists():
                logger.warning(f"⚠️ File not found: {image_path}")
                return ""

            if image_path.suffix.lower() not in self.supported_formats:
                logger.warning(f"⚠️ Format not supported: {image_path.suffix}")
                return ""

            logger.info(f"📸 OCR image: {image_path.name}")

            with Image.open(str(image_path)) as source_img:
                base_img = _resize_for_ocr(source_img.convert("RGB"))

            cropped = crop_box is not None
            img_for_ocr = _crop_pil_image(base_img, crop_box) if cropped else base_img

            # Vùng crop quá nhỏ → phóng to nhẹ để model detect tốt hơn
            min_width = max(160, int(getattr(Config, "OCR_MIN_CROP_WIDTH", 300)))
            if img_for_ocr.width < min_width:
                scale = min_width / img_for_ocr.width
                resized_img = img_for_ocr.resize(
                    (int(img_for_ocr.width * scale), int(img_for_ocr.height * scale)),
                    Image.Resampling.BILINEAR,
                )
                if img_for_ocr is not base_img:
                    img_for_ocr.close()
                img_for_ocr = resized_img

            cleaned = self._clean_text(self._run_rapid_ocr(img_for_ocr))

            # Fallback toàn ảnh là inference thứ hai và làm tăng latency.
            # Chỉ bật chủ động cho các layout cần thiết qua config.
            if (
                cropped
                and len(cleaned) < 6
                and bool(getattr(Config, "OCR_CROP_FALLBACK", False))
            ):
                logger.info(
                    f"↩️ [OCR] Crop cho quá ít text ({len(cleaned)} ký tự) — "
                    f"fallback OCR toàn ảnh"
                )
                full_cleaned = self._clean_text(self._run_rapid_ocr(base_img))
                if len(full_cleaned) > len(cleaned):
                    cleaned = full_cleaned

            if cleaned:
                logger.info(f"✅ Extracted: {len(cleaned)} chars")
            else:
                logger.warning("⚠️ No text detected in image")

            return cleaned

        except Exception as e:
            logger.error(f"❌ OCR error: {e}")
            return ""
        finally:
            if img_for_ocr is not None and img_for_ocr is not base_img:
                try:
                    img_for_ocr.close()
                except Exception:
                    pass
            if base_img is not None:
                try:
                    base_img.close()
                except Exception:
                    pass

    async def extract_first_valid_code(
        self,
        frame_paths: list,
        crop_box: tuple | None = None,
        is_valid_fn=None,
        already_cropped: bool = False,
    ) -> tuple:
        """OCR lần lượt từng frame, dừng sớm khi 1 frame đạt ngưỡng hợp lệ."""
        if not frame_paths:
            return "", ""

        check = is_valid_fn or (lambda t: len((t or "").replace("\n", "")) >= 6)
        effective_crop = None if already_cropped else crop_box
        loop = asyncio.get_running_loop()

        for idx, frame_path in enumerate(frame_paths):
            text = await loop.run_in_executor(
                _OCR_EXECUTOR,
                self.extract_code_from_image,
                frame_path,
                "eng",
                effective_crop,
            )
            if text and check(text):
                skipped = len(frame_paths) - idx - 1
                if skipped > 0:
                    logger.info(
                        f"⏭️ [OCR-EarlyExit] Có kết quả hợp lệ ở frame "
                        f"{idx + 1}/{len(frame_paths)} ('{Path(frame_path).name}') "
                        f"→ dừng sớm, bỏ qua {skipped} frame còn lại"
                    )
                return frame_path, text

        return "", ""

    def _clean_text(self, text: str) -> str:
        """Làm sạch text: xóa dòng trống, khoảng trắng thừa"""
        if not text:
            return ""
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        return "\n".join(lines)


# Global instance
_image_extractor = None


def init_image_extractor() -> ImageCodeExtractor:
    """Khởi tạo image extractor"""
    global _image_extractor
    if _image_extractor is None:
        try:
            _image_extractor = ImageCodeExtractor()
        except Exception as e:
            logger.error(f"❌ Cannot init image extractor: {e}")
            return None
    return _image_extractor


def get_image_extractor() -> ImageCodeExtractor:
    """Lấy image extractor instance"""
    global _image_extractor
    if _image_extractor is None:
        _image_extractor = init_image_extractor()
    return _image_extractor


def warmup_image_extractor() -> bool:
    """Load RapidOCR and execute one tiny inference before Telegram intake."""
    try:
        extractor = get_image_extractor()
        if extractor is None:
            return False
        warmup = Image.new("RGB", (64, 64), "white")
        try:
            extractor._run_rapid_ocr(warmup)
        finally:
            warmup.close()
        return True
    except Exception as exc:
        logger.warning("⚠️ OCR warm-up inference thất bại: %s", exc)
        return False
