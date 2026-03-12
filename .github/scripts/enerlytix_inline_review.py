"""
Enerlytix AI — GitHub Inline PR Review Agent  (v4)
===================================================
FIXES in this version:
  1. Inline comments now contain <!-- enerlytix-inline --> hidden marker
     so delete_old_inline_comments() can reliably find and remove them
  2. When Claude marks inline=false but the line is close to a diff line,
     it snaps and posts inline anyway (better coverage)
  3. Full file content is fetched and sent numbered so Claude sees ALL issues
  4. No comment limit — reports every issue found
  5. Exhaustive 12-category Endur issue checklist in system prompt
"""

import os, json, re, sys, base64
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
MAX_FILE_CHARS        = 20000

GH_HEADERS = {
    "Authorization":        f"Bearer {GITHUB_TOKEN}",
    "Accept":               "application/vnd.github.v3+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# Hidden markers — embedded in comment bodies so we can find and delete them
INLINE_MARKER  = "<!-- enerlytix-inline -->"
SUMMARY_MARKER = "<!-- enerlytix-review-summary -->"


# ══════════════════════════════════════════════════════════════════
#  PART 1 — FETCH DIFF + FULL FILE CONTENT
# ══════════════════════════════════════════════════════════════════

def fetch_pr_files() -> list[dict]:
    url  = f"https://api.github.com/repos/{REPO_NAME}/pulls/{PR_NUMBER}/files"
    resp = requests.get(url, headers=GH_HEADERS, params={"per_page": 100})
    resp.raise_for_status()
    return resp.json()


def fetch_full_file(path: str) -> str:
    """Fetch the complete current file from the PR head commit."""
    url  = f"https://api.github.com/repos/{REPO_NAME}/contents/{path}"
    resp = requests.get(url, headers=GH_HEADERS, params={"ref": HEAD_SHA})
    if not resp.ok:
        print(f"   ⚠️  Could not fetch full file {path}: {resp.status_code}")
        return ""
    try:
        raw_b64 = resp.json().get("content", "")
        return base64.b64decode(raw_b64).decode("utf-8", errors="replace")[:MAX_FILE_CHARS]
    except Exception as e:
        print(f"   ⚠️  Decode error for {path}: {e}")
        return ""


def parse_new_lines(patch: str) -> set[int]:
    """
    Return the set of new-file line numbers visible in the diff.
    Includes both added (+) lines and context lines — both are commentable.
    """
    valid        = set()
    current_new  = 0
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
        path = f["filename"]
        if f["status"] == "removed":
            continue
        if not any(path.lower().endswith(ext) for ext in REVIEWABLE_EXTENSIONS):
            continue

        patch        = f.get("patch", "")
        valid_lines  = parse_new_lines(patch)
        full_content = fetch_full_file(path)

        result[path] = {
            "status":    f["status"],
            "patch":     patch[:8000],
            "full":      full_content,
            "valid":     sorted(valid_lines),
            "additions": f.get("additions", 0),
            "deletions": f.get("deletions", 0),
        }
        count += 1
        print(f"   Loaded {path}: {len(full_content)} chars, {len(valid_lines)} diff lines")
    return result


# ══════════════════════════════════════════════════════════════════
#  PART 2 — CLAUDE PROMPT
# ══════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """You are a Principal Endur/OpenJVS engineer with 15+ years of hands-on production
experience at Tier 1 energy trading firms. You have delivered OpenJVS scripts, TPM/APM workflows,
position management systems, and settlement automation on Endur ETRM platforms.

You conduct EXHAUSTIVE, FORENSIC code reviews — you find EVERY bug, every anti-pattern,
every risk. You review code as if a financial regulator is watching and a production outage
is your personal responsibility.

You return ONLY valid JSON. No markdown fences, no preamble outside the JSON.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MANDATORY CHECK LIST — inspect EVERY item for every file:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. MEMORY LEAKS (CRITICAL — most common Endur production crash)
   • Every Table.tableNew() MUST have a matching Table.destroy() — no exceptions
   • Tables created inside loops MUST be destroyed at the end of each loop iteration
   • Tables in methods MUST be destroyed in a finally block
   • Missing destroy() causes Endur process heap exhaustion over time

2. MISSING EXCEPTION HANDLING
   • All DBase calls, Transaction calls, and API calls need try/catch(OException e)
   • Never allow OException to propagate uncaught — always handle or wrap
   • finally blocks must exist to destroy Tables even when exceptions occur
   • Catching generic Exception{} instead of OException is wrong in JVS

3. SQL INJECTION (CRITICAL — security vulnerability)
   • String concatenation in SQL is a hard block: "WHERE x = " + variable
   • Must use parameterised queries or sanitised integer casting
   • SELECT * is forbidden — always name every column explicitly
   • Queries with no row limit on large tables (ab_tran, ab_tran_info) need ROWNUM/TOP

4. HARDCODED ENVIRONMENT-SPECIFIC VALUES (CRITICAL — breaks DEV/UAT/PROD)
   • Hardcoded portfolio IDs, book IDs, legal entity IDs, instrument IDs
   • Any integer constant that looks like an Endur reference data ID
   • These MUST come from Ref.getValue(), a constants table, or USER_CONST lookup
   • Magic numbers with no named constant explaining what they represent

5. NULL CHECKS AND BOUNDS VALIDATION
   • Accessing table.getInt/getDouble/getString at row 1 without checking getNumRows() > 0
   • No null check on objects returned from API before calling methods on them
   • No check on DBase.runSql() return code before using the result table
   • Off-by-one: JVS Table rows are 1-indexed (row 1 = first row, NOT row 0)

6. N+1 DATABASE QUERIES (PERFORMANCE CRITICAL)
   • Running a SQL query inside a for loop — must batch into a single query with IN clause
   • Calling Transaction.retrieveField() inside a loop
   • Any DB call inside iteration over trade/position sets

7. DIVISION BY ZERO
   • Any division where the denominator is a variable (fee/notional, qty/price, etc.)
   • Must validate the denominator is non-zero before dividing

8. DEAD / DEBUG / INCOMPLETE CODE
   • TODO, FIXME, HACK comments left in production code
   • Empty method bodies or stub implementations (implement; return;)
   • Commented-out code blocks
   • Debug OConsole.oprint() calls that expose raw financial values to logs

9. DATA VALIDATION
   • Financial values (notional, quantity, price, fee) not validated as positive non-zero
   • Date range not validated (start before end)
   • No validation of input parameters from IContainerManager before use

10. TRANSACTION AND DATABASE INTEGRITY
    • DB write operations without commit/rollback handling
    • No idempotency check before inserting records
    • Missing check for duplicate trades before booking
    • Status transition without checking current state

11. CODE QUALITY AND MAINTAINABILITY
    • Method doing too many unrelated things (Single Responsibility)
    • Magic numbers inline in conditions (if (tranNum > 9999999))
    • Raw financial values printed to console (log to Endur audit trail instead)
    • Hardcoded field name strings — typos cause silent wrong-column reads
    • Missing @ScriptAttributes annotation on IScript implementations

12. ENDUR WORKFLOW COMPLETENESS
    • applyPortfolioOverride() stubs or TODO — incomplete business logic
    • No return/exit status set with Util.exitSucceed() / Util.exitFail()
    • Missing pre-validation before trade booking
    • No audit trail entry for fee calculations
"""


def build_prompt(file_maps: dict, tdd: str, standards: str) -> str:
    sections = ""
    for path, data in file_maps.items():
        # Number every line of the full file so Claude can reference exact line numbers
        numbered_lines = "\n".join(
            f"{i+1:4d} | {line}"
            for i, line in enumerate(data["full"].splitlines())
        )

        sections += f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FILE: `{path}`   (+{data['additions']} -{data['deletions']} in this PR)

DIFF LINES (lines in the PR diff — use inline=true for these):
{data['valid']}

FULL FILE — REVIEW EVERY LINE:
{numbered_lines}

DIFF (what changed):
```diff
{data['patch'][:3000]}
```
"""

    optional = ""
    if tdd:
        optional += f"\n## TDD / Requirements\n{tdd[:3000]}\n"
    if standards:
        optional += f"\n## Coding Standards\n{standards[:2000]}\n"

    return f"""Conduct a COMPLETE forensic review of this Endur/OpenJVS code.
Review the ENTIRE FILE — not just the diff.
Work through EVERY item in the checklist from your system prompt.
Report EVERY issue you find. Do NOT self-limit.

{optional}
{sections}

Return ONLY a valid JSON object — no markdown fences, no text outside the JSON:
{{
  "summary":  "3-4 sentence assessment — name the critical issues and overall quality.",
  "verdict":  "APPROVED" | "CHANGES_REQUESTED" | "COMMENT",
  "score":    <integer 1-10>,
  "stats":    {{ "critical": <n>, "warnings": <n>, "suggestions": <n> }},
  "comments": [
    {{
      "file":           "<exact filename>",
      "line":           <integer — from numbered file listing>,
      "inline":         <true if line is in DIFF LINES, false otherwise>,
      "severity":       "critical" | "warning" | "suggestion",
      "title":          "<issue title max 55 chars>",
      "body":           "<2 sentences max: what is wrong and the Endur production risk>",
      "suggested_code": "<single corrected line — no fences>"
    }}
  ]
}}

RULES:
- Report EVERY issue — do not say "most important only"
- "line" must exactly match the line number in the numbered listing
- "inline": true ONLY if that exact line number is in the DIFF LINES list
- suggested_code = only the replacement content for that line
- Do not duplicate the same issue on multiple lines
"""


def call_claude(prompt: str) -> dict:
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    print(f"   Prompt size: {len(prompt)} chars")
    msg = client.messages.create(
        model      = "claude-sonnet-4-6",
        max_tokens = 8192,
        system     = SYSTEM_PROMPT,
        messages   = [{"role": "user", "content": prompt}],
    )

    print(f"   Stop reason: {msg.stop_reason}")
    if msg.stop_reason == "max_tokens":
        print("   ⚠️  WARNING: Response was truncated — JSON will be incomplete!")

    raw = msg.content[0].text.strip()
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw.strip())

    print(f"   Response size: {len(raw)} chars")

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        # Try to salvage partial JSON
        print(f"⚠️  JSON parse error: {e}")
        print(f"   Raw (first 800):\n{raw[:800]}")
        return {
            "summary": f"Review ran but response format error: {e}. Check Actions log for raw output.",
            "verdict": "COMMENT", "score": 1,
            "stats":   {"critical": 0, "warnings": 0, "suggestions": 0},
            "comments": []
        }


