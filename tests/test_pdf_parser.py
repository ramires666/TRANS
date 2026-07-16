from pipeline.pdf.parser import _table_to_dict


class _Row:
    def __init__(self, cells):
        self.cells = cells


class _Table:
    bbox = (0, 0, 100, 40)
    row_count = 2
    col_count = 2
    # PyMuPDF may expose this in column-major rather than row-major order.
    cells = [
        (0, 0, 50, 20),
        (0, 20, 50, 40),
        (50, 0, 100, 20),
        (50, 20, 100, 40),
    ]
    rows = [
        _Row([(0, 0, 50, 20), (50, 0, 100, 20)]),
        _Row([(0, 20, 50, 40), (50, 20, 100, 40)]),
    ]


def test_table_cells_receive_api_row_and_column_metadata():
    parsed = _table_to_dict(_Table())

    assert parsed["bbox"] == [0.0, 0.0, 100.0, 40.0]
    assert parsed["row_count"] == 2
    assert parsed["col_count"] == 2
    by_index = {cell["index"]: cell for cell in parsed["cells"]}
    assert (by_index[0]["row"], by_index[0]["col"]) == (0, 0)
    assert (by_index[1]["row"], by_index[1]["col"]) == (1, 0)
    assert (by_index[2]["row"], by_index[2]["col"]) == (0, 1)
    assert (by_index[3]["row"], by_index[3]["col"]) == (1, 1)
