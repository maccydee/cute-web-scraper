from cute_web_scraper.cache import PageCache


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_put_then_get():
    cache: PageCache[str] = PageCache(ttl_s=60, max_entries=10)
    cache.put("https://a.com", False, "VALUE")
    assert cache.get("https://a.com", False) == "VALUE"


def test_missing_key_returns_none():
    cache: PageCache[str] = PageCache(ttl_s=60, max_entries=10)
    assert cache.get("https://a.com", False) is None


def test_js_render_is_part_of_the_key():
    cache: PageCache[str] = PageCache(ttl_s=60, max_entries=10)
    cache.put("https://a.com", False, "STATIC")
    assert cache.get("https://a.com", True) is None


def test_entry_expires():
    clock = FakeClock()
    cache: PageCache[str] = PageCache(ttl_s=60, max_entries=10, clock=clock)
    cache.put("https://a.com", False, "VALUE")
    clock.now += 61
    assert cache.get("https://a.com", False) is None


def test_eviction_bounds_size():
    cache: PageCache[str] = PageCache(ttl_s=60, max_entries=3)
    for i in range(10):
        cache.put(f"https://s{i}.com", False, str(i))
    assert cache.size() == 3
    assert cache.get("https://s0.com", False) is None
    assert cache.get("https://s9.com", False) == "9"


def test_zero_ttl_disables_cache():
    cache: PageCache[str] = PageCache(ttl_s=0, max_entries=10)
    cache.put("https://a.com", False, "VALUE")
    assert cache.get("https://a.com", False) is None