# ══════════════════════════════════════════════════════════════════
#  PART 3 — COMMENT FORMATTING
# ══════════════════════════════════════════════════════════════════

ICONS  = {"critical": "🔴", "warning": "🟡", "suggestion": "🔵"}
LABELS = {"critical": "**Critical**", "warning": "**Warning**", "suggestion": "Suggestion"}


def format_inline_body(c: dict) -> str:
    """
    Format an inline PR review comment.
    INLINE_MARKER is embedded as a hidden HTML comment so we can
    find and delete these comments on the next review run.
    """
    icon  = ICONS.get(c["severity"], "ℹ️")
    label = LABELS.get(c["severity"], "Note")
    fix   = c.get("suggested_code", "").strip()

    parts = [
        INLINE_MARKER,   # ← hidden deletion marker
        f"{icon} {label}: **{c.get('title', 'Issue')}**",
        "",
        c.get("body", ""),
    ]

    if fix:
        parts += [
            "",
            "**Suggested fix** *(click **Commit suggestion** to apply)*:",
            "```suggestion",
            fix,
            "```",
        ]
    return "\n".join(parts)


def format_off_diff_section(issues: list) -> str:
    """Format issues that are on lines NOT in the PR diff — go into the summary."""
    if not issues:
        return ""

    lines = [
        "",
        "---",
        "### ⚠️ Additional Issues Found in Existing Code",
        "_These issues are on lines not changed in this PR. Address in a follow-up._",
        "",
    ]
    for c in issues:
        icon  = ICONS.get(c.get("severity"), "ℹ️")
        label = LABELS.get(c.get("severity"), "Note")
        fix   = c.get("suggested_code", "").strip()
        lines.append(f"**{icon} {label} — `{c.get('file')}` line {c.get('line')}: {c.get('title')}**")
        lines.append(c.get("body", ""))
        if fix:
            lines.append(f"```java\n// Suggested fix:\n{fix}\n```")
        lines.append("")
    return "\n".join(lines)


