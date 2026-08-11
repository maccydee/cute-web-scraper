import json

import pytest

from cute_web_scraper.store import ResultStore, StoreError

ROWS = [
    {"url": "https://a.com", "title": "A", "price": 10.5},
    {"url": "https://b.com", "title": "B", "price": 20.0},
    {"url": "https://c.com", "title": "C", "price": 5.25},
]


@pytest.fixture
def store(tmp_path):
    return ResultStore(tmp_path / "results.db")


def test_save_and_list(store):
    info = store.save("products", ROWS)
    assert info.name == "products"
    assert info.row_count == 3
    assert set(info.columns) == {"url", "title", "price"}
    assert [t.name for t in store.list_tables()] == ["products"]


def test_save_replaces_existing(store):
    store.save("t", ROWS)
    store.save("t", ROWS[:1])
    assert store.get_table("t").row_count == 1


def test_append_adds_rows(store):
    store.save("t", ROWS)
    store.append("t", [{"url": "https://d.com", "title": "D", "price": 1.0}])
    assert store.get_table("t").row_count == 4


def test_get_table_returns_sample(store):
    store.save("t", ROWS)
    info = store.get_table("t", sample=2)
    assert info.row_count == 3
    assert len(info.sample) == 2
    assert info.sample[0]["url"] == "https://a.com"


def test_get_unknown_table_raises(store):
    with pytest.raises(StoreError, match="not found"):
        store.get_table("nope")


def test_query_filters_and_sorts(store):
    store.save("products", ROWS)
    result = store.query("SELECT title, price FROM products WHERE price > 6 ORDER BY price DESC")
    assert [r["title"] for r in result.rows] == ["B", "A"]


def test_query_aggregates(store):
    store.save("products", ROWS)
    result = store.query("SELECT COUNT(*) AS n, ROUND(AVG(price), 2) AS avg_price FROM products")
    assert result.rows[0]["n"] == 3
    assert result.rows[0]["avg_price"] == pytest.approx(11.92)


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM products",
        "DROP TABLE products",
        "UPDATE products SET price = 0",
        "INSERT INTO products (url) VALUES ('x')",
        "ATTACH DATABASE '/tmp/evil.db' AS evil",
        "CREATE TABLE evil (x TEXT)",
    ],
)
def test_write_statements_are_rejected(store, sql):
    """The model must not be able to destroy or alter saved data through query_table."""
    store.save("products", ROWS)
    with pytest.raises(StoreError):
        store.query(sql)
    assert store.get_table("products").row_count == 3


def test_multiple_statements_rejected(store):
    store.save("products", ROWS)
    with pytest.raises(StoreError, match="single"):
        store.query("SELECT 1; DELETE FROM products")


def test_query_row_cap(store):
    store.save("t", [{"i": i} for i in range(500)])
    result = store.query("SELECT * FROM t", max_rows=10)
    assert len(result.rows) == 10
    assert result.truncated is True
    assert result.total_rows_examined >= 10


def test_export_csv(store, tmp_path):
    store.save("t", ROWS)
    path = store.export("t", "csv", tmp_path)
    text = path.read_text()
    assert path.suffix == ".csv"
    assert "url,title,price" in text.replace(" ", "")
    assert "https://a.com" in text


def test_export_json(store, tmp_path):
    store.save("t", ROWS)
    path = store.export("t", "json", tmp_path)
    data = json.loads(path.read_text())
    assert len(data) == 3
    assert data[0]["title"] == "A"


def test_export_unknown_format(store, tmp_path):
    store.save("t", ROWS)
    with pytest.raises(StoreError, match="csv"):
        store.export("t", "pdf", tmp_path)


@pytest.mark.parametrize("name", ["bad name", "drop;table", "1abc", "", "a" * 100])
def test_invalid_table_names_rejected(store, name):
    with pytest.raises(StoreError):
        store.save(name, ROWS)


def test_save_empty_rows_rejected(store):
    with pytest.raises(StoreError, match="no rows"):
        store.save("t", [])


def test_ragged_rows_union_columns(store):
    store.save("t", [{"a": 1}, {"b": 2}])
    info = store.get_table("t")
    assert set(info.columns) == {"a", "b"}
    assert info.row_count == 2


def test_drop_table(store):
    store.save("t", ROWS)
    store.drop("t")
    assert store.list_tables() == []


@pytest.mark.parametrize(
    "sql",
    [
        # REPLACE() is a read-only string function, not REPLACE INTO. Found live.
        "SELECT REPLACE(title, ' - BBC News', '') AS headline FROM t",
        # Write keywords inside string literals are data, not statements.
        "SELECT * FROM t WHERE title = 'delete'",
        "SELECT * FROM t WHERE title LIKE '%update%'",
        "SELECT * FROM t WHERE title IN ('drop', 'create')",
    ],
)
def test_legitimate_queries_are_not_blocked(store, sql):
    store.save("t", [{"title": "x", "price": 1.0}])
    store.query(sql)  # must not raise


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM t WHERE 1=1; DROP TABLE t",
        "REPLACE INTO t VALUES ('x', 1)",
        "WITH x AS (SELECT 1) DELETE FROM t",
    ],
)
def test_write_attempts_still_rejected_after_literal_stripping(store, sql):
    store.save("t", [{"title": "x", "price": 1.0}])
    with pytest.raises(StoreError):
        store.query(sql)
    assert store.get_table("t").row_count == 1
