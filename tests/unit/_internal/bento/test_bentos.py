import os

import pytest

import bentoml
from bentoml._internal.bento import BentoStore


@pytest.mark.usefixtures("change_test_dir")
def test_create_simplebento(tmpdir):
    bento_store = BentoStore(tmpdir)

    bentoml.build(
        "simplebento.py:svc",
        version="1.0",
        build_ctx="./simplebento",
        description="simple bento",
        models=[],
        # models=['iris_classifier:v123'],
        include=["*.py", "config.json", "somefile", "*dir*"],
        exclude=[
            "*.storage",
            "/somefile",
        ],  # + anything specified in .bentoml_ignore file
        env=dict(
            # pip_install=bentoml.utils.find_required_pypi_packages(svc),
            conda="./environment.yaml",
            docker={
                # "base_image": bentoml.utils.builtin_docker_image("slim", gpu=True),
                "setup_script": "./setup_docker_container.sh",
            },
        ),
        labels={
            "team": "foo",
            "dataset_version": "abc",
            "framework": "pytorch",
        },
        _bento_store=bento_store,
    )

    test_path = os.path.join(tmpdir)
    assert set(os.listdir(test_path)) == set(["test.simplebento"])
    test_path = os.path.join(test_path, "test.simplebento")
    assert set(os.listdir(test_path)) == set(["latest", "1.0"])
    test_path = os.path.join(test_path, "1.0")
    assert set(os.listdir(test_path)) == set(
        [
            "bento.yaml",
            "apis",
            "README.md",
            "test.simplebento",
            "env",
        ]
    )
    test_path = os.path.join(test_path, "test.simplebento")
    assert set(os.listdir(test_path)) == set(["simplebento.py", "subdir"])
    assert set(os.listdir(os.path.join(test_path, "subdir"))) == set(["somefile"])