#!/usr/bin/env python3
"""Force a fresh deploy by pushing a small change."""

import subprocess, os

os.chdir("/home/razer/NoDick")
print("Adding tiny deploy trigger...")
with open("DEPLOY_TRIGGER", "w") as f:
    f.write("Deploy trigger: 2026-07-27T12:00\n")
subprocess.run(["git", "add", "DEPLOY_TRIGGER"], check=True)
subprocess.run(["git", "commit", "-m", "🚀 Trigger fresh Render deploy"], check=True)
subprocess.run(["git", "push", "origin", "main"], check=True)
print("✅ Pushed — Render should rebuild")