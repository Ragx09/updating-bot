import os, json, time
from datetime import datetime, timezone
from openai import OpenAI
from github import Github, GithubException
from notion_client import Client

# ── Clients ───────────────────────────────────────────────────────────────────
gh     = Github(os.environ["GH_TOKEN"])
ai     = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ["OPENROUTER_API_KEY"],
)
notion = Client(auth=os.environ["NOTION_TOKEN"])

NOTION_DB = os.environ["NOTION_DATABASE_ID"]
USERNAME  = os.environ["GH_USERNAME"]
MODEL     = "deepseek/deepseek-r1-0528:free"

# ── Idea source ───────────────────────────────────────────────────────────────

def get_idea():
    manual = os.environ.get("MANUAL_IDEA", "").strip()
    if manual:
        return manual
    try:
        with open("ideas.txt") as f:
            lines = [l.strip() for l in f if l.strip()]
            return lines[-1] if lines else None
    except FileNotFoundError:
        return None

# ── Project planning via Interactions API ────────────────────────────────────

def plan_project(idea: str) -> dict:
    prompt = (
        f"You are a senior software engineer. Plan a complete, runnable project "
        f"for this idea: '{idea}'\n\n"
        "Return ONLY a valid JSON object — no markdown fences, no preamble — with:\n"
        "{\n"
        '  "repo_name": "kebab-case-name",\n'
        '  "description": "one sentence description",\n'
        '  "tech_stack": ["python", "..."],\n'
        '  "files": [\n'
        '    {"path": "README.md", "content": "..."},\n'
        '    {"path": "main.py",   "content": "..."}\n'
        '  ]\n'
        "}\n"
        "Include all files needed to run the project (README, main source, "
        "requirements/package file, .gitignore)."
    )

    response = ai.chat.completions.create(
        model=MODEL,
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = (response.choices[0].message.content or "").strip()

    # Strip markdown fences if model adds them despite instructions
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    return json.loads(raw)

# ── GitHub repo management ────────────────────────────────────────────────────

def create_or_get_repo(name, description):
    user = gh.get_user(USERNAME)
    try:
        repo = user.get_repo(name)
        print(f"Repo '{name}' already exists — updating files")
        return repo, False
    except GithubException:
        repo = user.create_repo(
            name=name,
            description=description,
            private=False,
            auto_init=False
        )
        print(f"Created new repo: {name}")
        return repo, True

def push_files(repo, files):
    created, updated = 0, 0
    for file in files:
        path, content = file["path"], file["content"]
        try:
            existing = repo.get_contents(path)
            if existing.decoded_content.decode("utf-8", errors="replace") != content:
                repo.update_file(
                    path, f"feat: update {path} [ai-builder]",
                    content, existing.sha
                )
                print(f"  Updated  {path}")
                updated += 1
        except GithubException:
            repo.create_file(path, f"feat: add {path} [ai-builder]", content)
            print(f"  Created  {path}")
            created += 1
        time.sleep(0.3)   # gentle on the API
    return created, updated

# ── Notion logging ────────────────────────────────────────────────────────────

def log_to_notion(idea, repo_name, files, repo_url, created, updated):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    files_text = "\n".join(f"• {f['path']}" for f in files)
    log_text   = (
        f"Idea: {idea}\n"
        f"Repo: {repo_url}\n"
        f"Files created: {created} | updated: {updated}\n\n"
        f"File list:\n{files_text}"
    )

    notion.pages.create(
        parent={"database_id": NOTION_DB},
        properties={
            "Date":          {"title":     [{"text": {"content": today}}]},
            "Type":          {"select":    {"name": "Project Build"}},
            "Status":        {"select":    {"name": "✅ Success"}},
            "Repos Scanned": {"number":    0},
            "Commits Made":  {"number":    created + updated},
            "Files Changed": {"rich_text": [{"text": {"content": files_text[:2000]}}]},
            "Log":           {"rich_text": [{"text": {"content": log_text[:2000]}}]},
        }
    )
    print("✓ Logged to Notion")

# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    idea = get_idea()
    if not idea:
        print("No idea found. Add a line to ideas.txt or use workflow_dispatch.")
        return

    print(f"Building project for: '{idea}'")

    plan = plan_project(idea)
    print(f"Plan ready — repo: {plan['repo_name']}, files: {len(plan['files'])}")

    repo, _ = create_or_get_repo(plan["repo_name"], plan["description"])
    created, updated = push_files(repo, plan["files"])

    repo_url = f"https://github.com/{USERNAME}/{plan['repo_name']}"
    print(f"\n✓ Done: {repo_url}")

    log_to_notion(idea, plan["repo_name"], plan["files"], repo_url, created, updated)

if __name__ == "__main__":
    run()