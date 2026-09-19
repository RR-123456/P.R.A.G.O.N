"""
agentic/action_agent.py
────────────────────────
Jarvis v2.0 — Autonomous Action Agent

Uses CONTEXTUAL EMBEDDING (sentence-transformers) to detect action intent.
Falls back to word-set matching if embeddings unavailable.

Instead of brittle keyword matching, we embed the user query and compare
cosine similarity against a library of action example sentences.
"""

from __future__ import annotations
import os, time, json, subprocess, glob
import pyautogui, pyperclip
import urllib.request
from datetime import datetime


# ── Action example sentences for embedding comparison ─────────────────────
# Each list = example phrasings that should trigger that action
ACTION_EXAMPLES = {
    "book_train_ticket": [
        "how to book a train ticket",
        "book train ticket on IRCTC",
        "I want to travel by train",
        "reserve a seat on the train",
        "train booking from Chennai to Mumbai",
        "how do I buy a railway ticket",
        "book rail ticket online",
        "IRCTC ticket booking",
        "I need to go by train",
    ],
    "book_flight": [
        "book a flight ticket",
        "I want to fly to Delhi",
        "find me a flight",
        "book air ticket",
        "how to book airplane ticket",
        "flight from Chennai to Bangalore",
    ],
    "book_cab": [
        "book an Uber",
        "call a cab for me",
        "I need a ride",
        "book Ola cab",
        "get me a taxi",
        "how to book a cab",
    ],
    "order_food": [
        "order food online",
        "I want to order pizza",
        "order from Swiggy",
        "get me food delivery",
        "order biryani from Zomato",
        "how to order food",
    ],
    "edit_video": [
        "edit this video for me",
        "how to edit a video",
        "I want to trim a video",
        "cut and edit video clips",
        "add music to my video",
        "help me edit video",
    ],
    "book_hotel": [
        "book a hotel room",
        "find me a hotel in Goa",
        "I need a place to stay",
        "hotel booking for tonight",
    ],
    "pay_bill": [
        "pay my electricity bill",
        "how to pay utility bill online",
        "pay water bill",
        "online bill payment",
    ],
    "recharge_phone": [
        "recharge my mobile",
        "do a phone recharge",
        "add balance to my number",
        "how to recharge prepaid",
    ],
    "shop_amazon": [
        "buy something on Amazon",
        "order from Amazon",
        "shop on Amazon India",
        "search Amazon for headphones",
    ],
    "shop_flipkart": [
        "buy on Flipkart",
        "order from Flipkart",
        "search Flipkart for shoes",
    ],
}

# Threshold: cosine similarity above this = action intent detected
SIMILARITY_THRESHOLD = 0.45


class EmbeddingActionDetector:
    """Embedding-based action intent detector using sentence-transformers."""

    def __init__(self):
        self._model = None
        self._action_vecs = {}
        self._np = None
        self._ready = False
        self._try_load()

    def _try_load(self):
        try:
            from sentence_transformers import SentenceTransformer
            import numpy as np
            self._np = np
            self._model = SentenceTransformer("all-MiniLM-L6-v2")
            # Pre-compute mean vector for each action
            for action, examples in ACTION_EXAMPLES.items():
                vecs = self._model.encode(examples, convert_to_numpy=True)
                self._action_vecs[action] = vecs.mean(axis=0)
            self._ready = True
            print("[ Action ] Embedding-based intent detection ready")
        except ImportError:
            print("[ Action ] sentence-transformers not installed — using word-set fallback")
        except Exception as e:
            print(f"[ Action ] Embedding load error: {e} — using fallback")

    def _cosine(self, a, b) -> float:
        na = self._np.linalg.norm(a)
        nb = self._np.linalg.norm(b)
        if na == 0 or nb == 0:
            return 0.0
        return float(self._np.dot(a, b) / (na * nb))

    def detect(self, text: str) -> tuple[str | None, float]:
        """
        Returns (action_name, confidence) or (None, 0).
        Uses cosine similarity between query embedding and action prototypes.
        """
        if not self._ready:
            return None, 0.0
        query_vec = self._model.encode([text], convert_to_numpy=True)[0]
        best_action = None
        best_score = 0.0
        for action, proto in self._action_vecs.items():
            score = self._cosine(query_vec, proto)
            if score > best_score:
                best_score = score
                best_action = action
        if best_score >= SIMILARITY_THRESHOLD:
            print(f"[ Action ] Embedding match: '{text[:40]}' {best_action} (sim={best_score:.3f})")
            return best_action, best_score
        print(f"[ Action ] No embedding match (best={best_score:.3f} for {best_action})")
        return None, best_score


