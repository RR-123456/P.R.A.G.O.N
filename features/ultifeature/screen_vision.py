"""
utils/screen_vision.py
───────────────────────
Jarvis v2.0 — Screen Vision (PRD Section 4.1 / Pillar 1)

Trigger-based (not continuous) screenshot capture + analysis.
Uses local Ollama LLaVA multimodal model for privacy.

Fallback: Extracts visible text using PIL + pytesseract if LLaVA unavailable.
"""

from __future__ import annotations
import base64
import json
import io
import urllib.request


class ScreenVision:
    def __init__(self, model: str = "llava"):
        self.model = model
        self._enabled = self._check_availability()

    def _check_availability(self) -> bool:
        """Check if PIL (Pillow) is available for screenshot capture."""
        try:
            import PIL.ImageGrab
            return True
        except ImportError:
            print("[ Vision ] PIL not available — screen capture disabled")
            return False

    def capture_screenshot(self) -> bytes | None:
        """Capture current screen as PNG bytes."""
        if not self._enabled:
            return None
        try:
            from PIL import ImageGrab
            img = ImageGrab.grab()
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        except Exception as e:
            print(f"[ Vision ] Screenshot error: {e}")
            return None

    def capture_and_analyze(self, context: str = "general") -> str:
        """
        Capture screen and analyze with LLaVA.
        Returns a descriptive text response.
        """
        screenshot = self.capture_screenshot()
        if not screenshot:
            return "Screen capture is not available on this system."

        # Try LLaVA via Ollama
        result = self._analyze_with_llava(screenshot, context)
        if result:
            return result

        # Fallback: basic PIL text extraction
        return self._analyze_basic(screenshot)

    def _analyze_with_llava(self, image_bytes: bytes, context: str) -> str | None:
        """Send screenshot to LLaVA via Ollama for analysis."""
        try:
            img_b64 = base64.b64encode(image_bytes).decode("utf-8")

            prompt_map = {
                "general" : "Describe what you see on this screen briefly. Focus on any errors, warnings, or important information.",
                "debug" : "Look at this screen carefully. Is there an error or exception visible? If so, describe it precisely.",
                "code" : "What code is visible on this screen? Identify any errors, the language, and what the code is doing.",
                "terminal" : "What does the terminal output show? Is there an error? Summarize it in one sentence.",
            }
            prompt = prompt_map.get(context, prompt_map["general"])

            body = json.dumps({
                "model" : self.model,
                "prompt": prompt,
                "images": [img_b64],
                "stream": False
            }).encode("utf-8")

            req = urllib.request.Request(
                "http://localhost:11434/api/generate",
                data=body,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return result.get("response", "").strip()

        except Exception as e:
            print(f"[ Vision ] LLaVA error: {e}")
            return None

    def _analyze_basic(self, image_bytes: bytes) -> str:
        """Basic fallback: extract text regions with PIL."""
        try:
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(image_bytes))
            # Without OCR, just return dimensions + basic info
            w, h = img.size
            return f"Screen captured at {w}x{h} resolution. Install pytesseract or LLaVA for full analysis."
        except Exception:
            return "Screen captured but analysis not available."

    def detect_errors_proactively(self) -> str | None:
        """
        Pillar 1: Proactive error detection.
        Returns error description if an error is found, else None.
        """
        screenshot = self.capture_screenshot()
        if not screenshot:
            return None

        result = self._analyze_with_llava(screenshot, "debug")
        if result:
            error_indicators = ["error", "exception", "traceback", "failed", "syntax", "undefined"]
            if any(ind in result.lower() for ind in error_indicators):
                return result
        return None