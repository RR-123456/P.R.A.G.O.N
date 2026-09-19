import asyncio
import base64
import hashlib
import hmac
import json
import os
import signal
import sys
import time
from pathlib import Path
from livekit import rtc

BASE_DIR = Path(__file__).resolve().parent
API_CONFIG_PATH = BASE_DIR / "api" / "api_keys.json"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _mint_token(api_key: str, api_secret: str, room: str = "pragon-room", identity: str = "desktop-client") -> str:
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss": api_key, "sub": identity, "iat": now, "nbf": now, "exp": now + 3600 * 6,
        "name": identity,
        "video": {"room": room, "roomJoin": True, "canPublish": True, "canSubscribe": True, "canPublishData": True},
    }
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8")) + "." +
        _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    )
    sig = hmac.new(api_secret.encode(), signing_input.encode("ascii"), hashlib.sha256).digest()
    return signing_input + "." + _b64url(sig)


async def main():
    room = rtc.Room()

    @room.on("track_subscribed")
    def on_track_subscribed(track: rtc.Track, publication: rtc.RemoteTrackPublication, participant: rtc.RemoteParticipant):
        print(f"Agent {participant.identity} connected and speaking!")

    url = os.getenv("LIVEKIT_URL")
    api_key = os.getenv("LIVEKIT_API_KEY")
    api_secret = os.getenv("LIVEKIT_API_SECRET")

    if not url or not api_key or not api_secret:
        if API_CONFIG_PATH.exists():
            try:
                with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    url = url or cfg.get("livekit_url")
                    api_key = api_key or cfg.get("livekit_api_key")
                    api_secret = api_secret or cfg.get("livekit_api_secret")
            except Exception:
                pass

    token = os.getenv("LIVEKIT_CLIENT_TOKEN")
    if not token and api_key and api_secret:
        token = _mint_token(api_key, api_secret)

    if not token or not url:
        print(f"ERROR: LiveKit credentials missing. Check {API_CONFIG_PATH} or set LIVEKIT_URL and LIVEKIT_CLIENT_TOKEN.")
        return

    print(f"Connecting to LiveKit Room at {url}...")
    await room.connect(url, token)
    print("Connected to LiveKit room!")

    # Publish local microphone if available
    try:
        source = rtc.AudioSource()
        track = rtc.LocalAudioTrack.create_audio_track("mic", source)
        options = rtc.TrackPublishOptions()
        options.source = rtc.TrackSource.SOURCE_MICROPHONE
        await room.local_participant.publish_track(track, options)
        print("Microphone is live. Speak to PRAGON!")
    except Exception as exc:
        print(f"Microphone publish note: {exc}")

    # Keep client running cleanly on Windows and POSIX
    stop_event = asyncio.Event()
    try:
        loop = asyncio.get_running_loop()
        for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
            if sig:
                loop.add_signal_handler(sig, stop_event.set)
    except (NotImplementedError, AttributeError):
        pass

    try:
        while not stop_event.is_set():
            await asyncio.sleep(0.5)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass

    print("\nDisconnecting...")
    await room.disconnect()
    print("Disconnected.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

