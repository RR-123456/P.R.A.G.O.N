"""
Standalone GitHub Monitor diagnostic.
Run this with the SAME python env Pragon uses, from your Pragon project
root (so it can import features/agentic_feature/github_monitor.py).

    python github_monitor_check.py
    python github_monitor_check.py OWNER REPO   # also tests pr_status/repo_status

If GITHUB_TOKEN isn't visible here, it won't be visible to Pragon either --
set it, then open a BRAND NEW terminal window before running this (or
launching Pragon), since env vars only apply to new processes.
"""
import os
import sys

sys.path.insert(0, ".")

print("=" * 60)
print("1. Checking token source (api/githubapi.txt, then GITHUB_TOKEN env var)...")
token_file = os.path.join(".", "api", "githubapi.txt")
token = ""
source = ""
if os.path.exists(token_file):
    with open(token_file, "r", encoding="utf-8") as f:
        raw = f.read().strip()
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if lines and lines[0] != "PASTE_YOUR_GITHUB_TOKEN_HERE":
        token = lines[0]
        source = "api/githubapi.txt"
if not token:
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        source = "GITHUB_TOKEN environment variable"

if not token:
    print("   ❌ No token found in api/githubapi.txt OR the GITHUB_TOKEN env var.")
    print("   Fix: paste your token into api/githubapi.txt (just the token, nothing else).")
    sys.exit(1)
else:
    masked = token[:10] + "..." + token[-4:] if len(token) > 18 else "(short token)"
    print(f"   ✅ Token found via {source}")
    print(f"   Value: {masked}  (length: {len(token)})")
os.environ["GITHUB_TOKEN"] = token  # keep downstream checks consistent

print("\n2. Importing github_monitor module...")
try:
    from features.agentic_feature.github_monitor import GitHubMonitor
    print("   ✅ Import OK")
except Exception as e:
    print(f"   ❌ Import failed: {e}")
    print("   Fix: run this script from your Pragon project root folder.")
    sys.exit(1)

print("\n3. Initializing GitHubMonitor...")
monitor = GitHubMonitor()
if not monitor.is_connected:
    print("   ❌ Monitor reports not connected (token empty at import time)")
    sys.exit(1)
print("   ✅ Connected")

print("\n4. Fetching notifications (tests token validity + API reachability)...")
notifs = monitor.get_notifications()
print(f"   Result: {len(notifs)} unread notification(s)")
for n in notifs:
    print(f"     - [{n['reason']}] {n['title']} ({n['repo']})")
if not notifs:
    print("   (Empty is normal if you have no unread notifications right now —")
    print("    check the console output above for any 'HTTP 401' or 'HTTP 403' error,")
    print("    which would mean the token itself is invalid/expired/missing scopes.)")

if len(sys.argv) == 3:
    owner, repo = sys.argv[1], sys.argv[2]
    print(f"\n5. Testing repo_status on {owner}/{repo}...")
    status = monitor.get_repo_status(owner, repo)
    print(f"   {status.get('summary', status)}")

    print(f"\n6. Testing pr_status on {owner}/{repo}...")
    prs = monitor.get_pr_status(owner, repo)
    print(f"   {len(prs)} open PR(s)")
    for pr in prs:
        print(f"     - #{pr['number']} {pr['title']} by {pr['author']}")
else:
    print("\n(Tip: run 'python github_monitor_check.py OWNER REPO' to also test")
    print(" pr_status and repo_status against a specific repository.)")

print("\n" + "=" * 60)
print("Done. If you saw an HTTP 401 above, your token is invalid/expired.")
print("If you saw HTTP 403, your token is missing required scopes")
print("(needs: notifications read, and repo/contents read for the repos checked).")
