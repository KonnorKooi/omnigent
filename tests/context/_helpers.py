"""Helpers shared by project-context tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def init_git_repo(path: Path) -> None:
    """``git init`` a directory (requires the ``git_identity`` fixture).

    :param path: Directory to initialise.
    """
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)


def write_graph(path: Path) -> None:
    """Write a small graphify-style node-link graph.

    :param path: ``graph.json`` destination.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "directed": True,
        "nodes": [
            {
                "id": "metrics_psnr",
                "label": "compute_psnr",
                "community": 0,
                "source_file": "evaluation/metrics.py",
                "source_location": "L10",
            },
            {
                "id": "metrics_accumulator",
                "label": "MetricAccumulator",
                "community": 0,
                "source_file": "evaluation/common.py",
                "source_location": "L35",
            },
            {
                "id": "train_main",
                "label": "train_main",
                "community": 1,
                "source_file": "training/train.py",
                "source_location": "L5",
            },
            {
                "id": "model_encoder",
                "label": "Encoder",
                "community": 1,
                "source_file": "model/encoder.py",
                "source_location": "L1",
            },
        ],
        "links": [
            {"source": "metrics_accumulator", "target": "metrics_psnr", "relation": "calls"},
            {"source": "train_main", "target": "metrics_accumulator", "relation": "calls"},
            {"source": "train_main", "target": "model_encoder", "relation": "uses"},
        ],
    }
    path.write_text(json.dumps(document), encoding="utf-8")
