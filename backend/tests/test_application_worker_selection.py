from api import application_worker


def _discovery(root, count):
    files = []
    for index in range(count):
        relative = f"src/Service{index:03}.java"
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"class Service{index} {{}}", encoding="utf-8")
        files.append({"path": relative, "size": path.stat().st_size})
    return {"file_tree": files}


def test_deep_mode_reads_every_eligible_file_by_default(tmp_path, monkeypatch):
    discovery = _discovery(tmp_path, 150)
    monkeypatch.setenv("ANALYSIS_MODE", "Deep")
    monkeypatch.delenv("APPLICATION_ANALYSIS_HERMES_FILE_LIMIT", raising=False)

    selected, coverage = application_worker._selected_file_evidence(
        tmp_path, discovery
    )

    assert len(selected) == 150
    assert coverage["selectedFiles"] == coverage["eligibleFiles"] == 150
    assert coverage["fileLimit"] == 150


def test_quick_mode_keeps_prioritized_file_limit(tmp_path, monkeypatch):
    discovery = _discovery(tmp_path, 75)
    monkeypatch.setenv("ANALYSIS_MODE", "Quick")
    monkeypatch.delenv("APPLICATION_ANALYSIS_HERMES_FILE_LIMIT", raising=False)

    selected, coverage = application_worker._selected_file_evidence(
        tmp_path, discovery
    )

    assert len(selected) == 40
    assert coverage["fileLimit"] == 40


def test_explicit_file_limit_still_overrides_deep_mode(tmp_path, monkeypatch):
    discovery = _discovery(tmp_path, 50)
    monkeypatch.setenv("ANALYSIS_MODE", "Deep")
    monkeypatch.setenv("APPLICATION_ANALYSIS_HERMES_FILE_LIMIT", "12")

    selected, coverage = application_worker._selected_file_evidence(
        tmp_path, discovery
    )

    assert len(selected) == 12
    assert coverage["fileLimit"] == 12