# ── Word-set fallback (no embeddings) ─────────────────────────────────────
FALLBACK_RULES = [
    ({"train", "ticket"}, "book_train_ticket"),
    ({"train", "book"}, "book_train_ticket"),
    ({"train", "irctc"}, "book_train_ticket"),
    ({"railway", "ticket"}, "book_train_ticket"),
    ({"rail", "book"}, "book_train_ticket"),
    ({"flight", "book"}, "book_flight"),
    ({"flight", "ticket"}, "book_flight"),
    ({"cab", "book"}, "book_cab"),
    ({"uber"}, "book_cab"),
    ({"ola", "book"}, "book_cab"),
    ({"food", "order"}, "order_food"),
    ({"swiggy"}, "order_food"),
    ({"zomato"}, "order_food"),
    ({"video", "edit"}, "edit_video"),
    ({"hotel", "book"}, "book_hotel"),
    ({"bill", "pay"}, "pay_bill"),
    ({"electricity", "bill"}, "pay_bill"),
    ({"recharge"}, "recharge_phone"),
    ({"amazon", "buy"}, "shop_amazon"),
    ({"flipkart", "buy"}, "shop_flipkart"),
]

HOW_TO_TRIGGERS = [
    "how to", "how do i", "how can i", "help me",
    "i want to", "i need to", "book me", "order me",
    "get me a", "find me a",
]

FILLERS = {"how","to","a","an","the","i","me","can","do","please","want","need","for","my","should"}


def _word_set_detect(text: str) -> str | None:
    words = set(text.lower().split())
    clean_words = words - FILLERS
    is_how_to = any(pat in text.lower() for pat in HOW_TO_TRIGGERS)

    for word_set, action in FALLBACK_RULES:
        if word_set.issubset(clean_words) or (is_how_to and word_set.issubset(words)):
            print(f"[ Action ] Fallback word-set match: {word_set} {action}")
            return action
    return None


# ── Main ActionAgent class ─────────────────────────────────────────────────

