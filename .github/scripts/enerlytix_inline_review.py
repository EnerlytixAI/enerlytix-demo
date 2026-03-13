"""
Enerlytix AI — GitHub Inline PR Review Agent
=============================================
Posts line-level review comments with one-click committable suggestions,
exactly like GitHub Copilot code review.

Flow:
  1. Fetch PR diff from GitHub API
  2. Parse diff → build line→position map for every changed file
  3. Send full diff to Claude, ask for JSON array of line-level comments
  4. Map each comment's line number back to a GitHub diff "position"
  5. Submit a single PR Review with all inline comments + summary
"""

import os, json, re, sys, textwrap
import requests

# ── Env ───────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GITHUB_TOKEN      = os.environ["GITHUB_TOKEN"]
REPO_NAME         = os.environ["REPO_NAME"]
PR_NUMBER         = int(os.environ["PR_NUMBER"])
BASE_REF          = os.environ.get("BASE_REF", "main")
HEAD_SHA          = os.environ["HEAD_SHA"]
TDD_PATH          = os.environ.get("TDD_PATH", "")
STANDARDS_PATH    = os.environ.get("STANDARDS_PATH", "")

REVIEWABLE_EXTENSIONS = {".jvs", ".java", ".py", ".sql", ".js", ".ts", ".sh"}
MAX_FILES             = 8
MAX_FILE_DIFF_CHARS   = 8000

GH_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept":        "application/vnd.github.v3+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


# ══════════════════════════════════════════════════════════════════
#  PART 1 — DIFF PARSING
#  GitHub inline comments need a "position" — the line number
#  within the unified diff hunk, NOT the file line number.
#  We build two maps per file:
#    file_line_to_diff_position[filepath][new_line_no] = diff_position
#    diff_position_to_file_line[filepath][diff_position] = new_line_no
# ══════════════════════════════════════════════════════════════════

def fetch_pr_files() -> list[dict]:
    """Fetch changed file metadata + patches from GitHub API."""
    url  = f"https://api.github.com/repos/{REPO_NAME}/pulls/{PR_NUMBER}/files"
    resp = requests.get(url, headers=GH_HEADERS, params={"per_page": 100})
    resp.raise_for_status()
    return resp.json()


