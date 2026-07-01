"""Tests for EA JSON-line protocol."""

import pytest

from src.execution.protocol import (
    CMD_PLACE_PENDING,
    EARequest,
    EAResponse,
    RSP_OK,
    decode_message,
    encode_message,
    new_request_id,
)


def test_encode_decode_roundtrip():
    payload = {"type": CMD_PLACE_PENDING, "id": "abc", "symbol": "XAUUSD", "entry": 4360.0}
    line = encode_message(payload)
    assert line.endswith(b"\n")
    assert decode_message(line) == payload


def test_ea_request_to_dict():
    req = EARequest(cmd="PING", params={"foo": "bar"})
    d = req.to_dict()
    assert d["type"] == "PING"
    assert d["id"] == req.request_id
    assert d["foo"] == "bar"


def test_ea_response_ok():
    rsp = EAResponse.from_dict({"type": RSP_OK, "id": "x1", "retcode": 10009, "order": 555})
    assert rsp.ok
    assert rsp.request_id == "x1"
    assert rsp.retcode == 10009
    assert rsp.data["order"] == 555


def test_ea_response_err():
    rsp = EAResponse.from_dict({"type": "ERR", "id": "x2", "error": "fail", "retcode": 10013})
    assert not rsp.ok
    assert rsp.error == "fail"


def test_new_request_id_unique():
    assert new_request_id() != new_request_id()
