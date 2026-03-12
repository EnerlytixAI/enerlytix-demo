"""
Enerlytix AI — GitHub Inline PR Review Agent  (v2 — line+side API)
===================================================================
Posts line-level review comments with one-click committable suggestions.

KEY FIX vs v1:
  v1 used GitHub's old "position" field (unified diff line counter) which
  silently drops comments when the counter is off by even 1.

  v2 uses GitHub's newer "line" + "side" fields:
    - line  = actual file line number (what you see in the PR)
    - side  = "RIGHT" (new version of file)
  This is far more reliable and is what GitHub Copilot uses.

Flow:
  1. Fetch PR diff from GitHub API
  2. Parse diff → collect the set of NEW line numbers that are in the diff
  3. Send diff to Claude, tell it EXACTLY which line numbers are valid
  4. Claude returns JSON with line-level comments referencing those lines
  5. Submit a single PR Review with all inline comments + summary body
"""

import os, json, re, sys
import requests

# ── Env ───────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GITHUB_TOKEN      = os.environ["GITHUB_TOKEN"]
REPO_NAME         = os.environ["REPO_NAME"]
PR_NUMBER         = int(os.environ["PR_NUMBER"])
HEAD_SHA          = os.environ["HEAD_SHA"]
TDD_PATH          = os.environ.get("TDD_PATH", "")
STANDARDS_PATH    = os.environ.get("STANDARDS_PATH", "")

REVIEWABLE_EXTENSIONS = {".jvs", ".java", ".py", ".sql", ".js", ".ts", ".sh"}
MAX_FILES             = 8
MAX_FILE_DIFF_CHARS   = 10000

