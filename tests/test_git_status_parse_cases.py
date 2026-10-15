import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_path_keeps_first_char():
    """' M app/agent_tools.py' must yield path 'app/agent_tools.py', not 'pp/agent_tools.py'."""
    # Simulate the parsing logic from app/main.py _git_status
    line = " M app/agent_tools.py"
    # The bug: line.strip() removes the leading space, shifting the path start
    # Correct: slice from the raw line at index 3
    code = line[:2].strip()
    path = line[3:].strip()
    assert code == "M", "status code should be 'M', got %r" % code
    assert path == "app/agent_tools.py", "path should be 'app/agent_tools.py', got %r" % path


def test_path_modified_both():
    """'MM app/agent_tools.py' must yield path 'app/agent_tools.py'."""
    line = "MM app/agent_tools.py"
    code = line[:2].strip()
    path = line[3:].strip()
    assert code == "MM", "status code should be 'MM', got %r" % code
    assert path == "app/agent_tools.py", "path should be 'app/agent_tools.py', got %r" % path


def test_path_untracked():
    """'?? new_file.py' must yield path 'new_file.py'."""
    line = "?? new_file.py"
    code = line[:2].strip()
    path = line[3:].strip()
    assert code == "??", "status code should be '??', got %r" % code
    assert path == "new_file.py", "path should be 'new_file.py', got %r" % path


if __name__ == "__main__":
    test_path_keeps_first_char()
    test_path_modified_both()
    test_path_untracked()
    print("OK")