def parse_diff_positions(patch: str) -> dict:
    """
    Parse a unified diff patch and return:
      {
        new_line_no  (int) : diff_position (int),
        ...
      }
    diff_position is the 1-based counter of every line in the diff
    (including hunk headers). GitHub uses this as the 'position' field.
    """
    line_to_pos = {}   # new_line_number → diff_position
    pos_to_line = {}   # diff_position   → new_line_number

    if not patch:
        return line_to_pos

    diff_position = 0
    current_new_line = 0

    for raw_line in patch.splitlines():
        diff_position += 1

        # Hunk header: @@ -a,b +c,d @@
        hunk_match = re.match(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@', raw_line)
        if hunk_match:
            current_new_line = int(hunk_match.group(1)) - 1
            # Hunk headers also count as a diff position (no file line mapping)
            continue

        if raw_line.startswith('+'):
            # Added line — has a new-file line number
            current_new_line += 1
            line_to_pos[current_new_line] = diff_position
            pos_to_line[diff_position]    = current_new_line

        elif raw_line.startswith('-'):
            # Removed line — old file only, no new-file line number
            # We map to the diff_position so we can still comment on deletions
            # GitHub requires commenting on the last visible position in that hunk
            pass

        elif raw_line.startswith('\\'):
            # "\ No newline at end of file" — skip
            pass

        else:
            # Context line — exists in both old and new
            current_new_line += 1
            line_to_pos[current_new_line] = diff_position
            pos_to_line[diff_position]    = current_new_line

    return line_to_pos


def build_file_maps(pr_files: list[dict]) -> dict:
    """
    Returns per-file data including the diff, line map, and file content.
    Only includes reviewable files.
    """
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
        result[path] = {
            "status":       status,
            "patch":        patch,
            "line_to_pos":  parse_diff_positions(patch),
            "additions":    f.get("additions", 0),
            "deletions":    f.get("deletions", 0),
        }
        count += 1
    return result


# ══════════════════════════════════════════════════════════════════
#  PART 2 — CLAUDE PROMPT & PARSING
# ══════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """You are an expert Endur OpenJVS code reviewer with 15+ years of experience
in energy trading systems (Endur/ETRM, OpenJVS, commodity risk, TPM/APM workflows).

You review GitHub Pull Request diffs and return structured JSON — one entry per
specific code issue found, referencing the exact file and line number.

Rules:
- ONLY comment on lines that actually appear in the diff (added or context lines)
- Each comment must reference a real line number from the file
- For every issue, provide a concrete suggested_code fix (the corrected lines)
- suggested_code must be ONLY the replacement line(s) for that exact location —
  no surrounding context, no explanations inside the code block
- severity must be: "critical", "warning", or "suggestion"
- Keep comment text concise and actionable (2-4 sentences max)
- Focus on Endur-specific issues: memory leaks (Table.destroy), SQL injection,
  missing try/catch in OException handlers, hardcoded IDs, missing null checks,
  OpenJVS anti-patterns
"""

def build_claude_prompt(file_maps: dict, tdd: str, standards: str) -> str:
    diff_sections = ""
    for path, data in file_maps.items():
        diff_sections += f"""
## File: `{path}` ({data['status']}, +{data['additions']} -{data['deletions']})
Available line numbers in this file's diff: {sorted(data['line_to_pos'].keys())}

```diff
{data['patch']}
```
"""

    optional = ""
    if tdd:
        optional += f"\n## TDD Requirements\n{tdd[:4000]}\n"
    if standards:
        optional += f"\n## Coding Standards\n{standards[:3000]}\n"

    return f"""Review this Pull Request diff. Return ONLY valid JSON — no markdown fences, no commentary.

{optional}

{diff_sections}

Return a JSON object in EXACTLY this structure:
{{
  "summary": "2-3 sentence overall review summary",
  "verdict": "APPROVED" | "CHANGES_REQUESTED" | "COMMENT",
  "score": <integer 1-10>,
  "stats": {{
    "critical": <count>,
    "warnings": <count>,
    "suggestions": <count>
  }},
  "comments": [
    {{
      "file":           "<exact filename from diff>",
      "line":           <integer — must exist in that file's available line numbers>,
      "severity":       "critical" | "warning" | "suggestion",
      "title":          "<short issue title, max 60 chars>",
      "body":           "<explanation of the issue, 2-4 sentences>",
      "suggested_code": "<replacement code lines only — no markdown fences>"
    }}
  ]
}}

Important:
- "line" must be a real line number from the "Available line numbers" list shown above
- If you cannot find a valid line for an issue, omit that comment
- suggested_code should be the corrected version of ONLY the line(s) at that position
- Aim for 2-8 comments total across all files — only the most important issues
"""


def call_claude(prompt: str) -> dict:
    """Call Claude API, return parsed JSON response."""
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    message = client.messages.create(
        model  = "claude-sonnet-4-6",
        max_tokens = 4000,
        system = SYSTEM_PROMPT,
        messages = [{"role": "user", "content": prompt}]
    )

    raw = message.content[0].text.strip()

    # Strip accidental markdown fences
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```$',          '', raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"⚠️  JSON parse error: {e}")
        print(f"Raw response:\n{raw[:500]}")
        # Return minimal valid structure
        return {
            "summary": "Review completed but response format error.",
            "verdict": "COMMENT",
            "score": 5,
            "stats": {"critical": 0, "warnings": 0, "suggestions": 0},
            "comments": []
        }


# ══════════════════════════════════════════════════════════════════
#  PART 3 — COMMENT FORMATTING
# ══════════════════════════════════════════════════════════════════

SEVERITY_ICONS = {
    "critical":   "🔴",
    "warning":    "🟡",
    "suggestion": "🔵",
}

SEVERITY_LABELS = {
    "critical":   "**Critical**",
    "warning":    "**Warning**",
    "suggestion": "Suggestion",
}


def format_inline_comment(comment: dict) -> str:
    """
    Format a single inline comment body.
    Uses GitHub's suggestion syntax for one-click committable fixes.
    """
    icon     = SEVERITY_ICONS.get(comment["severity"], "ℹ️")
    label    = SEVERITY_LABELS.get(comment["severity"], "Note")
    title    = comment.get("title", "Issue found")
    body     = comment.get("body", "")
    fix_code = comment.get("suggested_code", "").strip()

    lines = [f"{icon} {label}: **{title}**", ""]
    lines.append(body)

    if fix_code:
        lines += [
            "",
            "**Suggested fix** *(click **Commit suggestion** to apply)*:",
            "```suggestion",
            fix_code,
            "```",
        ]

    return "\n".join(lines)


def format_summary_comment(review: dict, file_maps: dict) -> str:
    """
    Top-level PR comment with the dashboard + any comments that
    couldn't be placed inline (e.g. file-level issues).
    """
    stats   = review.get("stats", {})
    verdict = review.get("verdict", "COMMENT")
    score   = review.get("score", "-")
    summary = review.get("summary", "")

    # Verdict badge
    verdict_badge = {
        "APPROVED":          "✅ **APPROVED**",
        "CHANGES_REQUESTED": "❌ **CHANGES REQUESTED**",
        "COMMENT":           "💬 **REVIEW COMMENT**",
    }.get(verdict, "💬 **REVIEW COMMENT**")

    files_reviewed = "\n".join(
        f"- `{path}` (+{d['additions']} -{d['deletions']})"
        for path, d in file_maps.items()
    )

    return f"""<!-- enerlytix-review-summary -->
<details open>
<summary><strong>🤖 Enerlytix AI — Endur SME Review &nbsp;|&nbsp; {verdict_badge} &nbsp;|&nbsp; Score: {score}/10</strong></summary>

### Review Dashboard
| | Count |
|---|---|
| 🔴 Critical Issues | {stats.get('critical', 0)} |
| 🟡 Warnings | {stats.get('warnings', 0)} |
| 🔵 Suggestions | {stats.get('suggestions', 0)} |
| 📊 Overall Score | {score}/10 |
| Verdict | {verdict_badge} |

### Summary
{summary}

### Files Analysed
{files_reviewed}

---
> Inline comments with suggested fixes are posted directly on the changed lines below.
> Click **Commit suggestion** on any suggestion to apply it immediately.

<sub>Powered by Enerlytix AI · Claude Sonnet · [Docs](https://github.com/your-org/enerlytix)</sub>
</details>"""


# ══════════════════════════════════════════════════════════════════
#  PART 4 — GITHUB API CALLS
# ══════════════════════════════════════════════════════════════════

def dismiss_old_reviews():
    """
    Find and dismiss any previous Enerlytix pending reviews on this PR,
    so re-runs don't stack up duplicate inline comments.
    """
    url  = f"https://api.github.com/repos/{REPO_NAME}/pulls/{PR_NUMBER}/reviews"
    resp = requests.get(url, headers=GH_HEADERS)
    if not resp.ok:
        return

    marker = "enerlytix-review-summary"
    for review in resp.json():
        body = review.get("body") or ""
        if marker in body and review.get("state") == "PENDING":
            requests.delete(
                f"{url}/{review['id']}",
                headers=GH_HEADERS
            )


def delete_old_summary_comment():
    """Remove the previous top-level summary comment if it exists."""
    url    = f"https://api.github.com/repos/{REPO_NAME}/issues/{PR_NUMBER}/comments"
    resp   = requests.get(url, headers=GH_HEADERS, params={"per_page": 100})
    marker = "<!-- enerlytix-review-summary -->"
    if not resp.ok:
        return
    for comment in resp.json():
        if marker in (comment.get("body") or ""):
            requests.delete(
                f"https://api.github.com/repos/{REPO_NAME}/issues/comments/{comment['id']}",
                headers=GH_HEADERS
            )


def submit_pr_review(inline_comments: list[dict], review_body: str, verdict: str):
    """
    Submit a single PR Review containing all inline comments at once.
    This is the correct GitHub API — one review, many inline comments.
    """
    # Map verdict to GitHub event type
    event_map = {
        "APPROVED":          "APPROVE",
        "CHANGES_REQUESTED": "REQUEST_CHANGES",
        "COMMENT":           "COMMENT",
    }
    event = event_map.get(verdict, "COMMENT")

    payload = {
        "commit_id": HEAD_SHA,
        "body":      review_body,
        "event":     event,
        "comments":  inline_comments,
    }

    url  = f"https://api.github.com/repos/{REPO_NAME}/pulls/{PR_NUMBER}/reviews"
    resp = requests.post(url, headers=GH_HEADERS, json=payload)

    if not resp.ok:
        print(f"⚠️  Review submission failed: {resp.status_code}")
        print(f"   Response: {resp.text[:500]}")
        # Fall back to plain issue comment
        fallback_url = f"https://api.github.com/repos/{REPO_NAME}/issues/{PR_NUMBER}/comments"
        requests.post(fallback_url, headers=GH_HEADERS, json={"body": review_body})
    else:
        review_id = resp.json().get("id")
        print(f"✅ PR review submitted (id: {review_id}, event: {event})")
        print(f"   Inline comments: {len(inline_comments)}")


# ══════════════════════════════════════════════════════════════════
#  PART 5 — MAIN ORCHESTRATION
# ══════════════════════════════════════════════════════════════════

def load_optional_doc(path: str) -> str:
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()[:6000]
    except Exception:
        return ""


def main():
    print(f"🔍 Enerlytix Inline Review — PR #{PR_NUMBER} on {REPO_NAME}")

    # 1. Fetch PR files and build diff maps
    print("📥 Fetching PR diff from GitHub...")
    pr_files  = fetch_pr_files()
    file_maps = build_file_maps(pr_files)

    if not file_maps:
        print("ℹ️  No reviewable files changed — skipping.")
        sys.exit(0)

    print(f"📁 Files to review: {list(file_maps.keys())}")

    # 2. Load optional docs
    tdd       = load_optional_doc(TDD_PATH)
    standards = load_optional_doc(STANDARDS_PATH)

    # 3. Build prompt and call Claude
    print("🤖 Calling Claude for inline review...")
    prompt    = build_claude_prompt(file_maps, tdd, standards)
    review    = call_claude(prompt)

    print(f"✅ Claude response: {review.get('verdict')} | score: {review.get('score')}/10")
    print(f"   Comments returned: {len(review.get('comments', []))}")

    # 4. Map Claude's line numbers → GitHub diff positions
    inline_comments = []
    skipped         = 0

    for c in review.get("comments", []):
        filepath  = c.get("file", "")
        line_no   = c.get("line")

        if filepath not in file_maps:
            print(f"   ⚠️  Skipping comment — unknown file: {filepath}")
            skipped += 1
            continue

        line_map = file_maps[filepath]["line_to_pos"]

        if line_no not in line_map:
            # Try the nearest valid line in the diff
            valid_lines = sorted(line_map.keys())
            if not valid_lines:
                skipped += 1
                continue
            # Snap to nearest available line
            nearest = min(valid_lines, key=lambda x: abs(x - (line_no or 0)))
            print(f"   ℹ️  Line {line_no} not in diff for {filepath}, snapping to {nearest}")
            line_no = nearest

        diff_position = line_map[line_no]

        inline_comments.append({
            "path":     filepath,
            "position": diff_position,   # GitHub diff position (not file line)
            "body":     format_inline_comment(c),
        })

    print(f"   Inline comments mapped: {len(inline_comments)} | skipped: {skipped}")

    # 5. Build summary comment
    summary_body = format_summary_comment(review, file_maps)

    # 6. Clean up old review comments first
    print("🧹 Cleaning up previous reviews...")
    delete_old_summary_comment()
    dismiss_old_reviews()

    # 7. Submit the PR review with all inline comments
    print("📤 Submitting PR review...")
    submit_pr_review(
        inline_comments = inline_comments,
        review_body     = summary_body,
        verdict         = review.get("verdict", "COMMENT"),
    )

    # 8. Stats
    stats = review.get("stats", {})
    print(f"\n📊 Review complete:")
    print(f"   🔴 Critical : {stats.get('critical', 0)}")
    print(f"   🟡 Warnings : {stats.get('warnings', 0)}")
    print(f"   🔵 Suggest  : {stats.get('suggestions', 0)}")
    print(f"   Score       : {review.get('score')}/10")
    print(f"   Verdict     : {review.get('verdict')}")


if __name__ == "__main__":
    main()
