import os, random, base64
from datetime import datetime, timezone, timedelta
from openai import OpenAI
from github import Github
from notion_client import Client

# ── Clients ───────────────────────────────────────────────────────────────────
gh     = Github(os.environ["GH_TOKEN"])
ai     = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ["OPENROUTER_API_KEY"],
)
notion = Client(auth=os.environ["NOTION_TOKEN"])

NOTION_DB   = os.environ["NOTION_DATABASE_ID"]
USERNAME    = os.environ["GH_USERNAME"]
MODEL       = "deepseek/deepseek-v4-flash:free"
EXTENSIONS  = {".py", ".md", ".js", ".ts", ".txt", ".yaml", ".yml", ".json"}
SKIP_DAYS   = 3    # skip repos active in last N days
MAX_COMMITS = 3    # cap commits per day

log_lines = []

def log(msg):
    print(msg)
    log_lines.append(msg)

# ── Smart skip logic ──────────────────────────────────────────────────────────

def is_recently_active(repo):
    cutoff = datetime.now(timezone.utc) - timedelta(days=SKIP_DAYS)
    return bool(repo.pushed_at and repo.pushed_at > cutoff)

# ── File picking ──────────────────────────────────────────────────────────────

def get_all_repos():
    return [r for r in gh.get_user(USERNAME).get_repos()
            if not r.archived and not r.fork]

def pick_random_file(repo):
    try:
        contents = list(repo.get_contents(""))
        candidates = []
        while contents and len(candidates) < 200:
            item = contents.pop(0)
            if item.type == "dir":
                try:
                    contents.extend(repo.get_contents(item.path))
                except Exception:
                    pass
            elif any(item.path.endswith(ext) for ext in EXTENSIONS):
                candidates.append(item)
        return random.choice(candidates) if candidates else None
    except Exception:
        return None

# ── AI improvement via Claude ─────────────────────────────────────────────────

def improve_file(repo, file_obj):
    raw = base64.b64decode(file_obj.content).decode("utf-8", errors="replace")
    snippet = raw[:3000]

    response = ai.chat.completions.create(
        model=MODEL,
        max_tokens=2000,
        messages=[{
            "role": "user",
            "content": (
                f"You are improving a file in the repo '{repo.name}'.\n"
                f"File: {file_obj.path}\n\n"
                f"Current content (may be truncated):\n```\n{snippet}\n```\n\n"
                "Make ONE small, meaningful improvement: fix a typo, improve a comment, "
                "add a missing docstring, update a stale note, or fix minor formatting. "
                "Return ONLY the full updated file content, no explanation, no markdown fences."
            )
        }]
    )

    new_content = (response.choices[0].message.content or "").strip()

    # Safety guard: reject if change is disproportionately large
    if abs(len(new_content) - len(raw)) > len(raw) * 0.4:
        log(f"  Skipped {file_obj.path}: change too large, looks unsafe")
        return False, None
    if new_content == raw:
        log(f"  No change needed for {file_obj.path}")
        return False, None

    repo.update_file(
        path=file_obj.path,
        message=f"chore: minor improvement to {file_obj.path} [bot]",
        content=new_content,
        sha=file_obj.sha
    )
    log(f"  Committed to {repo.name}/{file_obj.path}")
    return True, file_obj.path

# ── Notion logging ────────────────────────────────────────────────────────────

def log_to_notion(repos_scanned, skipped, committed, commit_details, status):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    files_text = "\n".join(commit_details) if commit_details else "None"
    full_log   = "\n".join(log_lines)

    notion.pages.create(
        parent={"database_id": NOTION_DB},
        properties={
            "Date":          {"title":     [{"text": {"content": today}}]},
            "Type":          {"select":    {"name": "Daily Commit"}},
            "Status":        {"select":    {"name": status}},
            "Repos Scanned": {"number":    repos_scanned},
            "Commits Made":  {"number":    committed},
            "Files Changed": {"rich_text": [{"text": {"content": files_text[:2000]}}]},
            "Log":           {"rich_text": [{"text": {"content": full_log[:2000]}}]},
        }
    )
    log("Logged to Notion")

# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    repos = get_all_repos()
    log(f"Found {len(repos)} repos total")

    eligible, skipped_names = [], []
    for r in repos:
        if is_recently_active(r):
            skipped_names.append(r.name)
            log(f"  Skipping {r.name} — active in last {SKIP_DAYS} days")
        else:
            eligible.append(r)

    log(f"Eligible: {len(eligible)} | Skipped (recently active): {len(skipped_names)}")
    random.shuffle(eligible)

    committed, commit_details = 0, []
    for repo in eligible:
        if committed >= MAX_COMMITS:
            break
        log(f"Scanning {repo.name}...")
        f = pick_random_file(repo)
        if f:
            try:
                ok, path = improve_file(repo, f)
                if ok:
                    committed += 1
                    commit_details.append(f"- {repo.name}/{path}")
            except Exception as e:
                log(f"  Error on {repo.name}: {e}")

    if committed == 0:
        status = "No Changes"
    elif committed < MAX_COMMITS:
        status = "Partial"
    else:
        status = "Success"

    log(f"\nDone — {committed} commit(s), {len(skipped_names)} repo(s) skipped.")
    try:
        log_to_notion(len(repos), len(skipped_names), committed, commit_details, status)
    except Exception as e:
        log(f"Notion logging failed (non-fatal): {e}")

if __name__ == "__main__":
    run()