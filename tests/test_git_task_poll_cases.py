import os


def test_git_task_poll_endpoints():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    main_path = os.path.join(root, "app", "main.py")
    with open(main_path, "r", encoding="utf-8") as f:
        src = f.read()
    # POST endpoint returns job_id
    assert '@app.post("/api/git/task")' in src, "missing POST /api/git/task"
    assert '"job_id": job_id' in src, "POST must return job_id"
    # GET /api/git/task/<job_id> endpoint exists
    assert '@app.get("/api/git/task/{job_id}")' in src, "missing GET /api/git/task/{job_id}"
    assert 'def api_git_task_poll(job_id: str):' in src, "missing api_git_task_poll handler"
    # Status field is returned
    assert '"status": job.get("status", "pending")' in src, "GET must return status field"
    # Unknown job_id returns 404
    assert 'unknown job_id' in src, "unknown job_id must return 404 error"


if __name__ == "__main__":
    test_git_task_poll_endpoints()
    print("OK")