def format_summary(review: dict, file_maps: dict, off_diff: list) -> str:
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

    off_diff_section = format_off_diff_section(off_diff)

    return f"""{SUMMARY_MARKER}
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

### Files Reviewed
{files}

> 📌 Inline comments with **Commit suggestion** buttons are on the **Files changed** tab.
{off_diff_section}
<sub>Powered by Enerlytix AI · Claude Sonnet · Full-file forensic review v4</sub>
</details>"""


# ══════════════════════════════════════════════════════════════════
#  PART 4 — GITHUB API (with reliable marker-based deletion)
# ══════════════════════════════════════════════════════════════════

def delete_old_summary_comment():
    """Delete the previous summary comment using its HTML marker."""
    url  = f"https://api.github.com/repos/{REPO_NAME}/issues/{PR_NUMBER}/comments"
    resp = requests.get(url, headers=GH_HEADERS, params={"per_page": 100})
    if not resp.ok:
        return
    deleted = 0
    for c in resp.json():
        if SUMMARY_MARKER in (c.get("body") or ""):
            r = requests.delete(
                f"https://api.github.com/repos/{REPO_NAME}/issues/comments/{c['id']}",
                headers=GH_HEADERS
            )
            if r.ok:
                deleted += 1
    print(f"   Deleted {deleted} old summary comment(s)")


