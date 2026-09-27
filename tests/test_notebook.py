import json
import re
from pathlib import Path


def test_colab_evaluation_notebook_structure_and_safety() -> None:
    notebook_path = Path(__file__).resolve().parents[1] / "notebooks" / "evaluate.ipynb"
    notebook = json.loads(notebook_path.read_text(encoding="utf-8"))

    assert notebook["nbformat"] == 4
    assert notebook["nbformat_minor"] == 5
    assert notebook["metadata"]["kernelspec"]["name"] == "python3"
    assert notebook["metadata"]["accelerator"] == "GPU"
    assert all("id" in cell and "source" in cell for cell in notebook["cells"])
    for cell in notebook["cells"]:
        if cell["cell_type"] == "code":
            assert cell["outputs"] == []
            assert cell["execution_count"] is None

    source = "\n".join("".join(cell["source"]) for cell in notebook["cells"])
    assert "gate run" in source
    assert "fits_concurrently" in source
    assert 'userdata.get("HF_TOKEN")' in source or "userdata.get('HF_TOKEN')" in source
    assert "ngrok" not in source
    assert "cloudflared" not in source
    assert "0.0.0.0" not in source
    assert re.search(r"hf_[A-Za-z0-9]{10,}", source) is None


def test_notebook_never_leaves_billed_servers_waiting() -> None:
    notebook_path = Path(__file__).resolve().parents[1] / "notebooks" / "evaluate.ipynb"
    cells = json.loads(notebook_path.read_text(encoding="utf-8"))["cells"]
    ids = [cell["id"] for cell in cells]
    source = {cell["id"]: "".join(cell["source"]) for cell in cells}

    # Drive sign-in is interactive, so it must happen before any GPU server starts.
    assert ids.index("mount-drive") < ids.index("serve-and-run")
    assert all("drive.mount" not in text for cell_id, text in source.items() if cell_id != "mount-drive")
    # Servers always stop, and a failed run stops the notebook instead of copying another run.
    run_cell = source["serve-and-run"]
    assert "finally:" in run_cell and "stop_servers()" in run_cell.split("finally:", 1)[1]
    assert "check=True" in run_cell and '"--run-id",' in run_cell
    assert all("iterdir" not in text for text in source.values())
