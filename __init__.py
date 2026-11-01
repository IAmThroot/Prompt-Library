"""Prompt Library for ComfyUI.

Collects prompts from the metadata of your generated images, audio and video, keeps them
in a searchable library (even after the files are deleted), and lets you copy them back
into your workflow.
"""
from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

WEB_DIRECTORY = "./web"
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]

try:
    from . import db, routes, scanner, service  # noqa: F401  (routes registers the HTTP endpoints)

    db.init()
    from server import PromptServer
    PromptServer.instance.app.on_startup.append(service.startup)
    PromptServer.instance.app.on_cleanup.append(service.cleanup)
    scanner.start_scan()  # import whatever is already in the outputs folder
except Exception as e:  # never stop ComfyUI from starting
    print(f"[Prompt Library] backend failed to start: {e}")
