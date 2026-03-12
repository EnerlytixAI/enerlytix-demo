"""
Enerlytix AI — GitHub Inline PR Review Agent  (v3 — Full File Review)
======================================================================
KEY FIXES vs v2:
  1. Fetches FULL file content (not just the diff) — finds ALL issues in the file
  2. Exhaustive Senior Endur developer system prompt — 20+ issue categories
  3. Removed the "2-6 comments" limit — reports every issue found
  4. Inline comments placed on diff lines; off-diff issues go into summary
  5. max_tokens raised to 6000
"""

import os, json, re, sys, base64
import requests

# ── Env ───────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GITHUB_TOKEN      = os.environ["GITHUB_TOKEN"]
REPO_NAME         = os.environ["REPO_NAME"]
PR_NUMBER         = int(os.environ["PR_NUMBER"])
HEAD_SHA          = os.environ["HEAD_SHA"]
HEAD_REF          = os.environ.get("HEAD_REF", "")
TDD_PATH          = os.environ.get("TDD_PATH", "")
STANDARDS_PATH    = os.environ.get("STANDARDS_PATH", "")

REVIEWABLE_EXTENSIONS = {".jvs", ".java", ".py", ".sql", ".js", ".ts", ".sh"}
MAX_FILES             = 8
MAX_FILE_CHARS        = 18000   # full file content cap per file

