"""Where things live outside this repo: models, sidecars, tools.

The service runs as LocalSystem, whose home folder is not the user's, so the
user profile comes from USER_HOME when it is set rather than from "~".

    USER_HOME       the profile that owns the AI folder and the tools
    AI_HOME         models, sidecar daemons, indexes      (USER_HOME\\AI)
    COMFYUI_DIR     ComfyUI portable; its embedded Python runs the GPU sidecars
    SERVICE_PYTHON  the interpreter the service runs on   (this one)
"""
import os
import sys

USER_HOME = os.environ.get("USER_HOME") or os.path.expanduser("~")
AI_HOME = os.environ.get("AI_HOME") or os.path.join(USER_HOME, "AI")
COMFYUI_DIR = os.environ.get("COMFYUI_DIR") or os.path.join(AI_HOME, "ComfyUI")
COMFY_PYTHON = os.path.join(COMFYUI_DIR, "python_embeded", "python.exe")
SERVICE_PYTHON = os.environ.get("SERVICE_PYTHON") or sys.executable
NPM_GLOBAL = os.environ.get("NPM_GLOBAL") or os.path.join(USER_HOME, "npm-global")


def ai(*parts):
    """A path under AI_HOME."""
    return os.path.join(AI_HOME, *parts)


def to_wsl(path):
    """C:\\Users\\x\\.config -> /mnt/c/Users/x/.config"""
    drive, rest = os.path.splitdrive(path)
    return "/mnt/%s%s" % (drive.rstrip(":").lower(), rest.replace("\\", "/"))


# the WSL user's home, which the opencode runner uses as HOME inside the distro
WSL_HOME = os.environ.get("WSL_HOME") or "/home/" + (os.environ.get("WSL_USER") or os.path.basename(USER_HOME))
WSL_CONFIG_HOME = os.environ.get("WSL_CONFIG_HOME") or to_wsl(os.path.join(USER_HOME, ".config"))
