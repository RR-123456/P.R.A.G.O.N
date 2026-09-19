"""
agentic/n8n_integration.py
───────────────────────────
Jarvis v2.0 — n8n Automation Engine Integration

n8n is a self-hosted workflow automation tool.
Jarvis triggers n8n webhooks to execute cross-app workflows:
  - Send Gmail
  - Read Google Calendar
  - Send WhatsApp via Twilio
  - Post to Slack
  - Create Notion tasks
  - Trigger any custom n8n workflow

Setup:
  1. Install n8n: npm install -g n8n OR docker run -it n8nio/n8n
  2. Start n8n: n8n start (runs on http://localhost:5678)
  3. Create workflows with Webhook triggers
  4. Set N8N_BASE_URL environment variable (default: http://localhost:5678)
  5. Set N8N_API_KEY if you enabled API auth in n8n settings
"""

from __future__ import annotations
import os
import json
import urllib.request
import urllib.error
from datetime import datetime


N8N_BASE_URL = os.environ.get("N8N_BASE_URL", "http://localhost:5678")
N8N_API_KEY = os.environ.get("N8N_API_KEY", "")

# Webhook paths — set these up in your n8n instance
N8N_WEBHOOKS = {
    "send_gmail" : "/webhook/jarvis-send-gmail",
    "read_calendar" : "/webhook/jarvis-read-calendar",
    "send_whatsapp" : "/webhook/jarvis-send-whatsapp",
    "create_task" : "/webhook/jarvis-create-task",
    "morning_brief" : "/webhook/jarvis-morning-brief",
    "github_status" : "/webhook/jarvis-github-status",
    "food_order" : "/webhook/jarvis-food-order",
    "custom" : "/webhook/jarvis-custom",
}


class N8NIntegration:
    """
    Triggers n8n workflows via webhook calls.
    All workflows must be set up in your n8n instance first.
    """

    def __init__(self):
        self._base = N8N_BASE_URL.rstrip("/")
        self._key = N8N_API_KEY
        self._enabled = self._check_connection()

    def _check_connection(self) -> bool:
        try:
            req = urllib.request.Request(
                f"{self._base}/healthz",
                headers={"Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                print(f"[ n8n ] Connected to n8n at {self._base}")
                return True
        except Exception:
            print(f"[ n8n ] n8n not running at {self._base}")
            print(" Start with: n8n start OR docker run -it n8nio/n8n")
            return False

    def _trigger_webhook(self, webhook_name: str, data: dict) -> dict | None:
        """POST data to an n8n webhook and return the response."""
        if not self._enabled:
            print(f"[ n8n ] Skipping {webhook_name} — n8n not connected")
            return None

        path = N8N_WEBHOOKS.get(webhook_name, f"/webhook/{webhook_name}")
        url = f"{self._base}{path}"

        try:
            body = json.dumps(data).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            if self._key:
                headers["X-N8N-API-KEY"] = self._key

            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                print(f"[ n8n ] Webhook {webhook_name} triggered")
                return result
        except urllib.error.URLError as e:
            print(f"[ n8n ] Webhook {webhook_name} failed: {e}")
            return None
        except Exception as e:
            print(f"[ n8n ] Error: {e}")
            return None

    # ── Gmail ─────────────────────────────────────────────────────────

    def send_gmail(self, to: str, subject: str, body: str) -> bool:
        """Send Gmail via n8n Gmail node."""
        result = self._trigger_webhook("send_gmail", {
            "to" : to,
            "subject": subject,
            "body" : body,
            "timestamp": datetime.now().isoformat()
        })
        return result is not None

    # ── Google Calendar ───────────────────────────────────────────────

    def get_calendar_events(self, date: str = None) -> list[dict]:
        """Fetch today's Google Calendar events via n8n."""
        result = self._trigger_webhook("read_calendar", {
            "date": date or datetime.now().strftime("%Y-%m-%d")
        })
        if result and isinstance(result, list):
            return result
        if result and "events" in result:
            return result["events"]
        return []

    # ── WhatsApp (via Twilio) ─────────────────────────────────────────

    def send_whatsapp(self, to_number: str, message: str) -> bool:
        """Send WhatsApp message via n8n Twilio node."""
        result = self._trigger_webhook("send_whatsapp", {
            "to" : to_number,
            "message": message
        })
        return result is not None

    # ── Task Creation ─────────────────────────────────────────────────

    def create_task(self, title: str, due_date: str = "", notes: str = "") -> bool:
        """Create a task in Notion/Todoist via n8n."""
        result = self._trigger_webhook("create_task", {
            "title" : title,
            "due_date": due_date,
            "notes" : notes
        })
        return result is not None

    # ── Morning Brief Data ────────────────────────────────────────────

    def get_morning_brief_data(self) -> dict:
        """
        Trigger n8n morning brief workflow.
        Returns enriched data: calendar + weather + traffic + GitHub.
        """
        result = self._trigger_webhook("morning_brief", {
            "timestamp": datetime.now().isoformat(),
            "date" : datetime.now().strftime("%Y-%m-%d")
        })
        if result:
            return result
        return {}

    # ── GitHub Status ─────────────────────────────────────────────────

    def get_github_status(self, repo: str = "") -> dict:
        """Get GitHub CI/PR status via n8n GitHub node."""
        result = self._trigger_webhook("github_status", {"repo": repo})
        return result or {}

    # ── Custom Workflow ───────────────────────────────────────────────

    def trigger_custom(self, workflow_name: str, data: dict) -> dict | None:
        """Trigger any custom n8n workflow by webhook name."""
        return self._trigger_webhook(workflow_name, data)

    @property
    def is_connected(self) -> bool:
        return self._enabled

    def get_status(self) -> dict:
        return {
            "connected" : self._enabled,
            "base_url" : self._base,
            "api_key_set": bool(self._key),
            "webhooks" : list(N8N_WEBHOOKS.keys())
        }