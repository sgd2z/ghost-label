"""Onboarding and taste: the listener reacts to real artists, the AI asks a few adaptive
questions and writes the profile; favorites, plays and skips refresh it later."""

from __future__ import annotations

import time

S = {"type": "string"}
STRS = {"type": "array", "items": S}

# Nothing is required at the top level: small local models often drop "done" when they do ask.
OPTION = {"type": "object", "required": ["label"], "properties": {
    "label": {"type": "string", "description": "a short direct answer to the question, 1-5 everyday words"},
    "detail": {"type": "string", "description": "optional: a few plain words that make the answer clearer"}}}
QUESTION = {"type": "object", "properties": {
    "done": {"type": "boolean", "description": "true when you know enough; then leave the other fields out"},
    "question": {"type": "string", "description": "one short everyday question, under 15 words"},
    "options": {"type": "array", "items": OPTION, "minItems": 2, "maxItems": 4,
                "description": "2 to 4 different direct answers to that exact question"},
    "either": {"type": "string", "description": "optional no-preference answer worded for this question"},
    "multiple": {"type": "boolean", "description": "true if picking more than one answer makes sense"}}}

# Mechanical limits only (lengths and counts); what to ask is the model's call.
MAX_QUESTION_CHARS = 140
MAX_LABEL_CHARS = 40
MAX_DETAIL_CHARS = 80


def _clip(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit].rsplit(" ", 1)[0].rstrip(",;:.") + "…"


def _fold(text):
    return " ".join(str(text or "").casefold().split())


def question_problems(q, reactions, answers=None):
    """Mechanical checks on a proposed question; each problem is sent back to the model once."""
    if not answers and answers is not None and q.get("done") is True and (q.get("question") or "").strip():
        q = dict(q, done=False)       # "done" plus a question before anything was asked: take the question
    if q.get("done") is True:
        return [] if answers or answers is None else ["answersSoFar is empty, so ask one question first"]
    problems = []
    question = (q.get("question") or "").strip()
    if not question:
        problems.append("question is empty")
    elif len(question) > MAX_QUESTION_CHARS:
        problems.append(f"question is {len(question)} characters; keep it under {MAX_QUESTION_CHARS}")
    labels = [(o.get("label") or "").strip() if isinstance(o, dict) else "" for o in q.get("options") or []]
    if len([lab for lab in labels if lab]) < 2:
        problems.append("give 2 to 4 answers, each with a label")
    for lab in labels:
        if len(lab) > MAX_LABEL_CHARS:
            problems.append(f'answer "{lab[:50]}…" is too long; keep labels under {MAX_LABEL_CHARS} characters')
    if len({_fold(lab) for lab in labels}) < len(labels):
        problems.append("two answers are the same")
    names = {_fold(r.get("name")) for r in reactions if r.get("name")}
    copied = [lab for lab in labels if _fold(lab) in names]
    if copied:
        problems.append("answers must answer the question, not repeat names from the reactions list: " + ", ".join(copied))
    return problems


PROFILE = {"type": "object", "required": ["summary", "loves"], "properties": {
    "summary": {"type": "string", "description": "one shareable line"},
    "loves": S, "likes": S, "avoids": S, "genres": STRS, "eras": STRS, "moods": STRS,
    "vocals": S, "languages": STRS, "exploration": S}}


class Taste:
    def __init__(self, db, settings, provider, rules, catalog, library, log=print):
        self.db, self.settings, self.provider, self.rules = db, settings, provider, rules
        self.catalog, self.library, self.log = catalog, library, log

    def state(self):
        return self.library.taste()

    def save_reactions(self, reactions, answers=None, notes=None):
        values = {"reactions_json": reactions, "updated": time.time()}
        if answers is not None:
            values["answers_json"] = answers
        if notes is not None:
            values["notes"] = notes
        self.db.update("taste", 1, values)

    def next_question(self, reactions, answers, max_questions=4, status=None):
        if len(answers) >= max_questions:
            return {"done": True}
        import json
        by = {"love": [], "like": [], "no": []}
        for r in reactions:
            by.setdefault(r.get("reaction") or "like", []).append(r.get("name"))
        data = {"lovedArtists": by.get("love", []), "likedArtists": by.get("like", []),
                "notForMeArtists": by.get("no", []),
                "answersSoFar": answers, "questionsLeft": max_questions - len(answers)}
        user = ("Decide whether to ask another question, and if so which.\n\nData:\n"
                + json.dumps(data, ensure_ascii=False, indent=1))
        q = self.provider.json(self.rules.system("taste"), user, QUESTION, name="question", fast=True, status=status,
                               check=lambda value: question_problems(value, reactions, answers))
        asked = (q.get("question") or "").strip()
        if not asked or (q.get("done") is True and answers):
            return {"done": True}
        options = []
        for o in q.get("options") or []:
            label = (o.get("label") or "").strip()[:MAX_LABEL_CHARS]
            if label:
                options.append({"label": label, "detail": _clip(o.get("detail"), MAX_DETAIL_CHARS)})
        either = (q.get("either") or "").strip()[:MAX_LABEL_CHARS]
        if _fold(either) in {_fold(o["label"]) for o in options}:
            either = ""
        return {"done": False, "question": q["question"].strip(), "options": options[:4], "either": either,
                "multiple": bool(q.get("multiple", False))}

    def finish(self, reactions, answers, notes, allow_explicit=None):
        if allow_explicit is not None:
            self.settings.update({"library": {"allowExplicit": bool(allow_explicit)}, "explicitAsked": True})
        self.save_reactions(reactions, answers, notes)
        profile = self.write_profile()
        self.settings.update({"onboarded": True})
        return profile

    def write_profile(self, listening=None):
        import json
        t = self.state() or {}
        data = {"reactions": t.get("reactions") or [], "answers": t.get("answers") or [], "notes": t.get("notes") or "",
                "previousProfile": t.get("profile") or {}}
        rule = "taste"
        task = "Write the listener's taste profile."
        if listening is not None:
            data["listening"] = listening
            rule = "listening"
            task = "Update the listener's taste profile from their listening."
        profile = self.provider.json(self.rules.system(rule), task + "\n\nData:\n" + json.dumps(data, ensure_ascii=False, indent=1),
                                     PROFILE, name="profile")
        self.db.update("taste", 1, {"profile_json": profile, "signals_since_refresh": 0, "updated": time.time()})
        return profile

    def maybe_refresh(self):
        every = int(self.settings.section("library").get("tasteRefreshEvery") or 0)
        t = self.state() or {}
        if every <= 0 or (t.get("signals_since_refresh") or 0) < every or not self.provider.configured():
            return None
        self.db.update("taste", 1, {"signals_since_refresh": 0})
        return self.write_profile(listening=self.library.listening_summary())
