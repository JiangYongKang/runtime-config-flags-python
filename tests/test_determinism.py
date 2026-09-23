"""跨进程稳定性：灰度桶值不得受 Python hash 随机化影响。

通过子进程在不同 PYTHONHASHSEED 下重复计算，锁定可复现性。
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys

import pytest

from app.flags import stable_bucket

logger = logging.getLogger("runtime_config.tests")

_CHILD = (
    "from app.flags import stable_bucket;"
    "import json;"
    "print(json.dumps({"
    "'a': stable_bucket('new_ui','user-42','salt-1'),"
    "'b': stable_bucket('new_ui','user-43','salt-1'),"
    "'c': stable_bucket('other','user-42','salt-1')"
    "}))"
)


def _bucket_map(seed: int) -> dict:
    import os
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = str(seed)
    out = subprocess.check_output(
        [sys.executable, "-c", _CHILD], cwd=__import__("pathlib").Path.cwd(), env=env, text=True
    )
    return json.loads(out)


@pytest.mark.parametrize("seed", [0, 1, 12345])
def test_bucket_stable_across_hash_seeds(seed):
    result = _bucket_map(seed)
    baseline = {
        "a": stable_bucket("new_ui", "user-42", "salt-1"),
        "b": stable_bucket("new_ui", "user-43", "salt-1"),
        "c": stable_bucket("other", "user-42", "salt-1"),
    }
    logger.info("[跨进程确定性] PYTHONHASHSEED=%s -> %s（基线 %s）", seed, result, baseline)
    assert result == baseline