class ActionAgent:
    def __init__(self, speak_fn, listen_fn, confirm_gate=None):
        self._speak = speak_fn
        self._listen = listen_fn
        self._gate = confirm_gate
        self._browser = self._find_browser()
        self._detector = EmbeddingActionDetector()

    def _find_browser(self) -> str:
        paths = [
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        ]
        for p in paths:
            if os.path.exists(p):
                return p
        return "msedge"

    def _open_url(self, url: str):
        try:
            subprocess.Popen([self._browser, url])
        except Exception:
            subprocess.Popen(["cmd", "/c", "start", url])
        time.sleep(3)

    def _ask(self, question: str) -> str:
        self._speak(question)
        return self._listen() or ""

    # ── Intent Detection ──────────────────────────────────────────────

    def detect_action_intent(self, text: str) -> str | None:
        # 1. Try embedding-based detection first
        action, score = self._detector.detect(text)
        if action:
            return action
        # 2. Word-set fallback
        return _word_set_detect(text)

    # ── Dispatcher ────────────────────────────────────────────────────

    def execute(self, action: str, original_query: str) -> str:
        executors = {
            "book_train_ticket": self._book_train,
            "book_flight" : self._book_flight,
            "book_cab" : self._book_cab,
            "book_hotel" : self._book_hotel,
            "order_food" : self._order_food,
            "edit_video" : self._edit_video,
            "pay_bill" : self._pay_bill,
            "recharge_phone" : self._recharge_phone,
            "shop_amazon" : self._shop_amazon,
            "shop_flipkart" : self._shop_flipkart,
        }
        try:
            return executors.get(action, self._generic_task)(original_query)
        except Exception as e:
            print(f"[ Action ] Executor error: {e}")
            return f"I ran into an issue: {e}"

    # ── Executors ─────────────────────────────────────────────────────

    def _book_train(self, query: str) -> str:
        self._speak("Opening IRCTC right now, RR.")
        self._open_url("https://www.irctc.co.in/nget/train-search")
        from_city = self._ask("Which city are you travelling from?")
        to_city = self._ask(f"And where are you going from {from_city}?")
        date = self._ask("What is your travel date?")
        self._speak(f"IRCTC is open. Search trains from {from_city} to {to_city} on {date}. Log in if prompted.")
        return f"IRCTC opened. {from_city} {to_city} on {date}."

    def _book_flight(self, query: str) -> str:
        self._speak("Opening MakeMyTrip for flights.")
        from_city = self._ask("Flying from which city?")
        to_city = self._ask("Flying to which city?")
        date = self._ask("Travel date?")
        self._open_url("https://www.makemytrip.com/flights/")
        self._speak(f"MakeMyTrip is open. Search {from_city} to {to_city} on {date}.")
        return f"MakeMyTrip opened. {from_city} {to_city}."

    def _book_cab(self, query: str) -> str:
        self._speak("Opening Uber for you.")
        dest = self._ask("Where would you like to go?")
        self._open_url("https://m.uber.com/looking")
        self._speak(f"Uber is open. Enter {dest} as your destination.")
        return f"Uber opened. Destination: {dest}."

    def _book_hotel(self, query: str) -> str:
        city = self._ask("Which city do you need a hotel in?")
        self._speak(f"Searching hotels in {city}.")
        self._open_url(f"https://www.makemytrip.com/hotels/{city.lower().replace(' ','-')}-hotels.html")
        return f"Hotels opened for {city}."

    def _order_food(self, query: str) -> str:
        platform = "zomato" if "zomato" in query.lower() else "swiggy"
        food_item = self._ask("What would you like to order?")
        self._speak(f"Opening {platform.title()} for {food_item}.")
        urls = {
            "swiggy": f"https://www.swiggy.com/search?query={food_item.replace(' ','+')}",
            "zomato": f"https://www.zomato.com/order-food-online?query={food_item.replace(' ','+')}",
        }
        self._open_url(urls[platform])
        return f"{platform.title()} opened for {food_item}."

    def _edit_video(self, query: str) -> str:
        self._speak("Looking for a video editor on your system.")
        editors = [
            ("CapCut", r"C:\Users\%USERNAME%\AppData\Local\CapCut\Apps\CapCut.exe"),
            ("DaVinci", r"C:\Program Files\Blackmagic Design\DaVinci Resolve\Resolve.exe"),
        ]
        for name, path in editors:
            expanded = os.path.expandvars(path)
            matches = glob.glob(expanded)
            found = matches[0] if matches else (expanded if os.path.exists(expanded) else None)
            if found:
                self._speak(f"Found {name}. Opening it now.")
                subprocess.Popen([found])
                time.sleep(5)
                edit_type = self._ask(f"{name} is open. What edit do you need — trim, add music, add text, or merge?")
                self._speak(f"Import your video into {name} and I will guide you through {edit_type}.")
                return f"{name} opened for {edit_type}."
        self._speak("Opening Clipchamp online — free and easy.")
        self._open_url("https://clipchamp.com/en/video-editor/")
        return "Clipchamp online opened."

    def _pay_bill(self, query: str) -> str:
        self._speak("Opening PhonePe for bill payment.")
        self._open_url("https://phonepe.com/en/recharge-and-bill-payment.html")
        return "PhonePe opened."

    def _recharge_phone(self, query: str) -> str:
        number = self._ask("Which mobile number should I recharge?")
        self._speak(f"Opening Paytm for recharge of {number}.")
        self._open_url("https://paytm.com/mobile-recharge")
        return f"Paytm opened for {number}."

    def _shop_amazon(self, query: str) -> str:
        item = self._ask("What would you like to buy?")
        self._speak(f"Searching Amazon for {item}.")
        self._open_url(f"https://www.amazon.in/s?k={item.replace(' ','+')}")
        return f"Amazon opened for {item}."

    def _shop_flipkart(self, query: str) -> str:
        item = self._ask("What would you like to buy on Flipkart?")
        self._speak(f"Searching Flipkart for {item}.")
        self._open_url(f"https://www.flipkart.com/search?q={item.replace(' ','+')}")
        return f"Flipkart opened for {item}."

    def _generic_task(self, query: str) -> str:
        self._speak("Let me find the best way to do this for you.")
        self._open_url(f"https://www.google.com/search?q={query.replace(' ','+')}")
        return f"Google search opened for: {query}"