GH_HEADERS = {
    "Authorization":        f"Bearer {GITHUB_TOKEN}",
    "Accept":               "application/vnd.github.v3+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


# ══════════════════════════════════════════════════════════════════
#  PART 1 — DIFF PARSING
#  We only need the SET of new-file line numbers that appear in the
#  diff (added "+" lines and context " " lines).
#  Claude must reference one of these lines — no guessing.
# ══════════════════════════════════════════════════════════════════

def fetch_pr_files() -> list[dict]:
    url  = f"https://api.github.com/repos/{REPO_NAME}/pulls/{PR_NUMBER}/files"
    resp = requests.get(url, headers=GH_HEADERS, params={"per_page": 100})
    resp.raise_for_status()
    return resp.json()


def parse_new_lines(patch: str) -> set[int]:
    """
    Return the set of new-file line numbers visible in the diff.
    These are the only lines we can post inline comments on via the
    line+side API (side=RIGHT means the new version of the file).
    """
    valid = set()
    if not patch:
        return valid

    current_new_line = 0
    for raw in patch.splitlines():
        hunk = re.match(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@', raw)
        if hunk:
            current_new_line = int(hunk.group(1)) - 1
            continue
        if raw.startswith('+'):
            current_new_line += 1
            valid.add(current_new_line)   # added line — commentable
        elif raw.startswith('-'):
            pass                           # deleted line — no new-file number
        elif raw.startswith('\\'):
            pass                           # "No newline at end of file"
        else:
            current_new_line += 1
            valid.add(current_new_line)   # context line — also commentable

    return valid


def build_file_maps(pr_files: list[dict]) -> dict:
    """Return per-file data including patch, valid line set, stats."""
    result = {}
    count  = 0
    for f in pr_files:
        if count >= MAX_FILES:
            break
        path   = f["filename"]
        status = f["status"]
        if status == "removed":
            continue
        if not any(path.lower().endswith(ext) for ext in REVIEWABLE_EXTENSIONS):
            continue

        patch = f.get("patch", "")[:MAX_FILE_DIFF_CHARS]
        valid = parse_new_lines(patch)
        if not valid:
            continue   # no commentable lines

        result[path] = {
            "status":    status,
            "patch":     patch,
            "valid":     sorted(valid),   # sorted list for the prompt
            "additions": f.get("additions", 0),
            "deletions": f.get("deletions", 0),
        }
        count += 1
    return result


# ══════════════════════════════════════════════════════════════════
#  PART 2 — CLAUDE PROMPT
# ══════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """You are a senior Endur OpenJVS code reviewer with 15+ years of experience
in energy trading systems (Endur/ETRM, OpenJVS, commodity risk, TPM/APM workflows).

You review GitHub Pull Request diffs and return ONLY valid JSON — no markdown, no explanation.

Critical rules:
- "line" MUST be a number from the exact list of valid line numbers provided for that file
- Do NOT invent line numbers — only use lines from the provided list
- "suggested_code" must be ONLY the replacement line(s) — no fences, no explanations
- Keep "body" concise and actionable (2–3 sentences)
- Focus on Endur-specific issues: hardcoded IDs, memory leaks (missing Table.destroy),
  SQL injection via string concat, missing OException handlers, missing null checks
"""


def build_prompt(file_maps: dict, tdd: str, standards: str) -> str:
    sections = ""
    for path, data in file_maps.items():
        sections += f"""
## File: `{path}`  (+{data['additions']} -{data['deletions']})
VALID LINE NUMBERS YOU MAY USE: {data['valid']}

```diff
{data['patch']}
```
"""

    optional = ""
    if tdd:
        optional += f"\n## TDD Requirements\n{tdd[:3000]}\n"
    if standards:
        optional += f"\n## Coding Standards\n{standards[:2000]}\n"

    return f"""Review this Pull Request diff. Return ONLY a JSON object — no markdown fences.

{optional}
{sections}

Return this exact JSON structure:
{{
  "summary":  "2-3 sentence overall assessment",
  "verdict":  "APPROVED" | "CHANGES_REQUESTED" | "COMMENT",
  "score":    <integer 1-10>,
  "stats":    {{ "critical": <n>, "warnings": <n>, "suggestions": <n> }},
  "comments": [
    {{
      "file":           "<exact filename>",
      "line":           <integer — MUST be in that file's VALID LINE NUMBERS list>,
      "severity":       "critical" | "warning" | "suggestion",
      "title":          "<issue title, max 60 chars>",
      "body":           "<explanation, 2-3 sentences>",
      "suggested_code": "<replacement code for that line only — no fences>"
    }}
  ]
}}

IMPORTANT:
- Every "line" value must come from the VALID LINE NUMBERS list for that file
- Aim for 2-6 comments total — only the most impactful issues
- suggested_code is the corrected replacement for ONLY the lines at that position
"""


def call_claude(prompt: str) -> dict:
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    msg = client.messages.create(
        model      = "claude-sonnet-4-6",
        max_tokens = 4000,
        system     = SYSTEM_PROMPT,
        messages   = [{"role": "user", "content": prompt}],
    )

    raw = msg.content[0].text.strip()
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```$',          '', raw.strip())

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"⚠️  JSON parse error: {e}\nRaw:\n{raw[:400]}")
        return {
            "summary": "Review completed but JSON parse failed.",
            "verdict": "COMMENT", "score": 5,
            "stats":   {"critical": 0, "warnings": 0, "suggestions": 0},
            "comments": []
        }


# ══════════════════════════════════════════════════════════════════
#  PART 3 — COMMENT FORMATTING
# ══════════════════════════════════════════════════════════════════

ICONS = { "critical": "🔴", "warning": "🟡", "suggestion": "🔵" }
LABELS = { "critical": "**Critical**", "warning": "**Warning**", "suggestion": "Suggestion" }


def format_inline_body(c: dict) -> str:
    icon  = ICONS.get(c["severity"], "ℹ️")
    label = LABELS.get(c["severity"], "Note")
    title = c.get("title", "Issue found")
    body  = c.get("body", "")
    fix   = c.get("suggested_code", "").strip()

    lines = [f"{icon} {label}: **{title}**", "", body]

    if fix:
        lines += [
            "",
            "**Suggested fix** *(click **Commit suggestion** to apply)*:",
            "```suggestion",
            fix,
            "```",
        ]
    return "\n".join(lines)


def format_summary(review: dict, file_maps: dict) -> str:
    stats   = review.get("stats", {})
    verdict = review.get("verdict", "COMMENT")
    score   = review.get("score", "-")
    summary = review.get("summary", "")

    badge = {
        "APPROVED":          "✅ **APPROVED**",
        "CHANGES_REQUESTED": "❌ **CHANGES REQUESTED**",
        "COMMENT":           "💬 **REVIEW COMMENT**",
    }.get(verdict, "💬 **REVIEW COMMENT**")

    files = "\n".join(
        f"- `{p}` (+{d['additions']} -{d['deletions']})"
        for p, d in file_maps.items()
    )

    return f"""<!-- enerlytix-review-summary -->
<details open>
<summary><strong>🤖 Enerlytix AI — Endur SME Review &nbsp;|&nbsp; {badge} &nbsp;|&nbsp; Score: {score}/10</strong></summary>

### Review Dashboard
| | Count |
|---|---|
| 🔴 Critical Issues | {stats.get('critical', 0)} |
| 🟡 Warnings | {stats.get('warnings', 0)} |
| 🔵 Suggestions | {stats.get('suggestions', 0)} |
| 📊 Overall Score | {score}/10 |
| Verdict | {badge} |

### Summary
{summary}

### Files Analysed
{files}

---
> Inline comments with suggested fixes are posted directly on the changed lines below.
> Click **Commit suggestion** on any suggestion to apply it immediately.

<sub>Powered by Enerlytix AI · Claude Sonnet · [Docs](https://github.com/EnerlytixAI)</sub>
</details>"""


# ══════════════════════════════════════════════════════════════════
#  PART 4 — GITHUB API
# ══════════════════════════════════════════════════════════════════

def delete_old_summary_comment():
    url    = f"https://api.github.com/repos/{REPO_NAME}/issues/{PR_NUMBER}/comments"
    resp   = requests.get(url, headers=GH_HEADERS, params={"per_page": 100})
    marker = "<!-- enerlytix-review-summary -->"
    if not resp.ok:
        return
    for c in resp.json():
        if marker in (c.get("body") or ""):
            requests.delete(
                f"https://api.github.com/repos/{REPO_NAME}/issues/comments/{c['id']}",
                headers=GH_HEADERS
            )


def delete_old_review_comments():
    """Remove previous Enerlytix inline review comments on this PR."""
    url  = f"https://api.github.com/repos/{REPO_NAME}/pulls/{PR_NUMBER}/comments"
    resp = requests.get(url, headers=GH_HEADERS, params={"per_page": 100})
    if not resp.ok:
        return
    marker = "Enerlytix AI"
    for c in resp.json():
        if marker in (c.get("body") or ""):
            requests.delete(
                f"https://api.github.com/repos/{REPO_NAME}/pulls/comments/{c['id']}",
                headers=GH_HEADERS
            )


def post_summary_comment(body: str):
    url  = f"https://api.github.com/repos/{REPO_NAME}/issues/{PR_NUMBER}/comments"
    resp = requests.post(url, headers=GH_HEADERS, json={"body": body})
    if resp.ok:
        print(f"✅ Summary comment posted (id: {resp.json().get('id')})")
    else:
        print(f"⚠️  Summary comment failed: {resp.status_code} {resp.text[:200]}")


def post_inline_comment(path: str, line: int, body: str) -> bool:
    """
    Post a single inline PR review comment using the line+side API.
    side=RIGHT means the new version of the file (what was added/kept).
    Returns True on success.
    """
    payload = {
        "body":      body,
        "commit_id": HEAD_SHA,
        "path":      path,
        "line":      line,
        "side":      "RIGHT",
    }
    url  = f"https://api.github.com/repos/{REPO_NAME}/pulls/{PR_NUMBER}/comments"
    resp = requests.post(url, headers=GH_HEADERS, json=payload)

    if resp.ok:
        return True
    else:
        print(f"   ⚠️  Inline comment failed on {path}:{line} → {resp.status_code}: {resp.text[:200]}")
        return False


# ══════════════════════════════════════════════════════════════════
#  PART 5 — MAIN
# ══════════════════════════════════════════════════════════════════

def load_doc(path: str) -> str:
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path, errors="replace") as f:
            return f.read()[:5000]
    except Exception:
        return ""


def main():
    print(f"🔍 Enerlytix Inline Review v2 — PR #{PR_NUMBER} on {REPO_NAME}")

    # 1. Fetch PR diff
    print("📥 Fetching PR diff...")
    pr_files  = fetch_pr_files()
    file_maps = build_file_maps(pr_files)

    if not file_maps:
        print("ℹ️  No reviewable files changed — skipping.")
        sys.exit(0)

    print(f"📁 Files to review: {list(file_maps.keys())}")
    for path, data in file_maps.items():
        print(f"   {path}: valid lines = {data['valid']}")

    # 2. Load optional docs
    tdd       = load_doc(TDD_PATH)
    standards = load_doc(STANDARDS_PATH)

    # 3. Call Claude
    print("🤖 Calling Claude...")
    prompt = build_prompt(file_maps, tdd, standards)
    review = call_claude(prompt)

    print(f"✅ Claude: {review.get('verdict')} | score: {review.get('score')}/10 | comments: {len(review.get('comments', []))}")

    # 4. Clean up old comments
    print("🧹 Removing old review comments...")
    delete_old_summary_comment()
    delete_old_review_comments()

    # 5. Post summary comment
    summary_body = format_summary(review, file_maps)
    post_summary_comment(summary_body)

    # 6. Post inline comments one by one using line+side API
    posted  = 0
    skipped = 0

    for c in review.get("comments", []):
        filepath = c.get("file", "")
        line_no  = c.get("line")

        if filepath not in file_maps:
            print(f"   ⚠️  Unknown file: {filepath} — skipping")
            skipped += 1
            continue

        valid_lines = file_maps[filepath]["valid"]

        # Snap to nearest valid line if Claude picked one just outside the diff
        if line_no not in valid_lines:
            if not valid_lines:
                skipped += 1
                continue
            nearest = min(valid_lines, key=lambda x: abs(x - (line_no or 0)))
            print(f"   ℹ️  Line {line_no} not in diff for {filepath} → snapping to {nearest}")
            line_no = nearest

        body = format_inline_body(c)
        ok   = post_inline_comment(filepath, line_no, body)
        if ok:
            posted += 1
            print(f"   ✅ Comment posted: {filepath}:{line_no} [{c.get('severity')}]")
        else:
            skipped += 1

    # 7. Final stats
    stats = review.get("stats", {})
    print(f"\n📊 Review complete:")
    print(f"   🔴 Critical  : {stats.get('critical', 0)}")
    print(f"   🟡 Warnings  : {stats.get('warnings', 0)}")
    print(f"   🔵 Suggest   : {stats.get('suggestions', 0)}")
    print(f"   Score        : {review.get('score')}/10")
    print(f"   Verdict      : {review.get('verdict')}")
    print(f"   Inline posted: {posted} | skipped: {skipped}")


if __name__ == "__main__":
    main()
