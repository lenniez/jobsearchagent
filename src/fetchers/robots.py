"""Minimal robots.txt matcher implementing Google's longest-match-wins
precedence rule (ties favor Allow), since stdlib urllib.robotparser doesn't
support the `*` / `$` wildcards that real-world robots.txt files rely on —
verified against builtinnyc.com/robots.txt, whose Allow/Disallow rules only
resolve correctly under this precedence (e.g. `Allow: /jobs*?page=1$`
overriding the broader `Disallow: /jobs*?page=`).
"""
from __future__ import annotations

import re
from urllib.parse import urlparse


def _pattern_to_regex(pattern: str) -> re.Pattern:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    escaped = re.escape(body).replace(r"\*", ".*")
    return re.compile("^" + escaped + ("$" if anchored else ""))


class RobotsRules:
    def __init__(self, rules: list[tuple[str, str]]):
        # rules: list of ("allow" | "disallow", pattern)
        self._rules = [(rule_type, pattern, _pattern_to_regex(pattern)) for rule_type, pattern in rules]

    @classmethod
    def parse(cls, robots_txt: str, user_agent: str) -> "RobotsRules":
        """Extract the rule group matching user_agent, falling back to '*'."""
        groups: dict[str, list[tuple[str, str]]] = {}
        current_agents: list[str] = []
        for raw_line in robots_txt.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            field, _, value = line.partition(":")
            field = field.strip().lower()
            value = value.strip()
            if field == "user-agent":
                agent = value.lower()
                current_agents = [agent]
                groups.setdefault(agent, [])
            elif field in ("allow", "disallow") and current_agents:
                for agent in current_agents:
                    if value:
                        groups.setdefault(agent, []).append((field, value))

        agent_lower = user_agent.lower()
        for agent in groups:
            if agent != "*" and agent in agent_lower:
                return cls(groups[agent])
        return cls(groups.get("*", []))

    def can_fetch(self, path: str) -> bool:
        best = None  # (pattern_length, is_allow)
        for rule_type, pattern, rx in self._rules:
            if rx.match(path):
                length = len(pattern.rstrip("$"))
                is_allow = rule_type == "allow"
                if best is None or length > best[0] or (length == best[0] and is_allow and not best[1]):
                    best = (length, is_allow)
        return True if best is None else best[1]


def path_for(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return path
