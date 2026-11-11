"""Source rules: bundled text files the AI receives as context; user copies override them."""

from __future__ import annotations

import os

BUNDLED = os.path.join(os.path.dirname(__file__), "rules")


class Rules:
    def __init__(self, paths):
        self.user_dir = paths.rules

    def names(self):
        return sorted(f[:-3] for f in os.listdir(BUNDLED) if f.endswith(".md") and f != "README.md")

    def bundled(self, name: str) -> str:
        with open(os.path.join(BUNDLED, f"{name}.md"), encoding="utf-8") as f:
            return f.read()

    def get(self, name: str) -> str:
        user = os.path.join(self.user_dir, f"{name}.md")
        if os.path.isfile(user):
            with open(user, encoding="utf-8") as f:
                return f.read()
        return self.bundled(name)

    def is_edited(self, name: str) -> bool:
        return os.path.isfile(os.path.join(self.user_dir, f"{name}.md"))

    def set(self, name: str, text: str) -> None:
        if name not in self.names():
            raise KeyError(name)
        with open(os.path.join(self.user_dir, f"{name}.md"), "w", encoding="utf-8") as f:
            f.write(text)

    def reset(self, name: str) -> None:
        path = os.path.join(self.user_dir, f"{name}.md")
        if os.path.isfile(path):
            os.remove(path)

    def system(self, name: str) -> str:
        """general.md + the task's rule file."""
        return self.get("general").strip() + "\n\n---\n\n" + self.get(name).strip()
