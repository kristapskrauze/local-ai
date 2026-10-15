import os


def test_git_api_endpoints_registered():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    main_path = os.path.join(root, "app", "main.py")
    with open(main_path, "r", encoding="utf-8") as f:
        src = f.read()
    for marker in (
        '@app.get("/api/git/status")',
        'SERVICE_REPO',
        '"branch"',
        '"files"',
        '@app.get("/api/git/diff")',
        '@app.post("/api/git/commit")',
        '@app.post("/api/git/task")',
    ):
        assert marker in src, "missing endpoint: %s" % marker


def test_git_status_path_not_stripped():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    main_path = os.path.join(root, "app", "main.py")
    with open(main_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "path = line[3:]" in src, "path must not be .strip()-ed (leading space is part of the path)"
    assert "path = line[3:].strip()" not in src, "path = line[3:].strip() mangles paths with a leading space"


if __name__ == "__main__":
    test_git_api_endpoints_registered()
    test_git_status_path_not_stripped()
    print("OK")
