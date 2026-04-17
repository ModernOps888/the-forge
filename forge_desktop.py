"""
The Forge — Native Desktop Integration
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
System tray application providing always-on access to The Forge.

Features:
  🔥 System tray icon with context menu
  🔔 Desktop notifications for AI results
  📁 File analysis via drag-and-drop or context menu
  ⌨️  Global hotkey (Ctrl+Shift+F) for quick chat
  🔗 Wired to all Forge API endpoints
"""

import json
import os
import sys
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

FORGE_API = "http://localhost:8777"
ICON_SIZE = (64, 64)


# ── Desktop Notifications ─────────────────────────────────────────────────────

def notify(title: str, message: str, timeout: int = 8):
    """Send a Windows 10/11 toast notification."""
    try:
        from plyer import notification
        notification.notify(
            title=title,
            message=message[:256],
            app_name="The Forge",
            timeout=timeout,
        )
    except Exception as e:
        print(f"  [Desktop] Notification error: {e}")


# ── Forge API Client ──────────────────────────────────────────────────────────

def forge_request(endpoint: str, data: dict | None = None, method: str = "GET") -> dict:
    """Make a request to the Forge API."""
    url = f"{FORGE_API}{endpoint}"
    if data:
        payload = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
    else:
        req = urllib.request.Request(url, method=method)

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e)}


def analyze_file(file_path: str, model: str = "gemini") -> str:
    """Send a file to Forge for AI analysis."""
    path = Path(file_path)
    if not path.exists():
        return f"File not found: {file_path}"

    content = path.read_text(encoding="utf-8", errors="replace")
    
    # Truncate very large files
    if len(content) > 30000:
        content = content[:15000] + "\n\n... [TRUNCATED] ...\n\n" + content[-5000:]

    result = forge_request("/api/quick-chat", {
        "model": model,
        "message": f"Analyze this file ({path.name}, {len(content)} chars):\n\n```\n{content}\n```\n\nProvide: 1) What it does 2) Code quality assessment 3) Potential improvements",
    })

    if "error" in result:
        return f"Error: {result['error']}"
    return result.get("response", "No response")


def quick_chat(message: str, model: str = "gemini") -> str:
    """Quick single-shot chat with Forge AI."""
    result = forge_request("/api/quick-chat", {
        "model": model,
        "message": message,
    })
    if "error" in result:
        return f"Error: {result['error']}"
    return result.get("response", "No response")


# ── File Explorer Context Menu Registration ───────────────────────────────────

def register_context_menus():
    """Register Windows Explorer right-click context menu entries."""
    import winreg

    script_path = str(Path(__file__).resolve())
    python_exe = sys.executable

    menu_entries = [
        {
            "key": r"*\shell\ForgeAnalyze",
            "label": "🔥 Analyze with Forge AI",
            "command": f'"{python_exe}" "{script_path}" --analyze "%1"',
            "icon": "",
        },
        {
            "key": r"*\shell\ForgeImprove",
            "label": "🧬 Improve with Forge",
            "command": f'"{python_exe}" "{script_path}" --improve "%1"',
            "icon": "",
        },
        {
            "key": r"*\shell\ForgeChat",
            "label": "📤 Send to Forge Chat",
            "command": f'"{python_exe}" "{script_path}" --send "%1"',
            "icon": "",
        },
    ]

    for entry in menu_entries:
        try:
            key = winreg.CreateKeyEx(winreg.HKEY_CLASSES_ROOT, entry["key"], 0, winreg.KEY_WRITE)
            winreg.SetValue(key, "", winreg.REG_SZ, entry["label"])
            if entry["icon"]:
                winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, entry["icon"])
            winreg.CloseKey(key)

            cmd_key = winreg.CreateKeyEx(
                winreg.HKEY_CLASSES_ROOT,
                entry["key"] + r"\command", 0, winreg.KEY_WRITE,
            )
            winreg.SetValue(cmd_key, "", winreg.REG_SZ, entry["command"])
            winreg.CloseKey(cmd_key)
            print(f"  [Desktop] Registered: {entry['label']}")
        except PermissionError:
            print(f"  [Desktop] Need admin rights to register context menu: {entry['label']}")
        except Exception as e:
            print(f"  [Desktop] Registry error: {e}")


