"""Read settings.json and write state.json on the repo's `data` branch.

The dashboard edits settings.json and the bot edits state.json, each through
GitHub's contents API, so the two never step on each other.
"""

from __future__ import annotations

import base64
import copy
import json
import logging
import os
from typing import Optional, Tuple

import requests

log = logging.getLogger("bot.store")

BRANCH = "data"


class Store:
    def __init__(self, repo: str, token: str):
        self.api = f"https://api.github.com/repos/{repo}"
        self.h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        self.shas: dict = {}
        self.etags: dict = {}   # path -> (etag, data): unchanged files come back 304, which
                                # doesn't count against GitHub's hourly API allowance

    def ensure_branch(self) -> None:
        r = requests.get(f"{self.api}/branches/{BRANCH}", headers=self.h, timeout=20)
        if r.status_code == 200:
            return
        default = requests.get(self.api, headers=self.h, timeout=20).json()["default_branch"]
        sha = requests.get(f"{self.api}/git/ref/heads/{default}", headers=self.h, timeout=20).json()["object"]["sha"]
        requests.post(f"{self.api}/git/refs", headers=self.h, timeout=20,
                      json={"ref": f"refs/heads/{BRANCH}", "sha": sha}).raise_for_status()
        log.info("Created %s branch", BRANCH)

    def read(self, path: str) -> Tuple[Optional[dict], Optional[str]]:
        h = dict(self.h)
        if path in self.etags:
            h["If-None-Match"] = self.etags[path][0]
        r = requests.get(f"{self.api}/contents/{path}", headers=h, params={"ref": BRANCH}, timeout=20)
        if r.status_code == 304:
            return copy.deepcopy(self.etags[path][1]), self.shas.get(path)
        if r.status_code == 404:
            return None, None
        r.raise_for_status()
        body = r.json()
        self.shas[path] = body["sha"]
        if not body.get("content"):
            # Files over 1 MB come back without content; fetch them raw instead.
            raw = requests.get(f"{self.api}/contents/{path}", params={"ref": BRANCH}, timeout=60,
                               headers={**self.h, "Accept": "application/vnd.github.raw"})
            raw.raise_for_status()
            data = raw.json()
        else:
            data = json.loads(base64.b64decode(body["content"]))
        if r.headers.get("ETag"):
            self.etags[path] = (r.headers["ETag"], copy.deepcopy(data))
        return data, body["sha"]

    def write(self, path: str, data: dict, message: str) -> bool:
        payload = {
            "message": message, "branch": BRANCH,
            "content": base64.b64encode(json.dumps(data, separators=(",", ":")).encode()).decode(),
        }
        if self.shas.get(path):
            payload["sha"] = self.shas[path]
        r = requests.put(f"{self.api}/contents/{path}", headers=self.h, json=payload, timeout=30)
        if r.status_code == 409 or r.status_code == 422:
            # Our sha is stale; refresh it and try once more.
            self.read(path)
            payload["sha"] = self.shas.get(path)
            r = requests.put(f"{self.api}/contents/{path}", headers=self.h, json=payload, timeout=30)
        if r.ok:
            self.shas[path] = r.json()["content"]["sha"]
            return True
        log.warning("Write %s failed: %s %s", path, r.status_code, r.text[:200])
        return False


class LocalStore:
    """Same interface on the local disk, for running the bot on a computer."""

    def __init__(self, root: str = "local-data"):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def ensure_branch(self) -> None:
        pass

    def read(self, path: str):
        p = os.path.join(self.root, path)
        if not os.path.exists(p):
            return None, None
        with open(p) as fh:
            return json.load(fh), "local"

    def write(self, path: str, data: dict, message: str) -> bool:
        with open(os.path.join(self.root, path), "w") as fh:
            json.dump(data, fh, indent=1)
        return True
