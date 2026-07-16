import logging

from pipeline.text.segmenter import segment


def _span(text, bbox, *, size=12, font="Body", color=0, flags=0):
    return {
        "text": text,
        "bbox": bbox,
        "size": size,
        "font": font,
        "color": color,
        "flags": flags,
    }


def _block(text, bbox, spans):
    return {
        "type": 0,
        "bbox": bbox,
        "number": 0,
        "lines": [{"bbox": bbox, "dir": [1.0, 0.0], "wmode": 0,
                   "spans": spans}],
    }


def _parse_data(blocks, tables=None):
    return {
        "toc": [],
        "pages": [{
            "page": 0,
            "rect": [0, 0, 100, 100],
            "blocks": blocks,
            "images": [],
            "tables": tables or [],
        }],
    }


def test_dominant_style_ignores_small_bullet_span_and_preserves_flags():
    bbox = [0, 0, 80, 20]
    block = _block(
        "● body",
        bbox,
        [
            _span("\uf06c", [0, 5, 5, 11], size=5, font="PUA", flags=1),
            _span("●", [0, 5, 6, 11], size=6, font="Bullet", flags=0),
            _span(" body", [8, 0, 80, 20], size=12, font="Body",
                  color=123, flags=20),
        ],
    )

    result = segment(_parse_data([block]), {}, logging.getLogger("test"))

    assert len(result) == 1
    assert result[0]["font"] == "Body"
    assert result[0]["size"] == 12
    assert result[0]["color"] == 123
    assert result[0]["flags"] == 20


def test_cell_mapping_uses_inner_layout_bbox_and_prevents_cross_cell_merge():
    left_bbox = [40, 5, 50, 15]
    right_bbox = [49, 5, 60, 15]
    blocks = [
        _block("Left", left_bbox,
               [_span("Left", left_bbox, font="Body", flags=4)]),
        _block("Right", right_bbox,
               [_span("Right", right_bbox, font="Body", flags=4)]),
    ]
    tables = [{
        "bbox": [0, 0, 100, 20],
        "method": "find_tables",
        "cells": [
            {"index": 0, "bbox": [0, 0, 50, 20], "row": 0, "col": 0},
            {"index": 1, "bbox": [50, 0, 100, 20], "row": 0, "col": 1},
        ],
    }]

    result = segment(_parse_data(blocks, tables), {}, logging.getLogger("test"))

    assert len(result) == 2
    left, right = result
    assert left["bbox"] == left_bbox
    assert left["type"] == "cell"
    assert left["layout_bbox"] == [2.0, 1.0, 48.0, 19.0]
    assert (left["table_idx"], left["cell_idx"], left["row"], left["col"]) == (0, 0, 0, 0)
    assert right["bbox"] == right_bbox
    assert right["type"] == "cell"
    assert right["layout_bbox"] == [52.0, 1.0, 98.0, 19.0]
    assert (right["table_idx"], right["cell_idx"], right["row"], right["col"]) == (0, 1, 0, 1)


def test_one_extracted_block_spanning_two_cells_is_split():
    block = {
        "type": 0,
        "bbox": [10, 5, 90, 15],
        "number": 0,
        "lines": [
            {"bbox": [10, 5, 30, 15], "dir": [1.0, 0.0], "wmode": 0,
             "spans": [_span("Left", [10, 5, 30, 15])]},
            {"bbox": [70, 5, 90, 15], "dir": [1.0, 0.0], "wmode": 0,
             "spans": [_span("Right", [70, 5, 90, 15])]},
        ],
    }
    tables = [{
        "bbox": [0, 0, 100, 20],
        "method": "find_tables",
        "cells": [
            {"index": 0, "bbox": [0, 0, 50, 20], "row": 0, "col": 0},
            {"index": 1, "bbox": [50, 0, 100, 20], "row": 0, "col": 1},
        ],
    }]

    result = segment(_parse_data([block], tables), {}, logging.getLogger("test"))

    assert [item["text"] for item in result] == ["Left", "Right"]
    assert [item["cell_idx"] for item in result] == [0, 1]
    assert [item["bbox"] for item in result] == [[10, 5, 30, 15],
                                                  [70, 5, 90, 15]]


def test_blocks_in_the_same_cell_form_one_logical_segment():
    first = _block("First", [5, 2, 40, 8],
                   [_span("First", [5, 2, 40, 8])])
    second = _block("Second", [5, 10, 40, 16],
                    [_span("Second", [5, 10, 40, 16])])
    tables = [{
        "bbox": [0, 0, 50, 20],
        "method": "find_tables",
        "cells": [
            {"index": 0, "bbox": [0, 0, 50, 20], "row": 0, "col": 0},
        ],
    }]

    result = segment(_parse_data([first, second], tables), {},
                     logging.getLogger("test"))

    assert len(result) == 1
    assert result[0]["text"] == "First\nSecond"
    assert result[0]["bbox"] == [5, 2, 40, 16]
    assert result[0]["layout_bbox"] == [2.0, 1.0, 48.0, 19.0]