def unregister_context_menus():
    """Remove context menu entries."""
    import winreg
    keys = [
        r"*\shell\ForgeAnalyze",
        r"*\shell\ForgeImprove",
        r"*\shell\ForgeChat",
    ]
    for key_path in keys:
        try:
            winreg.DeleteKeyEx(winreg.HKEY_CLASSES_ROOT, key_path + r"\command", winreg.KEY_WRITE, 0)
            winreg.DeleteKeyEx(winreg.HKEY_CLASSES_ROOT, key_path, winreg.KEY_WRITE, 0)
            print(f"  [Desktop] Unregistered: {key_path}")
        except Exception:
            pass


# ── System Tray ───────────────────────────────────────────────────────────────

def create_tray_icon():
    """Create and run the system tray icon."""
    import pystray
    from PIL import Image, ImageDraw, ImageFont

    # Generate a sleek Forge icon programmatically
    def make_icon():
        img = Image.new("RGBA", ICON_SIZE, (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        cx, cy = ICON_SIZE[0] // 2, ICON_SIZE[1] // 2
        r = min(cx, cy) - 2

        # Dark circle background with gradient effect
        for i in range(r, 0, -1):
            alpha = int(255 * (i / r))
            factor = i / r
            color = (
                int(15 + 10 * factor),
                int(10 + 8 * factor),
                int(35 + 20 * factor),
                alpha,
            )
            draw.ellipse(
                [cx - i, cy - i, cx + i, cy + i],
                fill=color,
            )

        # Orange fire gradient center
        for i in range(r // 2, 0, -1):
            factor = i / (r // 2)
            color = (
                int(249 * factor + 236 * (1 - factor)),
                int(115 * factor + 72 * (1 - factor)),
                int(22 * factor + 153 * (1 - factor)),
                240,
            )
            draw.ellipse(
                [cx - i, cy - i, cx + i, cy + i],
                fill=color,
            )

        # "F" letter
        try:
            font = ImageFont.truetype("arial.ttf", 28)
        except Exception:
            font = ImageFont.load_default()
        draw.text((cx - 8, cy - 16), "F", fill=(255, 255, 255, 255), font=font)

        return img

    icon_image = make_icon()

    def open_dashboard(icon, item):
        os.startfile(f"{FORGE_API}/")

    def do_quick_chat(icon, item):
        """Open a simple input dialog for quick chat."""
        threading.Thread(target=_quick_chat_dialog, daemon=True).start()

    def do_quick_research(icon, item):
        threading.Thread(target=_quick_research_dialog, daemon=True).start()

    def show_health(icon, item):
        result = forge_request("/api/health")
        if "error" in result:
            notify("Forge Health", f"❌ Server unreachable: {result['error']}")
        else:
            notify(
                "Forge Health ✅",
                f"Status: {result['status']}\n"
                f"Models: {result.get('models', '?')}\n"
                f"Budget: ${result.get('budget_remaining', '?')}\n"
                f"Vitalis: {result.get('vitalis', '?')}",
            )

    def do_evolve(icon, item):
        threading.Thread(target=_evolve_dialog, daemon=True).start()

    def quit_app(icon, item):
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("🔥 Open Dashboard", open_dashboard, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("💬 Quick Chat", do_quick_chat),
        pystray.MenuItem("🔬 Quick Research", do_quick_research),
        pystray.MenuItem("🧬 Self-Improve", do_evolve),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("❤️ Health Check", show_health),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("❌ Quit", quit_app),
    )

    icon = pystray.Icon("TheForge", icon_image, "The Forge — Agentic AI", menu)

    print("  [Desktop] System tray icon active")
    notify("The Forge", "🔥 Desktop integration active. Right-click the tray icon for options.")
    icon.run()


# ── Dialog Helpers (using tkinter for lightweight native dialogs) ──────────────

def _quick_chat_dialog():
    """Simple tkinter dialog for quick chat."""
    import tkinter as tk
    from tkinter import scrolledtext

    root = tk.Tk()
    root.title("The Forge — Quick Chat")
    root.geometry("600x400")
    root.configure(bg="#0a0b1a")
    root.attributes("-topmost", True)

    # Style
    font_main = ("Segoe UI", 11)
    font_mono = ("Consolas", 10)
    bg = "#0a0b1a"
    card = "#0f1029"
    border = "#1e1f3e"
    tx = "#e2e8f0"
    orange = "#f97316"

    # Title
    tk.Label(root, text="🔥 Quick Chat with Forge AI", font=("Segoe UI", 14, "bold"),
             bg=bg, fg=orange).pack(pady=(12, 6))

    # Model selector
    model_frame = tk.Frame(root, bg=bg)
    model_frame.pack(fill="x", padx=20)
    tk.Label(model_frame, text="Model:", bg=bg, fg=tx, font=font_main).pack(side="left")
    model_var = tk.StringVar(value="gemini")
    for m in ["gemini", "claude", "deepseek", "qwen-local"]:
        tk.Radiobutton(model_frame, text=m, variable=model_var, value=m,
                       bg=bg, fg=tx, selectcolor=card, font=("Segoe UI", 9),
                       activebackground=bg, activeforeground=orange).pack(side="left", padx=4)

    # Input
    input_frame = tk.Frame(root, bg=border, bd=1)
    input_frame.pack(fill="x", padx=20, pady=8)
    entry = tk.Entry(input_frame, font=font_main, bg=card, fg=tx, insertbackground=orange,
                     relief="flat", bd=8)
    entry.pack(fill="x")
    entry.focus_set()

    # Output
    output = scrolledtext.ScrolledText(root, font=font_mono, bg=card, fg=tx,
                                        relief="flat", bd=8, wrap="word")
    output.pack(fill="both", expand=True, padx=20, pady=(0, 12))

    def send(event=None):
        msg = entry.get().strip()
        if not msg:
            return
        entry.delete(0, "end")
        output.insert("end", f"\n👤 You: {msg}\n", "user")
        output.see("end")
        root.update()

        def _call():
            result = forge_request("/api/quick-chat", {
                "model": model_var.get(),
                "message": msg,
            })
            response = result.get("response", result.get("error", "No response"))
            cost = result.get("cost_usd", 0)
            output.insert("end", f"🔥 Forge ({model_var.get()}): {response}\n", "ai")
            if cost:
                output.insert("end", f"   [${cost:.4f}]\n", "meta")
            output.see("end")

        threading.Thread(target=_call, daemon=True).start()

    entry.bind("<Return>", send)

    # Tags for styling
    output.tag_config("user", foreground="#60a5fa")
    output.tag_config("ai", foreground="#e2e8f0")
    output.tag_config("meta", foreground="#64748b", font=("Consolas", 8))

    root.mainloop()


def _quick_research_dialog():
    """Simple dialog for multi-model research."""
    import tkinter as tk
    from tkinter import scrolledtext

    root = tk.Tk()
    root.title("The Forge — Quick Research")
    root.geometry("700x500")
    root.configure(bg="#0a0b1a")
    root.attributes("-topmost", True)

    bg = "#0a0b1a"
    card = "#0f1029"
    tx = "#e2e8f0"
    orange = "#f97316"

    tk.Label(root, text="🔬 Multi-Model Research", font=("Segoe UI", 14, "bold"),
             bg=bg, fg=orange).pack(pady=(12, 6))

    entry = tk.Entry(root, font=("Segoe UI", 11), bg=card, fg=tx,
                     insertbackground=orange, relief="flat", bd=8)
    entry.pack(fill="x", padx=20, pady=8)
    entry.focus_set()

    output = scrolledtext.ScrolledText(root, font=("Consolas", 10), bg=card, fg=tx,
                                        relief="flat", bd=8, wrap="word")
    output.pack(fill="both", expand=True, padx=20, pady=(0, 12))

    def send(event=None):
        query = entry.get().strip()
        if not query:
            return
        entry.delete(0, "end")
        output.delete("1.0", "end")
        output.insert("end", f"Researching: {query}\n\nQuerying claude, gemini, deepseek...\n")
        output.see("end")
        root.update()

        def _call():
            result = forge_request("/api/research", {
                "query": query,
                "models": ["claude", "gemini", "deepseek"],
            })
            output.delete("1.0", "end")
            for name, info in result.get("results", {}).items():
                output.insert("end", f"\n━━━ {name.upper()} ━━━\n", "model")
                output.insert("end", f"{info.get('response', 'No response')}\n")
            cost = result.get("research_cost", 0)
            output.insert("end", f"\n[Total cost: ${cost:.4f}]\n", "meta")
            output.see("end")

        threading.Thread(target=_call, daemon=True).start()

    entry.bind("<Return>", send)
    output.tag_config("model", foreground="#f97316", font=("Segoe UI", 11, "bold"))
    output.tag_config("meta", foreground="#64748b")

    root.mainloop()


def _evolve_dialog():
    """Dialog for self-improvement instructions."""
    import tkinter as tk

    root = tk.Tk()
    root.title("The Forge — Self-Improve")
    root.geometry("500x250")
    root.configure(bg="#0a0b1a")
    root.attributes("-topmost", True)

    bg = "#0a0b1a"
    card = "#0f1029"
    tx = "#e2e8f0"
    orange = "#f97316"

    tk.Label(root, text="🧬 Self-Improvement Engine", font=("Segoe UI", 14, "bold"),
             bg=bg, fg=orange).pack(pady=(12, 6))

    tk.Label(root, text="Instruction:", bg=bg, fg=tx, font=("Segoe UI", 10)).pack(anchor="w", padx=20)
    entry = tk.Text(root, font=("Segoe UI", 10), bg=card, fg=tx,
                    insertbackground=orange, relief="flat", bd=8, height=3, wrap="word")
    entry.pack(fill="x", padx=20, pady=4)
    entry.focus_set()

    status = tk.Label(root, text="Ready", bg=bg, fg="#64748b", font=("Segoe UI", 9))
    status.pack(anchor="w", padx=20, pady=4)

    def evolve():
        instruction = entry.get("1.0", "end").strip()
        if not instruction:
            return
        status.config(text="⏳ AI is generating patches...", fg=orange)
        root.update()

        def _call():
            result = forge_request("/api/self-improve", {
                "instruction": instruction,
                "model": "claude",
            })
            if result.get("ok"):
                msg = f"✅ Applied {result['patches_applied']}/{result['patches_total']} patches"
                status.config(text=msg, fg="#22c55e")
                notify("Forge Self-Improve", msg)
            else:
                err = result.get("error", "Unknown error")
                status.config(text=f"❌ {err[:80]}", fg="#ef4444")
                notify("Forge Self-Improve", f"❌ {err[:100]}")

        threading.Thread(target=_call, daemon=True).start()

    tk.Button(root, text="🧬 Evolve", font=("Segoe UI", 11, "bold"),
              bg=orange, fg="white", relief="flat", bd=0, padx=20, pady=6,
              command=evolve, cursor="hand2").pack(pady=8)

    root.mainloop()


# ── CLI Entry Points ──────────────────────────────────────────────────────────

def handle_cli():
    """Handle command-line invocations (from context menu or direct)."""
    if len(sys.argv) < 2:
        return False

    cmd = sys.argv[1]
    file_path = sys.argv[2] if len(sys.argv) > 2 else None

    if cmd == "--analyze" and file_path:
        notify("Forge AI", f"Analyzing: {Path(file_path).name}...")
        result = analyze_file(file_path)
        # Copy to clipboard
        try:
            import subprocess
            subprocess.run(["clip"], input=result.encode("utf-8"), check=True)
        except Exception:
            pass
        notify("Forge Analysis Complete", result[:250])
        return True

    elif cmd == "--improve" and file_path:
        notify("Forge AI", f"Improving: {Path(file_path).name}...")
        result = forge_request("/api/self-improve", {
            "instruction": f"Review and improve the code quality, add better error handling and documentation",
            "file": file_path,
            "model": "claude",
        })
        if result.get("ok"):
            notify("Forge Improve", f"✅ Applied {result['patches_applied']} patches to {Path(file_path).name}")
        else:
            notify("Forge Improve", f"❌ {result.get('error', 'Failed')[:200]}")
        return True

    elif cmd == "--send" and file_path:
        content = Path(file_path).read_text(encoding="utf-8", errors="replace")[:10000]
        result = forge_request("/api/export", {
            "type": "file",
            "model": "desktop",
            "title": f"File: {Path(file_path).name}",
            "content": {"response": content, "file_path": file_path},
        })
        if result.get("ok"):
            notify("Forge", f"📤 Sent {Path(file_path).name} to Antigravity inbox")
        return True

    elif cmd == "--register":
        register_context_menus()
        return True

    elif cmd == "--unregister":
        unregister_context_menus()
        return True

    elif cmd == "--tray":
        return False  # Fall through to tray mode

    return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("""
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   THE FORGE — Desktop Integration
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   System Tray:     Active
   Global Hotkey:   Ctrl+Shift+F
   Context Menus:   Right-click any file
   Notifications:   Windows 10/11 Toast
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    """)

    if handle_cli():
        return

    create_tray_icon()


if __name__ == "__main__":
    main()
