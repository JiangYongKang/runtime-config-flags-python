"""跨进程可复现：在全新解释器进程中复算判定，结果必须与主进程一致。"""

from __future__ import annotations

import json
import subprocess
import sys

from runtime.flags import evaluate, stable_bucket

from .conftest import log_case, make_raw, build_config


def _external_compute(subjects: list[str], pct: int) -> list[bool]:
    code = (
        "import json, sys\n"
        "from runtime.parser import parse_source\n"
        "from runtime.merger import merge_sources\n"
        "from runtime.flags import evaluate\n"
        "doc = json.loads(sys.stdin.read())\n"
        "subjects = doc.pop('_subjects')\n"
        "cfg = merge_sources([parse_source(doc)], version=1)\n"
        "print(json.dumps([evaluate(cfg, 'feature_x', s).on "
        "for s in subjects]))\n"
    )
    raw = make_raw({"enabled": True, "percentage": pct, "overrides": {}})
    payload = {**raw, "_subjects": subjects}
    proc = subprocess.run(
        [sys.executable, "-c", code],
        input=json.dumps(payload), capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


def test_decisions_reproduce_in_fresh_process():
    subjects = [f"user-{i}" for i in range(120)]
    cfg = build_config([make_raw(
        {"enabled": True, "percentage": 47, "overrides": {}})])
    inproc = [evaluate(cfg, "feature_x", s).on for s in subjects]
    external = _external_compute(subjects, 47)
    assert inproc == external
    assert 0 < sum(inproc) < len(subjects)
    log_case("跨进程判定复现：120 主体在全新进程中结果逐一一致",
             raws={"percentage": 47, "subjects": 120}, version=1,
             result={"on_count": sum(inproc),
                     "sample_bucket": stable_bucket("feature_x", "user-0")})


def test_hash_vector_is_part_of_contract():
    # 固定向量锁定公式：实现一旦变更，判定分布与复现性立刻受测试保护
    assert stable_bucket("feature_x", "alice") == 64
    assert stable_bucket("rollout", "bob") == 27
    log_case("固定哈希向量锁定判定公式", version="-",
             result={"feature_x|alice": 64, "rollout|bob": 27})
