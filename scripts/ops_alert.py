"""Turn Wasla production monitor failures into actionable GitHub issues.

This deliberately does not send emails directly. GitHub delivers notifications
according to the repository owner's verified notification email preferences.
Only public workflow metadata is included; no customer data or secrets.
"""
import json
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

OWNER = "ibrahimtwaiti"
WATCHED = frozenset({
    "Production health",
    "Production queue health",
    "PostgreSQL database backup to R2",
})
FAILURES = frozenset({"failure", "timed_out", "action_required"})
BACKUP_WORKFLOW = "backup.yml"
MAX_BACKUP_AGE_HOURS = 26


def api(method, path, payload=None, *, token=None, repo=None):
    token = token or os.environ["GH_TOKEN"]
    repo = repo or os.environ["GITHUB_REPOSITORY"]
    if repo != "ibrahimtwaiti/Wasla":
        raise ValueError("Unexpected repository; refusing to create alerts")
    url = "https://api.github.com/repos/" + repo + "/" + path.lstrip("/")
    data = json.dumps(payload).encode() if payload is not None else None
    req = Request(url, data=data, method=method, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + token,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "wasla-operations-alert",
        "Content-Type": "application/json",
    })
    with urlopen(req, timeout=20) as response:
        data = response.read()
    return json.loads(data) if data else None


def alert_from_event(event, event_name):
    """Return (key, title, safe body) or None."""
    if event_name == "workflow_dispatch":
        return ("test", "[Wasla TEST] تجربة إشعار التشغيل",
                "هذا تنبيه اختبار اصطناعي، وليس عطلًا حقيقيًا. "
                "يرجى التحقق من وصول إشعار GitHub إلى البريد المختار ثم إغلاق المسألة.")
    if event_name != "workflow_run":
        return None
    run = event.get("workflow_run", {})
    name = run.get("name")
    conclusion = run.get("conclusion")
    if name not in WATCHED or run.get("head_branch") != "main" or conclusion not in FAILURES:
        return None
    run_id = run.get("id")
    if not isinstance(run_id, int) or run_id <= 0:
        return None
    # Construct the URL ourselves; do not trust arbitrary event URLs.
    url = "https://github.com/ibrahimtwaiti/Wasla/actions/runs/" + str(run_id)
    body = ("فشل أو تعطل فحص تشغيلي في فرع main.\n\n"
            f"**Workflow:** {name}\n**Result:** {conclusion}\n"
            f"**Run:** {url}\n\n"
            "المسؤول الوحيد الحالي: @ibrahimtwaiti. "
            "يرجى فحص السبب وتوثيق المعالجة. لا تضع أسرارًا أو بيانات عملاء في المسألة.")
    return ("workflow-" + name.lower().replace(" ", "-"),
            "[Wasla ALERT] " + name, body)


def backup_age_alert(latest_success, now=None):
    now = now or datetime.now(timezone.utc)
    if not latest_success:
        age_text = "لا يوجد تشغيل نسخ احتياطي ناجح مسجل على main."
        link = "https://github.com/ibrahimtwaiti/Wasla/actions/workflows/backup.yml"
    else:
        started = latest_success.get("created_at", "")
        time = datetime.fromisoformat(started.replace("Z", "+00:00"))
        if time.tzinfo is None:
            raise ValueError("Backup timestamp missing timezone")
        if now - time < timedelta(hours=MAX_BACKUP_AGE_HOURS):
            return None
        age_text = f"أحدث نسخة ناجحة تجاوز عمرها {MAX_BACKUP_AGE_HOURS} ساعة."
        run_id = latest_success.get("id")
        if not isinstance(run_id, int):
            raise ValueError("Invalid run ID")
        link = "https://github.com/ibrahimtwaiti/Wasla/actions/runs/" + str(run_id)
    return ("backup-overdue", "[Wasla ALERT] لم تُثبت نسخة احتياطية حديثة",
            age_text + "\n\n" + link +
            "\n\nالمسؤول: @ibrahimtwaiti. تحقق من R2 وسجل Actions، "
            "ولا تفترض نجاح الاستعادة لمجرد نجاح مهمة النسخ.")


def open_issues():
    # Paginate to avoid duplicate issues when a repo has over 100 open items.
    page = 1
    while True:
        result = api("GET", "issues?" + urlencode({"state": "open", "per_page": 100, "page": page}))
        for issue in result:
            if "pull_request" not in issue:
                yield issue
        if len(result) < 100:
            break
        page += 1


def create_once(alert):
    key, title, body = alert
    marker = "<!-- wasla-ops-alert:" + key + " -->"
    if any(marker in issue.get("body", "") for issue in open_issues()):
        print("Alert already open:", key)
        return False
    api("POST", "issues", {
        "title": title,
        "body": marker + "\n" + body,
        "assignees": [OWNER],
    })
    print("Created operational issue:", key)
    return True


def main():
    name = os.environ["GITHUB_EVENT_NAME"]
    if name in {"workflow_run", "workflow_dispatch"}:
        with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as stream:
            event = json.load(stream)
        alert = alert_from_event(event, name)
    elif name == "schedule":
        runs = api("GET", "actions/workflows/" + BACKUP_WORKFLOW + "/runs?"
                   + urlencode({"branch": "main", "status": "success", "per_page": 1}))
        all_runs = runs.get("workflow_runs", [])
        alert = backup_age_alert(all_runs[0] if all_runs else None)
    else:
        raise ValueError("Unexpected event")
    if alert:
        create_once(alert)
    else:
        print("No operational alert needed")


if __name__ == "__main__":
    main()
