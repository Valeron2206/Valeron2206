"""Render the neofetch-style profile card from live GitHub statistics.

Reads profile.txt for the copy and ascii_art.txt for the portrait, queries the
GitHub API for the numbers, and writes dark_mode.svg and light_mode.svg.

    ACCESS_TOKEN=<pat> USER_NAME=<login> python today.py
"""
import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
USER = os.environ.get("USER_NAME", "Valeron2206")
TOKEN = os.environ.get("ACCESS_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
HEADERS = {"Authorization": f"bearer {TOKEN}", "User-Agent": f"{USER}-profile-card"}

# Layout, in CSS pixels. Character widths assume a 0.6em monospace advance.
ART_FONT, ART_LINE, ART_X, ART_Y = 9, 11, 20, 22
TXT_FONT, TXT_LINE, TXT_Y = 13, 17, 26
GUTTER, PAD = 24, 20

THEMES = {
    "dark_mode.svg": {
        "bg": "#161b22", "text": "#c9d1d9", "key": "#ffa657",
        "value": "#a5d6ff", "dots": "#616e7f", "add": "#3fb950", "del": "#f85149",
    },
    "light_mode.svg": {
        "bg": "#ffffff", "text": "#24292f", "key": "#953800",
        "value": "#0a3069", "dots": "#57606a", "add": "#1a7f37", "del": "#cf222e",
    },
}


# --------------------------------------------------------------------- GitHub

def graphql(query, attempts=5, **variables):
    """GitHub occasionally answers 502 under load, so retry with a backoff."""
    body = json.dumps({"query": query, "variables": variables}).encode()
    for attempt in range(attempts):
        request = urllib.request.Request(
            "https://api.github.com/graphql", data=body,
            headers={**HEADERS, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request) as response:
                payload = json.load(response)
        except (urllib.error.HTTPError, urllib.error.URLError) as error:
            code = getattr(error, "code", None)
            if code in (502, 503, 504) or isinstance(error, urllib.error.URLError):
                if attempt == attempts - 1:
                    raise
                time.sleep(2 ** attempt)
                continue
            raise
        data = payload.get("data")
        if data is None:
            raise RuntimeError(payload.get("errors", payload))
        return data


REPO_QUERY = """
query($login:String!, $cursor:String){
  user(login:$login){
    createdAt
    followers{totalCount}
    repositoriesContributedTo(contributionTypes:[COMMIT,PULL_REQUEST,REPOSITORY]){totalCount}
    repositories(first:100, after:$cursor, ownerAffiliations:OWNER){
      pageInfo{hasNextPage endCursor}
      nodes{ nameWithOwner isFork stargazerCount }
    }
  }
}"""

# Counted per repository rather than from contributionsCollection: commits
# authored with an email GitHub cannot match to the account are missing there.
HISTORY_QUERY = """
query($owner:String!, $name:String!){
  repository(owner:$owner, name:$name){
    defaultBranchRef{ target{ ... on Commit { history{ totalCount } } } }
  }
}"""


def collect():
    repos, cursor, user = [], None, None
    while True:
        user = graphql(REPO_QUERY, login=USER, cursor=cursor)["user"]
        repos += user["repositories"]["nodes"]
        page = user["repositories"]["pageInfo"]
        if not page["hasNextPage"]:
            break
        cursor = page["endCursor"]

    created = dt.datetime.fromisoformat(user["createdAt"].replace("Z", "+00:00"))
    owned = [r for r in repos if not r["isFork"]]

    now = dt.datetime.now(dt.timezone.utc)
    commits = 0
    for repo in owned:
        owner, name = repo["nameWithOwner"].split("/")
        branch = graphql(HISTORY_QUERY, owner=owner,
                         name=name)["repository"]["defaultBranchRef"]
        if branch is None:                           # repository without commits
            continue
        commits += branch["target"]["history"]["totalCount"]

    return {
        "repos": f"{len(owned):,}",
        "contributed": f"{user['repositoriesContributedTo']['totalCount']:,}",
        "stars": f"{sum(r['stargazerCount'] for r in repos):,}",
        "followers": f"{user['followers']['totalCount']:,}",
        "commits": f"{commits:,}",
        "account_age": humanize_age(created, now),
        "updated": now.strftime("%Y-%m-%d"),
    }


def humanize_age(start, now):
    years = now.year - start.year
    months = now.month - start.month
    days = now.day - start.day
    if days < 0:
        months -= 1
        previous = (now.replace(day=1) - dt.timedelta(days=1)).day
        days += previous
    if months < 0:
        years -= 1
        months += 12
    parts = []
    if years:
        parts.append(f"{years} year{'s' if years != 1 else ''}")
    if months:
        parts.append(f"{months} month{'s' if months != 1 else ''}")
    parts.append(f"{days} day{'s' if days != 1 else ''}")
    return ", ".join(parts)


# ---------------------------------------------------------------------- Cards

def parse_profile(text, stats):
    """profile.txt -> list of ('title'|'section'|'row'|'gap', payload)."""
    lines = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("#"):
            continue
        if not line.strip():
            if lines and lines[-1][0] != "gap":
                lines.append(("gap", None))
            continue
        line = line.format(**stats)
        if line.startswith("@ "):
            lines.append(("title", line[2:].strip()))
        elif line.startswith("[") and line.endswith("]"):
            lines.append(("section", line[1:-1].strip()))
        elif ":" in line:
            key, value = line.split(":", 1)
            lines.append(("row", (key.strip(), value.strip())))
        else:
            raise SystemExit(f"profile.txt: cannot parse {raw!r}")
    while lines and lines[-1][0] == "gap":
        lines.pop()
    return lines


def escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def column_width(entries):
    """Widest row, in characters, with room for at least three dot leaders."""
    widest = 0
    for kind, payload in entries:
        if kind == "row":
            key, value = payload
            widest = max(widest, len(key) + len(value) + 8)
        elif kind in ("title", "section"):
            widest = max(widest, len(payload) + 6)
    return widest


DELTA = re.compile(r"(\d[\d,]*(?:\+\+|--))")


def render_value(value):
    """Colour the ++ / -- deltas the way git does."""
    out = []
    for chunk in DELTA.split(value):
        if not chunk:
            continue
        if DELTA.fullmatch(chunk):
            klass = "addColor" if chunk.endswith("++") else "delColor"
            out.append(f'<tspan class="{klass}">{escape(chunk)}</tspan>')
        else:
            out.append(f'<tspan class="value">{escape(chunk)}</tspan>')
    return "".join(out)


def render_rows(entries, x, width):
    spans, y = [], TXT_Y
    for kind, payload in entries:
        if kind == "gap":
            spans.append(f'<tspan x="{x}" y="{y}" class="cc">. </tspan>')
            y += TXT_LINE
            continue
        if kind in ("title", "section"):
            label = f"{payload} " if kind == "title" else f"- {payload} "
            rule = "—" * max(3, width - len(label) - 3)
            spans.append(
                f'<tspan x="{x}" y="{y}">{escape(label)}</tspan>'
                f'<tspan class="cc">{rule}-—-</tspan>')
            y += TXT_LINE
            continue
        key, value = payload
        dots = max(3, width - len(key) - len(value) - 5)
        key_markup = ".".join(
            f'<tspan class="key">{escape(part)}</tspan>' for part in key.split("."))
        spans.append(
            f'<tspan x="{x}" y="{y}" class="cc">. </tspan>{key_markup}'
            f'<tspan>:</tspan><tspan class="cc"> {"." * dots} </tspan>{render_value(value)}')
        y += TXT_LINE
    return spans, y


def build_svg(theme, art_lines, entries):
    width_chars = column_width(entries)
    text_x = ART_X + int(max(len(line) for line in art_lines) * ART_FONT * 0.6) + GUTTER
    width = text_x + int(width_chars * TXT_FONT * 0.6) + PAD
    spans, text_bottom = render_rows(entries, text_x, width_chars)
    height = max(ART_Y + len(art_lines) * ART_LINE, text_bottom) + PAD

    art_spans = "\n".join(
        f'<tspan x="{ART_X}" y="{ART_Y + index * ART_LINE}">{escape(line)}</tspan>'
        for index, line in enumerate(art_lines))

    return f"""<?xml version='1.0' encoding='UTF-8'?>
<svg xmlns="http://www.w3.org/2000/svg" width="{width}px" height="{height}px"
     font-family="ConsolasFallback,Consolas,Menlo,'DejaVu Sans Mono',monospace">
<style>
@font-face {{
  src: local('Consolas'), local('Consolas Bold');
  font-family: 'ConsolasFallback';
  font-display: swap;
  size-adjust: 109%;
}}
.key {{fill: {theme['key']};}}
.value {{fill: {theme['value']};}}
.addColor {{fill: {theme['add']};}}
.delColor {{fill: {theme['del']};}}
.cc {{fill: {theme['dots']};}}
text, tspan {{white-space: pre;}}
</style>
<rect width="{width}px" height="{height}px" fill="{theme['bg']}" rx="15"/>
<text x="{ART_X}" y="{ART_Y}" fill="{theme['text']}" font-size="{ART_FONT}px" xml:space="preserve">
{art_spans}
</text>
<text x="{text_x}" y="{TXT_Y}" fill="{theme['text']}" font-size="{TXT_FONT}px" xml:space="preserve">
{chr(10).join(spans)}
</text>
</svg>
"""


def main():
    if not TOKEN:
        raise SystemExit("set ACCESS_TOKEN (or GITHUB_TOKEN) before running")
    art_lines = (ROOT / "ascii_art.txt").read_text(encoding="utf-8").rstrip("\n").split("\n")
    entries = parse_profile((ROOT / "profile.txt").read_text(encoding="utf-8"), collect())
    for filename, theme in THEMES.items():
        (ROOT / filename).write_text(build_svg(theme, art_lines, entries), encoding="utf-8")
        print(f"wrote {filename}")


if __name__ == "__main__":
    main()
