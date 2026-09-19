"""
agentic/crew_orchestrator.py
─────────────────────────────
Jarvis v2.0 — CrewAI Orchestration (PRD Section 6.3 / Pillar 3)

Three-agent crew:
  • Planner Agent — breaks the task into steps
  • Executor Agent — carries out each step
  • Reviewer Agent — checks output quality, triggers self-correction

Falls back gracefully if crewai is not installed.
"""

from __future__ import annotations
import json
import urllib.request
from datetime import datetime


# ── Lightweight Agent base (no crewai dependency required) ────────────────

class Agent:
    """Simple agent that calls Ollama with a role-specific system prompt."""

    def __init__(self, role: str, goal: str, model: str = "gemma3:1b"):
        self.role = role
        self.goal = goal
        self.model = model

    def run(self, task: str, context: str = "") -> str:
        system = (
            f"You are the {self.role} agent in a multi-agent AI system.\n"
            f"Your goal: {self.goal}\n"
            f"Context from previous agents:\n{context}\n\n"
            "Be concise. No markdown. No bullet points. Plain sentences only."
        )
        try:
            body = json.dumps({
                "model" : self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": task}
                ],
                "stream" : False
            }).encode("utf-8")
            req = urllib.request.Request(
                "http://localhost:11434/api/chat",
                data=body,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return result["message"]["content"].strip()
        except Exception as e:
            return f"[{self.role} agent error: {e}]"


class CrewOrchestrator:
    """
    Three-agent crew for complex multi-step tasks.

    Flow:
      1. Planner breaks task into ordered steps
      2. Executor executes each step sequentially
      3. Reviewer checks result quality, flags issues

    Used for:
      - Morning orchestration (calendar + GitHub + email draft)
      - Chain-of-action workflows (PRD Pillar 3)
      - Complex research tasks
    """

    def __init__(self, model: str = "gemma3:1b", groq_brain=None):
        self.model = model
        self.groq_brain = groq_brain # use Groq for smarter reasoning if available

        self.planner = Agent(
            role = "Planner",
            goal = "Break the user's request into clear, ordered steps. Output a numbered plan.",
            model = model
        )
        self.executor = Agent(
            role = "Executor",
            goal = "Execute the plan step by step. Be specific and actionable.",
            model = model
        )
        self.reviewer = Agent(
            role = "Reviewer",
            goal = (
                "Review the executor's output for quality and completeness. "
                "If something is wrong or incomplete, say RETRY. "
                "If it is good, say APPROVED and summarise the result in one sentence."
            ),
            model = model
        )

        self._max_retries = 3

    def run(self, task: str, context: str = "") -> str:
        """
        Run the full crew pipeline on a task.
        Returns the final reviewed result.
        """
        print(f"[ Crew ] Starting orchestration: {task[:60]}")

        # Step 1: Plan
        plan = self.planner.run(task, context)
        print(f"[ Crew / Planner ] {plan[:120]}")

        # Step 2: Execute with self-correction loop
        executor_output = ""
        for attempt in range(1, self._max_retries + 1):
            exec_context = f"Plan:\n{plan}\n\nPrevious attempt: {executor_output}"
            executor_output = self.executor.run(task, exec_context)
            print(f"[ Crew / Executor ] Attempt {attempt}: {executor_output[:120]}")

            # Step 3: Review
            review_context = f"Task: {task}\nPlan: {plan}\nOutput: {executor_output}"
            review = self.reviewer.run("Review this output.", review_context)
            print(f"[ Crew / Reviewer ] {review[:120]}")

            if "RETRY" not in review.upper():
                # Approved — extract summary
                summary = review.replace("APPROVED", "").strip().lstrip(".,- ")
                return summary if summary else executor_output

        # All retries exhausted
        return (
            f"I ran three planning cycles on this task, RR, "
            f"and here is the best result I could produce: {executor_output[:200]}"
        )

    def morning_orchestration(
        self,
        calendar_events: list[dict],
        deadlines: list[dict],
        github_status: dict | None = None
    ) -> str:
        """
        PRD Pillar 3 — Morning Chain-of-Action.
        Reads calendar + deadlines + GitHub, drafts brief + any needed emails.
        """
        context_parts = []

        if calendar_events:
            events_str = ", ".join([
                f"{e.get('title','event')} at {e.get('time','?')}"
                for e in calendar_events[:5]
            ])
            context_parts.append(f"Today's calendar: {events_str}")

        if deadlines:
            dl_str = ", ".join([
                f"{d['title']} due {d['due_date']}"
                for d in deadlines[:3]
            ])
            context_parts.append(f"Active deadlines: {dl_str}")

        if github_status:
            context_parts.append(
                f"GitHub status: {github_status.get('summary', 'No issues detected')}"
            )

        context = "\n".join(context_parts) if context_parts else "No external data available."

        task = (
            "Generate a warm, motivating morning brief for RR. "
            "Mention today's schedule, upcoming deadlines, and any urgent items. "
            "Keep it under 5 sentences. Spoken aloud via TTS — no markdown."
        )
        return self.run(task, context)