GH_HEADERS = {
    "Authorization":        f"Bearer {GITHUB_TOKEN}",
    "Accept":               "application/vnd.github.v3+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


# ══════════════════════════════════════════════════════════════════
#  PART 1 — FETCH DIFF + FULL FILE CONTENT
# ══════════════════════════════════════════════════════════════════

def fetch_pr_files() -> list[dict]:
    url  = f"https://api.github.com/repos/{REPO_NAME}/pulls/{PR_NUMBER}/files"
    resp = requests.get(url, headers=GH_HEADERS, params={"per_page": 100})
    resp.raise_for_status()
    return resp.json()


def fetch_full_file(path: str) -> str:
    """Fetch the full current content of a file from the PR head branch."""
    url  = f"https://api.github.com/repos/{REPO_NAME}/contents/{path}"
    params = {"ref": HEAD_SHA}
    resp = requests.get(url, headers=GH_HEADERS, params=params)
    if not resp.ok:
        return ""
    data = resp.json()
    content_b64 = data.get("content", "")
    try:
        return base64.b64decode(content_b64).decode("utf-8", errors="replace")[:MAX_FILE_CHARS]
    except Exception:
        return ""


def parse_new_lines(patch: str) -> set[int]:
    """Return set of new-file line numbers that appear in the diff (added + context)."""
    valid = set()
    if not patch:
        return valid
    current_new = 0
    for raw in patch.splitlines():
        hunk = re.match(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@', raw)
        if hunk:
            current_new = int(hunk.group(1)) - 1
            continue
        if raw.startswith('+'):
            current_new += 1
            valid.add(current_new)
        elif raw.startswith('-'):
            pass
        elif raw.startswith('\\'):
            pass
        else:
            current_new += 1
            valid.add(current_new)
    return valid


def build_file_maps(pr_files: list[dict]) -> dict:
    result = {}
    count  = 0
    for f in pr_files:
        if count >= MAX_FILES:
            break
        path   = f["filename"]
        if f["status"] == "removed":
            continue
        if not any(path.lower().endswith(ext) for ext in REVIEWABLE_EXTENSIONS):
            continue

        patch       = f.get("patch", "")
        valid_lines = parse_new_lines(patch)
        full_content = fetch_full_file(path)

        result[path] = {
            "status":      f["status"],
            "patch":       patch[:10000],
            "full":        full_content,
            "valid":       sorted(valid_lines),
            "additions":   f.get("additions", 0),
            "deletions":   f.get("deletions", 0),
        }
        count += 1
    return result


# ══════════════════════════════════════════════════════════════════
#  PART 2 — CLAUDE PROMPT (exhaustive Senior Endur reviewer)
# ══════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """You are a Principal Endur/OpenJVS developer with 15+ years of hands-on experience
delivering commodity trading and risk management systems at Tier 1 energy companies.
You have deep expertise in OpenJVS scripting, Endur TPM/APM workflows, JVS memory model,
Endur database patterns, and enterprise Java/OpenJVS integration.

You conduct THOROUGH, FORENSIC code reviews — not surface-level checks.
Your job is to find EVERY issue in the code, the way a principal engineer would
before approving a change to a live production trading system.

You return ONLY valid JSON. No markdown fences, no preamble, no explanation outside the JSON.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ENDUR/JVS ISSUE CATEGORIES — CHECK ALL OF THESE:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1.  MEMORY MANAGEMENT (CRITICAL in JVS)
    - Every Table created must call Table.destroy() — missing destroys cause heap leaks
      that crash long-running Endur processes
    - Tables created inside loops MUST be destroyed at the bottom of the loop body
    - DBase result tables must be destroyed after use
    - Vector/Table created in methods must be destroyed in finally blocks

2.  EXCEPTION HANDLING
    - All database calls must be wrapped in try/catch(OException e)
    - Never swallow exceptions silently — always log or rethrow
    - Missing finally blocks where Table.destroy() should live
    - Catching generic Exception instead of OException is an anti-pattern

3.  SQL INJECTION & QUERY SAFETY
    - String concatenation to build SQL is a CRITICAL security flaw
    - User input or tran_num/ins_num values must use parameterised queries
    - SELECT * is forbidden — always select explicit columns
    - Missing WHERE clause constraints can return millions of rows
    - No row limit (ROWNUM / TOP) on queries that could return large sets

4.  HARDCODED VALUES (CRITICAL — breaks across environments)
    - Hardcoded portfolio IDs, book IDs, legal entity IDs, or instrument IDs
    - Hardcoded server names, database names, or connection strings
    - Hardcoded user names or party names
    - All environment-specific values must come from constants, config tables,
      or Endur USER_CONST / USER_TABLE lookups

5.  NULL AND BOUNDS CHECKING
    - Accessing row 1 of a Table without checking getNumRows() > 0 first
    - No null check before calling methods on objects returned from API
    - Missing check on DBase.runSql() return code before using the table
    - Array/Vector access without bounds checking

6.  LOOP AND ITERATION PATTERNS
    - Performing a DB query INSIDE a loop (N+1 query pattern) — must be batched
    - Creating objects inside loops without destroying them (memory leak)
    - Loop index starting at wrong value (JVS Tables are 1-indexed, not 0-indexed)
    - Modifying a collection while iterating over it

7.  ENDUR API ANTI-PATTERNS
    - Using deprecated Endur API methods
    - Calling Transaction.retrieveField() in a loop instead of batching
    - Missing OConsole.print() vs proper Endur logging
    - Not using Util.exitFail() / Util.exitSucceed() for script termination
    - Missing @ScriptAttributes annotation in JVS scripts

8.  TRANSACTION INTEGRITY
    - DB operations without transaction handling (missing commit/rollback)
    - No idempotency check before inserting records
    - Missing duplicate trade detection logic
    - Updating trade status without checking current state first

9.  PERFORMANCE
    - Fetching entire tables when only a few columns/rows are needed
    - Missing indexes hint comments on heavy queries
    - Repeated calls to the same slow API in a loop
    - Large result sets loaded entirely into memory

10. CODE QUALITY & MAINTAINABILITY
    - Magic numbers with no named constant or comment explaining them
    - Dead code, commented-out blocks, TODO/FIXME left in production code
    - Methods that are too long (>50 lines) and do too many things
    - Poor variable names (a, b, x, temp, data)
    - Missing Javadoc/comments on public methods
    - Inconsistent error message formatting

11. ENDUR WORKFLOW SPECIFIC
    - TPM/APM task scripts not handling all required task states
    - Missing pre-process / post-process checks
    - Not validating trade dates against settlement calendars
    - Missing business date vs system date distinction
    - Position table updates without proper locking

12. DATA VALIDATION
    - No validation that notional/quantity/price values are positive and non-zero
    - Division without checking the divisor is non-zero first
    - Date range validation missing (start date before end date)
    - Currency/unit of measure not validated

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REVIEW RULES:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Review the ENTIRE FILE CONTENT provided, not just changed lines
- Report EVERY issue you find — do not self-censor or limit to "most important"
- For each issue, reference the exact line number from the file
- If the line is in the diff (provided as VALID INLINE LINES), use it as an inline comment
- If the line is NOT in the diff, still report it — set "inline": false
- "suggested_code" must be the corrected replacement line(s) only, no fences
- severity: "critical" = must fix before merge, "warning" = should fix, "suggestion" = nice to have
"""


def build_prompt(file_maps: dict, tdd: str, standards: str) -> str:
    sections = ""
    for path, data in file_maps.items():
        # Annotate the full file with line numbers so Claude can reference them precisely
        numbered = "\n".join(
            f"{i+1:4d} | {line}"
            for i, line in enumerate(data["full"].splitlines())
        )
        sections += f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FILE: `{path}`   status={data['status']}   +{data['additions']} -{data['deletions']}
VALID INLINE LINES (in the diff — use these for inline=true comments):
{data['valid']}

FULL FILE CONTENT (numbered — review every line):
{numbered}

DIFF (what changed in this PR):
```diff
{data['patch'][:4000]}
```
"""

    optional = ""
    if tdd:
        optional += f"\n## TDD / Requirements\n{tdd[:3000]}\n"
    if standards:
        optional += f"\n## Coding Standards\n{standards[:2000]}\n"

    return f"""You are reviewing the following Endur/OpenJVS code.
Review the ENTIRE file — not just the diff. Find every issue.

{optional}
{sections}

Return ONLY a JSON object with this exact structure (no markdown fences):
{{
  "summary":  "Thorough 4-6 sentence assessment covering all major issues found",
  "verdict":  "APPROVED" | "CHANGES_REQUESTED" | "COMMENT",
  "score":    <integer 1-10>,
  "stats":    {{ "critical": <n>, "warnings": <n>, "suggestions": <n> }},
  "comments": [
    {{
      "file":           "<exact filename>",
      "line":           <integer — exact line number from the numbered file content>,
      "inline":         <true if line is in VALID INLINE LINES list, false otherwise>,
      "severity":       "critical" | "warning" | "suggestion",
      "title":          "<concise issue title, max 60 chars>",
      "body":           "<explain the issue clearly: what is wrong, why it matters in Endur, what the risk is>",
      "suggested_code": "<corrected replacement line(s) only — no fences, no explanations>"
    }}
  ]
}}

CRITICAL RULES:
- Report EVERY issue found — do not limit yourself
- Line numbers must match the numbered file content exactly
- inline=true ONLY if that line number is in the VALID INLINE LINES list
- suggested_code replaces ONLY that specific line — keep it syntactically valid
- Do not report the same issue twice on different lines
"""


def call_claude(prompt: str) -> dict:
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    msg = client.messages.create(
        model      = "claude-sonnet-4-6",
        max_tokens = 6000,
        system     = SYSTEM_PROMPT,
        messages   = [{"role": "user", "content": prompt}],
    )

    raw = msg.content[0].text.strip()
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```$',          '', raw.strip())

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"⚠️  JSON parse error: {e}\nRaw (first 600 chars):\n{raw[:600]}")
        return {
            "summary": "Review completed but response format error. Check Actions log.",
            "verdict": "COMMENT", "score": 5,
            "stats":   {"critical": 0, "warnings": 0, "suggestions": 0},
            "comments": []
        }


# ══════════════════════════════════════════════════════════════════
#  PART 3 — COMMENT FORMATTING
# ══════════════════════════════════════════════════════════════════

ICONS  = {"critical": "🔴", "warning": "🟡", "suggestion": "🔵"}
LABELS = {"critical": "**Critical**", "warning": "**Warning**", "suggestion": "Suggestion"}


def format_inline_body(c: dict) -> str:
    icon  = ICONS.get(c["severity"], "ℹ️")
    label = LABELS.get(c["severity"], "Note")
    body  = c.get("body", "")
    fix   = c.get("suggested_code", "").strip()

    lines = [f"{icon} {label}: **{c.get('title', 'Issue')}**", "", body]
    if fix:
        lines += [
            "",
            "**Suggested fix** *(click **Commit suggestion** to apply)*:",
            "```suggestion",
            fix,
            "```",
        ]
    return "\n".join(lines)


def format_summary(review: dict, file_maps: dict, off_diff_issues: list) -> str:
    stats   = review.get("stats", {})
    verdict = review.get("verdict", "COMMENT")
    score   = review.get("score", "-")

    badge = {
        "APPROVED":          "✅ **APPROVED**",
        "CHANGES_REQUESTED": "❌ **CHANGES REQUESTED**",
        "COMMENT":           "💬 **REVIEW COMMENT**",
    }.get(verdict, "💬 **REVIEW COMMENT**")

    files = "\n".join(
        f"- `{p}` (+{d['additions']} -{d['deletions']})"
        for p, d in file_maps.items()
    )

    # Off-diff issues section (issues on lines not in this PR's diff)
    off_diff_section = ""
    if off_diff_issues:
        off_diff_section = "\n### ⚠️ Issues Found in Existing Code (outside this diff)\n"
        off_diff_section += "_These issues exist in the file but are on lines not changed in this PR. They should be addressed in a follow-up._\n\n"
        for c in off_diff_issues:
            icon  = ICONS.get(c.get("severity"), "ℹ️")
            label = LABELS.get(c.get("severity"), "Note")
            off_diff_section += (
                f"**{icon} {label} — Line {c.get('line')}: {c.get('title')}**\n"
                f"{c.get('body', '')}\n"
            )
            fix = c.get("suggested_code", "").strip()
            if fix:
                off_diff_section += f"```suggestion\n{fix}\n```\n"
            off_diff_section += "\n---\n"

    return f"""<!-- enerlytix-review-summary -->
<details open>
<summary><strong>🤖 Enerlytix AI — Senior Endur SME Review &nbsp;|&nbsp; {badge} &nbsp;|&nbsp; Score: {score}/10</strong></summary>

### Review Dashboard
| | Count |
|---|---|
| 🔴 Critical Issues | {stats.get('critical', 0)} |
| 🟡 Warnings | {stats.get('warnings', 0)} |
| 🔵 Suggestions | {stats.get('suggestions', 0)} |
| 📊 Overall Score | {score}/10 |
| Verdict | {badge} |

### Summary
{review.get('summary', '')}

### Files Analysed
{files}
{off_diff_section}
---
> Inline comments with suggested fixes are posted on the changed lines in **Files changed** tab.
> Click **Commit suggestion** on any suggestion to apply it immediately.

<sub>Powered by Enerlytix AI · Claude Sonnet · Full-file review</sub>
</details>"""


# ══════════════════════════════════════════════════════════════════
#  PART 4 — GITHUB API
# ══════════════════════════════════════════════════════════════════

def delete_old_summary_comment():
    url    = f"https://api.github.com/repos/{REPO_NAME}/issues/{PR_NUMBER}/comments"
    resp   = requests.get(url, headers=GH_HEADERS, params={"per_page": 100})
    if not resp.ok:
        return
    for c in resp.json():
        if "<!-- enerlytix-review-summary -->" in (c.get("body") or ""):
            requests.delete(
                f"https://api.github.com/repos/{REPO_NAME}/issues/comments/{c['id']}",
                headers=GH_HEADERS
            )


def delete_old_inline_comments():
    url  = f"https://api.github.com/repos/{REPO_NAME}/pulls/{PR_NUMBER}/comments"
    resp = requests.get(url, headers=GH_HEADERS, params={"per_page": 100})
    if not resp.ok:
        return
    for c in resp.json():
        if "Enerlytix AI" in (c.get("body") or ""):
            requests.delete(
                f"https://api.github.com/repos/{REPO_NAME}/pulls/comments/{c['id']}",
                headers=GH_HEADERS
            )


def post_summary_comment(body: str):
    url  = f"https://api.github.com/repos/{REPO_NAME}/issues/{PR_NUMBER}/comments"
    resp = requests.post(url, headers=GH_HEADERS, json={"body": body})
    if resp.ok:
        print(f"✅ Summary posted (id: {resp.json().get('id')})")
    else:
        print(f"⚠️  Summary failed: {resp.status_code} {resp.text[:200]}")


def post_inline_comment(path: str, line: int, body: str) -> bool:
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
    print(f"   ⚠️  Inline failed {path}:{line} → {resp.status_code}: {resp.text[:200]}")
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
    print(f"🔍 Enerlytix Inline Review v3 — PR #{PR_NUMBER} on {REPO_NAME}")

    # 1. Fetch PR diff + full file contents
    print("📥 Fetching PR diff + full file contents...")
    pr_files  = fetch_pr_files()
    file_maps = build_file_maps(pr_files)

    if not file_maps:
        print("ℹ️  No reviewable files changed.")
        sys.exit(0)

    for path, data in file_maps.items():
        print(f"   {path}: diff_lines={data['valid']}, full_file={len(data['full'])} chars")

    # 2. Load optional docs + call Claude
    tdd       = load_doc(TDD_PATH)
    standards = load_doc(STANDARDS_PATH)

    print("🤖 Calling Claude (full-file forensic review)...")
    prompt = build_prompt(file_maps, tdd, standards)
    review = call_claude(prompt)

    all_comments = review.get("comments", [])
    print(f"✅ Claude: {review.get('verdict')} | score={review.get('score')}/10 | total issues={len(all_comments)}")

    # 3. Split comments into inline (in diff) vs off-diff
    inline_comments   = []
    off_diff_comments = []

    for c in all_comments:
        filepath = c.get("file", "")
        line_no  = c.get("line")
        is_inline = c.get("inline", False)

        if filepath not in file_maps:
            print(f"   ⚠️  Unknown file: {filepath}")
            off_diff_comments.append(c)
            continue

        valid_lines = file_maps[filepath]["valid"]

        if is_inline and line_no in valid_lines:
            inline_comments.append(c)
        else:
            # Try snapping to nearest valid line if it's close (within 3 lines)
            if valid_lines and line_no:
                nearest = min(valid_lines, key=lambda x: abs(x - line_no))
                if abs(nearest - line_no) <= 3:
                    print(f"   ℹ️  Snapping {filepath}:{line_no} → {nearest}")
                    c["line"] = nearest
                    inline_comments.append(c)
                    continue
            off_diff_comments.append(c)

    print(f"   Inline: {len(inline_comments)} | Off-diff (in summary): {len(off_diff_comments)}")

    # 4. Clean up old comments
    print("🧹 Cleaning up previous review...")
    delete_old_summary_comment()
    delete_old_inline_comments()

    # 5. Post summary (includes off-diff issues)
    summary_body = format_summary(review, file_maps, off_diff_comments)
    post_summary_comment(summary_body)

    # 6. Post inline comments
    posted = 0
    for c in inline_comments:
        body = format_inline_body(c)
        ok   = post_inline_comment(c["file"], c["line"], body)
        if ok:
            posted += 1
            print(f"   ✅ {c['file']}:{c['line']} [{c.get('severity')}] {c.get('title')}")

    # 7. Final summary
    stats = review.get("stats", {})
    print(f"\n📊 Review complete:")
    print(f"   🔴 Critical  : {stats.get('critical', 0)}")
    print(f"   🟡 Warnings  : {stats.get('warnings', 0)}")
    print(f"   🔵 Suggest   : {stats.get('suggestions', 0)}")
    print(f"   Score        : {review.get('score')}/10")
    print(f"   Inline posted: {posted}")
    print(f"   In summary   : {len(off_diff_comments)}")


if __name__ == "__main__":
    main()
