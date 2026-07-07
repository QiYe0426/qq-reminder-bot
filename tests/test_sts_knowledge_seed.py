from pathlib import Path

import sts_knowledge_seed as seed


def write_guide(path: Path, body: str) -> None:
    path.write_text(
        "\\section{卡牌评价}\n"
        "\\subsection{巨像}\n"
        f"{body}\n",
        encoding="utf-8",
    )


def test_guide_paths_excludes_108_diff_source(tmp_path, monkeypatch) -> None:
    old_path = tmp_path / "main_1.tex"
    new_path = tmp_path / "main_108.tex"
    old_path.write_text("\\section{旧攻略}\n正文足够长，用来测试普通攻略导入。", encoding="utf-8")
    new_path.write_text("\\section{新攻略}\n正文足够长，用来测试差异源不被完整导入。", encoding="utf-8")
    monkeypatch.setattr(seed, "DEFAULT_GUIDE_DIR", tmp_path)
    monkeypatch.delenv("STS2_GUIDE_TEX_PATHS", raising=False)

    paths = seed.guide_paths()

    assert old_path in paths
    assert new_path not in paths


def test_build_sts2_guide_version_diff_items_only_changed_sections(tmp_path) -> None:
    old_path = tmp_path / "main_1.tex"
    new_path = tmp_path / "main_108.tex"
    write_guide(
        old_path,
        "107版本：巨像这张牌还行，能提供稳定收益，抓位中等偏上。"
        "这段文字故意写长一点，保证章节内容超过最小长度。",
    )
    write_guide(
        new_path,
        "108版本：巨像因为环境和数值变化，抓率大不如前，只在特定构筑中考虑。"
        "这段文字故意写长一点，保证章节内容超过最小长度。",
    )

    items = seed.build_sts2_guide_version_diff_items(old_path, new_path)

    assert len(items) == 1
    assert items[0]["category"] == "STS2/guide/version-diff"
    assert "巨像" in items[0]["title"]
    assert "107版本" in items[0]["content"]
    assert "108版本" in items[0]["content"]
    assert "文本差异" in items[0]["content"]
