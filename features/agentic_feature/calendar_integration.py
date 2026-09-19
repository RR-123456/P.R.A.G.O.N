"""
agentic/calendar_integration.py
─────────────────────────────────
Jarvis v2.0 — Google Calendar Integration

Reads today's events from Google Calendar via the API.
Used by Morning Brief + Chain-of-Action orchestration.

Setup (one-time):
  1. Go to console.cloud.google.com
  2. Create a project Enable Google Calendar API
  3. Create OAuth2 credentials Download as credentials.json
  4. Place credentials.json in your project root
  5. First run will open browser for auth saves token.json

No credentials = graceful fallback (no calendar in brief).
"""

from __future__ import annotations
import os
import json
from datetime import datetime, timedelta, timezone


SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']
CREDENTIALS_FILE = 'credentials.json'
TOKEN_FILE = 'token.json'


class CalendarIntegration:
    def __init__(self):
        self._service = None
        self._enabled = False
        self._try_init()

    def _try_init(self):
        try:
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from google.auth.transport.requests import Request
            from googleapiclient.discovery import build

            creds = None

            if os.path.exists(TOKEN_FILE):
                creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                elif os.path.exists(CREDENTIALS_FILE):
                    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
                    creds = flow.run_local_server(port=0)
                    with open(TOKEN_FILE, 'w') as f:
                        f.write(creds.to_json())
                else:
                    print("[ Calendar ] credentials.json not found — Calendar disabled")
                    return

            self._service = build('calendar', 'v3', credentials=creds)
            self._enabled = True
            print("[ Calendar ] Google Calendar connected")

        except ImportError:
            print("[ Calendar ] google-api-python-client not installed — Calendar disabled")
            print(" Install: pip install google-api-python-client google-auth-oauthlib")
        except Exception as e:
            print(f"[ Calendar ] Init failed: {e}")

    def get_today_events(self) -> list[dict]:
        """
        Fetch today's calendar events.
        Returns list of { title, time, location, description }
        """
        if not self._enabled or not self._service:
            return self._get_demo_events()

        try:
            now = datetime.now(timezone.utc)
            end_of_day = now.replace(hour=23, minute=59, second=59)

            events_result = self._service.events().list(
                calendarId = 'primary',
                timeMin = now.isoformat(),
                timeMax = end_of_day.isoformat(),
                maxResults = 10,
                singleEvents= True,
                orderBy = 'startTime'
            ).execute()

            events = events_result.get('items', [])
            result = []

            for event in events:
                start = event['start'].get('dateTime', event['start'].get('date', ''))
                # Parse time
                try:
                    dt = datetime.fromisoformat(start.replace('Z', '+00:00'))
                    time = dt.strftime('%I:%M %p')
                except Exception:
                    time = start

                result.append({
                    'title' : event.get('summary', 'Untitled event'),
                    'time' : time,
                    'location' : event.get('location', ''),
                    'description': event.get('description', '')[:100],
                })

            return result

        except Exception as e:
            print(f"[ Calendar ] Fetch error: {e}")
            return []

    def get_upcoming_events(self, days: int = 7) -> list[dict]:
        """Fetch events for the next N days."""
        if not self._enabled:
            return []
        try:
            now = datetime.now(timezone.utc)
            future = now + timedelta(days=days)

            events_result = self._service.events().list(
                calendarId = 'primary',
                timeMin = now.isoformat(),
                timeMax = future.isoformat(),
                maxResults = 20,
                singleEvents= True,
                orderBy = 'startTime'
            ).execute()

            return [
                {
                    'title': e.get('summary', 'Event'),
                    'time' : e['start'].get('dateTime', e['start'].get('date', ''))
                }
                for e in events_result.get('items', [])
            ]
        except Exception as e:
            print(f"[ Calendar ] Upcoming fetch error: {e}")
            return []

    def _get_demo_events(self) -> list[dict]:
        """Demo events when Calendar is not connected."""
        return []

    @property
    def is_connected(self) -> bool:
        return self._enabled