def delete_old_inline_comments():
    """
    Delete previous inline PR review comments using the hidden INLINE_MARKER.
    This is what was broken before — we now embed the marker in every inline comment.
    """
    url  = f"https://api.github.com/repos/{REPO_NAME}/pulls/{PR_NUMBER}/comments"
    resp = requests.get(url, headers=GH_HEADERS, params={"per_page": 100})
    if not resp.ok:
        return
    deleted = 0
    for c in resp.json():
        if INLINE_MARKER in (c.get("body") or ""):
            r = requests.delete(
                f"https://api.github.com/repos/{REPO_NAME}/pulls/comments/{c['id']}",
                headers=GH_HEADERS
            )
            if r.ok:
                deleted += 1
    print(f"   Deleted {deleted} old inline comment(s)")


def post_summary_comment(body: str):
    url  = f"https://api.github.com/repos/{REPO_NAME}/issues/{PR_NUMBER}/comments"
    resp = requests.post(url, headers=GH_HEADERS, json={"body": body})
    if resp.ok:
        print(f"✅ Summary comment posted (id: {resp.json().get('id')})")
    else:
        print(f"⚠️  Summary failed: {resp.status_code} {resp.text[:300]}")


def post_inline_comment(path: str, line: int, body: str) -> bool:
    """Post inline comment using GitHub's line+side API (reliable, no position math)."""
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
    print(f"\n{'='*60}")
    print(f"🔍 Enerlytix Inline Review v4 — PR #{PR_NUMBER} on {REPO_NAME}")
    print(f"{'='*60}")

    # 1. Fetch diff + full file contents
    print("\n📥 Fetching PR files and full content...")
    pr_files  = fetch_pr_files()
    file_maps = build_file_maps(pr_files)

    if not file_maps:
        print("ℹ️  No reviewable files in this PR.")
        sys.exit(0)

    # 2. Load optional docs
    tdd       = load_doc(TDD_PATH)
    standards = load_doc(STANDARDS_PATH)
    if tdd:       print(f"📄 TDD doc loaded: {len(tdd)} chars")
    if standards: print(f"📄 Standards loaded: {len(standards)} chars")

    # 3. Call Claude for full-file forensic review
    print("\n🤖 Calling Claude — full-file forensic review...")
    prompt = build_prompt(file_maps, tdd, standards)
    review = call_claude(prompt)

    all_comments = review.get("comments", [])
    print(f"\n✅ Claude response:")
    print(f"   Verdict : {review.get('verdict')}")
    print(f"   Score   : {review.get('score')}/10")
    print(f"   Issues  : {len(all_comments)} total")
    print(f"   Stats   : {review.get('stats')}")

    # 4. Classify comments — inline (in diff) vs off-diff (full file)
    inline_ready  = []
    off_diff      = []

    for c in all_comments:
        filepath = c.get("file", "")
        line_no  = c.get("line")
        is_inline = c.get("inline", False)

        if filepath not in file_maps:
            print(f"   ⚠️  Unknown file '{filepath}' — moving to off-diff")
            off_diff.append(c)
            continue

        valid_lines = file_maps[filepath]["valid"]

        if is_inline and line_no in valid_lines:
            # Perfect — Claude got it right
            inline_ready.append(c)
        elif line_no in valid_lines:
            # Claude said off-diff but line IS actually in diff — post inline
            c["inline"] = True
            inline_ready.append(c)
        else:
            # Line not in diff — try snapping to nearest diff line (within 5 lines)
            if valid_lines and line_no:
                nearest = min(valid_lines, key=lambda x: abs(x - line_no))
                if abs(nearest - line_no) <= 5:
                    print(f"   ↪ Snapping {filepath}:{line_no} → nearest diff line {nearest}")
                    c["line"] = nearest
                    inline_ready.append(c)
                    continue
            off_diff.append(c)

    print(f"\n   📌 Inline comments: {len(inline_ready)}")
    print(f"   📋 Off-diff (summary): {len(off_diff)}")

    # 5. Delete old review comments BEFORE posting new ones
    print("\n🧹 Deleting previous review comments...")
    delete_old_inline_comments()
    delete_old_summary_comment()

    # 6. Post new summary comment
    print("\n📤 Posting new review...")
    summary_body = format_summary(review, file_maps, off_diff)
    post_summary_comment(summary_body)

    # 7. Post inline comments
    posted  = 0
    failed  = 0
    for c in inline_ready:
        body = format_inline_body(c)
        ok   = post_inline_comment(c["file"], c["line"], body)
        if ok:
            posted += 1
            print(f"   ✅ {c['file']}:{c['line']} [{c.get('severity')}] {c.get('title')}")
        else:
            failed += 1
            # Fall back: add failed inline comment to summary output
            off_diff.append(c)

    # 8. Final stats
    print(f"\n{'='*60}")
    print(f"📊 REVIEW COMPLETE")
    print(f"   🔴 Critical  : {review.get('stats', {}).get('critical', 0)}")
    print(f"   🟡 Warnings  : {review.get('stats', {}).get('warnings', 0)}")
    print(f"   🔵 Suggest   : {review.get('stats', {}).get('suggestions', 0)}")
    print(f"   Score        : {review.get('score')}/10")
    print(f"   Inline posted: {posted} | failed: {failed}")
    print(f"   In summary   : {len(off_diff)}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
