import json

from codexapi import TaskResult

from yieldthought_agents.tasks.functional_bringup import parse_metrics, sanitize_branch_name, SetupError
from yieldthought_agents.worker import classify_outcome, parse_issue_body, parse_system_from_ttsmi_output


def test_sanitize_branch_name():
    hf_id = "meta-llama/Llama-3.1-8B-Instruct"
    assert sanitize_branch_name(hf_id) == "meta-llama-llama-3.1-8b-instruct"


def test_parse_issue_body_fields():
    body = """
    hf_model_id: meta-llama/Llama-3.1-8B-Instruct
    hf_revision: main
    prefill_len: 128
    decode_len: 32
    batch: 2
    notes: optional text
    """
    fields = parse_issue_body(body)
    assert fields["hf_model_id"] == "meta-llama/Llama-3.1-8B-Instruct"
    assert fields["hf_revision"] == "main"
    assert fields["prefill_len"] == "128"
    assert fields["decode_len"] == "32"
    assert fields["batch"] == "2"
    assert fields["notes"] == "optional text"


def test_parse_metrics():
    metrics = {"top1": 0.95, "top5": 0.98, "tokens": 10, "mode": "hf", "trace": False, "timing": {"total": 1.0}}
    output = "prefix\nYT_METRICS=" + json.dumps(metrics) + "\n"
    parsed = parse_metrics(output)
    assert parsed["top1"] == 0.95
    assert parsed["top5"] == 0.98
    assert parsed["tokens"] == 10


def test_classify_failure():
    failure = TaskResult(False, "summary", 1, "errors", "thread")
    assert classify_outcome(None, SetupError("boom")) == "setup error"
    assert classify_outcome(failure, None) == "failed"


def test_parse_system_n150():
    output = """
    Board Type   Device Series
    Wormhole     n150 L
    """
    assert parse_system_from_ttsmi_output(output) == "n150"


def test_parse_system_n300():
    output = """
    Board Type   Device Series
    Wormhole     n300 L
    """
    assert parse_system_from_ttsmi_output(output) == "n300"


def test_parse_system_lb():
    output = """
    Board Type   Device Series
    Wormhole     n300 L
    Wormhole     n300 L
    Wormhole     n300 L
    Wormhole     n300 L
    """
    assert parse_system_from_ttsmi_output(output) == "lb"
