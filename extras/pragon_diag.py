"""Run this with the SAME python/venv that runs Pragon to see what's missing."""
import importlib

# Required
required = ["sounddevice","websockets","google.genai","google.generativeai","PIL",
            "requests","bs4","ddgs","playwright","pyautogui","pynput",
            "pyperclip","pygetwindow","cv2","numpy","mss","psutil","send2trash",
            "youtube_transcript_api","pptx","fastapi","uvicorn","cryptography",
            "multipart","qrcode"]

# Optional (graceful fallback if missing, but changes behavior!)
optional = ["langdetect","sentence_transformers","googleapiclient","google_auth_oauthlib",
            "pypdf","docx","openpyxl","pytesseract","chromadb"]

def check(name):
    try:
        importlib.import_module(name)
        return "OK"
    except ImportError as e:
        return f"MISSING ({e})"

print("=== REQUIRED ===")
for m in required:
    print(f"{m:30s} {check(m)}")

print("\n=== OPTIONAL (silent fallback = degraded behavior, no crash) ===")
for m in optional:
    print(f"{m:30s} {check(m)}")
