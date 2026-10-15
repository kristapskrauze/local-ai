import os


def test_git_task_is_background_propose():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    main_path = os.path.join(root, "app", "main.py")
    with open(main_path, "r", encoding="utf-8") as f:
        src = f.read()
    for marker in (
        '@app.post("/api/git/task")',
        '@app.get("/api/git/task/status")',
        '_GIT_TASK_JOBS',
        '_run_git_task_propose',
    ):
        assert marker in src, "missing: %s" % marker
    assert 'task-%s.txt' not in src, "old file-write task endpoint must be removed"


if __name__ == "__main__":
    test_git_task_is_background_propose()
    print("OK")
