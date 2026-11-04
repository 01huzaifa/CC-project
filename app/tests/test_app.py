import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app import app, cart_total, valid_email

CART = {"X-Cart-Id": "3f0c9a52-8a8a-4c63-9f3c-0f6a1f4b2c11"}


def test_health_returns_ok():
    r = app.test_client().get("/health")
    assert r.status_code == 200 and r.data == b"OK"


def test_info_has_hostname():
    assert "hostname" in app.test_client().get("/api/info").get_json()


def test_cart_total_adds_price_times_quantity():
    items = [{"price_cents": 1000, "qty": 2}, {"price_cents": 250, "qty": 3}]
    assert cart_total(items) == 2750
    assert cart_total([]) == 0


def test_email_validation():
    assert valid_email("a@b.com")
    assert not valid_email("not-an-email")
    assert not valid_email("")


def test_cart_requires_cart_id():
    assert app.test_client().get("/api/cart").status_code == 400


def test_add_to_cart_rejects_bad_quantity():
    r = app.test_client().post("/api/cart/items", json={"product_id": 1, "qty": 500}, headers=CART)
    assert r.status_code == 400


def test_checkout_rejects_bad_email():
    r = app.test_client().post("/api/checkout", json={"name": "A", "email": "x", "address": "Street 1"}, headers=CART)
    assert r.status_code == 400


def test_admin_forbidden_without_token(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    c = app.test_client()
    assert c.get("/api/admin/orders").status_code == 403
    assert c.get("/api/admin/orders", headers={"X-Admin-Token": "wrong"}).status_code == 403
