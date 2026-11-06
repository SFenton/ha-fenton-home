"""The house configuration document: shallow validation, hashing, file choice. No Home Assistant."""

from __future__ import annotations

import json

import pytest

from fenton_home.const import HOUSE_CONFIG_MAX_BYTES
from fenton_home.house_config import HouseConfigError, find_file, load_file, parse_document

DEMO = {"schema": 1, "rooms": [{"id": "demo_room", "name": "Demo Room"}], "weather": {"entity": "weather.demo"}}


def reason(text: str, *, is_json: bool = True) -> str:
    with pytest.raises(HouseConfigError) as err:
        parse_document(text, is_json=is_json)
    return err.value.reason


def test_a_valid_document_is_served_without_the_schema_key_in_config() -> None:
    document = parse_document(json.dumps(DEMO), is_json=True)
    assert document["schema"] == 1
    assert document["config"] == {"rooms": DEMO["rooms"], "weather": DEMO["weather"]}
    assert len(document["hash"]) == 64


def test_the_hash_ignores_key_order_and_follows_content() -> None:
    reordered = {"weather": DEMO["weather"], "rooms": DEMO["rooms"], "schema": 1}
    changed = {**DEMO, "weather": {"entity": "weather.other"}}
    assert parse_document(json.dumps(reordered), is_json=True)["hash"] == parse_document(json.dumps(DEMO), is_json=True)["hash"]
    assert parse_document(json.dumps(changed), is_json=True)["hash"] != parse_document(json.dumps(DEMO), is_json=True)["hash"]


def test_yaml_parses() -> None:
    document = parse_document("schema: 2\nrooms:\n  - id: demo_room\n", is_json=False)
    assert document["schema"] == 2 and document["config"] == {"rooms": [{"id": "demo_room"}]}


@pytest.mark.parametrize(("text", "expected"), [
    ("{not json", "unparseable"),
    ("[1, 2]", "not_a_mapping"),
    ('{"rooms": []}', "bad_schema"),
    ('{"schema": 0, "rooms": []}', "bad_schema"),
    ('{"schema": "1", "rooms": []}', "bad_schema"),
    ('{"schema": true, "rooms": []}', "bad_schema"),
    ('{"schema": 1, "rooms": "text"}', "bad_section"),
    ('{"schema": 1, "count": 3}', "bad_section"),
])
def test_invalid_documents_are_refused(text: str, expected: str) -> None:
    assert reason(text) == expected


def test_yaml_values_that_json_cannot_carry_are_refused() -> None:
    assert reason("schema: 1\nrooms:\n  when: 2026-11-06\n", is_json=False) == "not_json"
    assert reason("schema: [", is_json=False) == "unparseable"


def test_yaml_wins_over_json(tmp_path) -> None:
    assert find_file(tmp_path) is None
    (tmp_path / "house.json").write_text(json.dumps(DEMO))
    assert find_file(tmp_path).name == "house.json"
    (tmp_path / "house.yaml").write_text("schema: 1\n")
    assert find_file(tmp_path).name == "house.yaml"


def test_load_file_not_configured_and_size_limit(tmp_path) -> None:
    assert load_file(tmp_path) is None
    big = {"schema": 1, "rooms": ["x" * 1024] * 300}
    (tmp_path / "house.json").write_text(json.dumps(big))
    assert (tmp_path / "house.json").stat().st_size > HOUSE_CONFIG_MAX_BYTES
    with pytest.raises(HouseConfigError) as err:
        load_file(tmp_path)
    assert err.value.reason == "too_large"


def test_load_file_reads_the_chosen_file(tmp_path) -> None:
    (tmp_path / "house.yaml").write_text("schema: 3\nrooms: []\n")
    assert load_file(tmp_path)["schema"] == 3


def test_a_file_that_is_not_utf8_is_refused(tmp_path) -> None:
    (tmp_path / "house.json").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(HouseConfigError) as err:
        load_file(tmp_path)
    assert err.value.reason == "unreadable"
