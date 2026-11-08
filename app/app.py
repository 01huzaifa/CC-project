import hmac
import os
import re
import socket
import uuid
from contextlib import contextmanager

import pymysql
from flask import Flask, jsonify, request

app = Flask(__name__)
_db_ready = False
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

SEED_PRODUCTS = [
    ("Wireless Earbuds", "Bluetooth 5.3, 24-hour battery with charging case.", 5999, 40, "Electronics", "🎧"),
    ("Mechanical Keyboard", "Hot-swappable switches, tactile feel, USB-C.", 8900, 25, "Electronics", "⌨️"),
    ("Smart Watch", "Heart-rate tracking and a 7-day battery.", 12900, 15, "Electronics", "⌚"),
    ("Ceramic Mug Set", "Set of four, dishwasher safe.", 2400, 60, "Home", "☕"),
    ("Desk Lamp", "Dimmable LED with warm and cool modes.", 3500, 30, "Home", "💡"),
    ("Canvas Backpack", "20L with a laptop sleeve, water resistant.", 4500, 35, "Fashion", "🎒"),
    ("Running Shoes", "Lightweight mesh upper, cushioned sole.", 7900, 20, "Fashion", "👟"),
    ("Notebook Pack", "Three dotted A5 notebooks.", 1200, 100, "Stationery", "📓"),
]


# ---------- database helpers ----------
def get_connection():
    """DB settings come from environment variables - never hardcoded."""
    return pymysql.connect(
        host=os.environ["DB_HOST"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_NAME"],
        port=int(os.environ.get("DB_PORT", 3306)),
        connect_timeout=5,
        cursorclass=pymysql.cursors.DictCursor,
    )


@contextmanager
def db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create tables and seed products. Safe to run from many instances."""
    global _db_ready
    if _db_ready:
        return
    with db() as conn, conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS products (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(120) NOT NULL UNIQUE,
            description VARCHAR(500) NOT NULL DEFAULT '',
            price_cents INT NOT NULL,
            stock INT NOT NULL DEFAULT 0,
            category VARCHAR(50) NOT NULL DEFAULT 'General',
            emoji VARCHAR(16) NOT NULL DEFAULT '📦'
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS cart_items (
            cart_id CHAR(36) NOT NULL,
            product_id INT NOT NULL,
            qty INT NOT NULL,
            PRIMARY KEY (cart_id, product_id),
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS orders (
            id INT AUTO_INCREMENT PRIMARY KEY,
            customer_name VARCHAR(120) NOT NULL,
            email VARCHAR(200) NOT NULL,
            address VARCHAR(300) NOT NULL,
            total_cents INT NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'PLACED',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS order_items (
            id INT AUTO_INCREMENT PRIMARY KEY,
            order_id INT NOT NULL,
            product_id INT NOT NULL,
            name VARCHAR(120) NOT NULL,
            price_cents INT NOT NULL,
            qty INT NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
        )""")
        cur.executemany(
            "INSERT IGNORE INTO products (name, description, price_cents, stock, category, emoji) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            SEED_PRODUCTS,
        )
    _db_ready = True


# ---------- small pure helpers (unit tested) ----------
def cart_total(items):
    return sum(i["price_cents"] * i["qty"] for i in items)


def valid_email(value):
    return bool(EMAIL_RE.match(value or ""))


def get_cart_id():
    try:
        return str(uuid.UUID(request.headers.get("X-Cart-Id", "")))
    except ValueError:
        return None


def is_admin():
    expected = os.environ.get("ADMIN_TOKEN", "")
    given = request.headers.get("X-Admin-Token", "")
    return bool(expected) and hmac.compare_digest(expected, given)


def load_cart(cur, cid):
    cur.execute(
        """SELECT p.id, p.name, p.price_cents, p.emoji, p.stock, c.qty
           FROM cart_items c JOIN products p ON p.id = c.product_id
           WHERE c.cart_id = %s ORDER BY p.id""",
        (cid,),
    )
    items = cur.fetchall()
    return {"items": items, "total_cents": cart_total(items)}


# ---------- basic endpoints ----------
@app.get("/health")
def health():
    # Load balancer health check. Deliberately does not touch the DB.
    return "OK", 200


@app.get("/api/info")
def info():
    return jsonify(hostname=socket.gethostname())


# ---------- catalogue ----------
@app.get("/api/products")
def list_products():
    init_db()
    q = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    sql, args = "SELECT * FROM products WHERE 1=1", []
    if q:
        sql += " AND (name LIKE %s OR description LIKE %s)"
        args += [f"%{q}%", f"%{q}%"]
    if category:
        sql += " AND category = %s"
        args.append(category)
    with db() as conn, conn.cursor() as cur:
        cur.execute(sql + " ORDER BY id", args)
        return jsonify(cur.fetchall())


@app.get("/api/products/<int:pid>")
def get_product(pid):
    init_db()
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM products WHERE id = %s", (pid,))
        row = cur.fetchone()
    return (jsonify(row), 200) if row else (jsonify(error="product not found"), 404)


@app.get("/api/categories")
def categories():
    init_db()
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT category FROM products ORDER BY category")
        return jsonify([r["category"] for r in cur.fetchall()])


# ---------- cart (stored in the DB, so any instance can serve any request) ----------
@app.get("/api/cart")
def get_cart():
    cid = get_cart_id()
    if not cid:
        return jsonify(error="missing or invalid X-Cart-Id"), 400
    init_db()
    with db() as conn, conn.cursor() as cur:
        return jsonify(load_cart(cur, cid))


def _set_qty(cur, cid, pid, qty):
    """Returns an error tuple or None."""
    if qty <= 0:
        cur.execute("DELETE FROM cart_items WHERE cart_id=%s AND product_id=%s", (cid, pid))
        return None
    cur.execute("SELECT stock FROM products WHERE id=%s", (pid,))
    p = cur.fetchone()
    if not p:
        return jsonify(error="product not found"), 404
    if qty > p["stock"]:
        return jsonify(error=f"Only {p['stock']} in stock"), 409
    cur.execute(
        "INSERT INTO cart_items (cart_id, product_id, qty) VALUES (%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE qty = %s",
        (cid, pid, qty, qty),
    )
    return None


@app.post("/api/cart/items")
def add_to_cart():
    cid = get_cart_id()
    body = request.get_json(silent=True) or {}
    if not cid:
        return jsonify(error="missing or invalid X-Cart-Id"), 400
    try:
        pid, delta = int(body["product_id"]), int(body.get("qty", 1))
    except (KeyError, ValueError, TypeError):
        return jsonify(error="product_id and qty must be numbers"), 400
    if delta < 1 or delta > 99:
        return jsonify(error="qty must be between 1 and 99"), 400
    init_db()
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT qty FROM cart_items WHERE cart_id=%s AND product_id=%s", (cid, pid))
        row = cur.fetchone()
        err = _set_qty(cur, cid, pid, (row["qty"] if row else 0) + delta)
        if err:
            return err
        return jsonify(load_cart(cur, cid))


@app.put("/api/cart/items")
def set_cart_item():
    cid = get_cart_id()
    body = request.get_json(silent=True) or {}
    if not cid:
        return jsonify(error="missing or invalid X-Cart-Id"), 400
    try:
        pid, qty = int(body["product_id"]), int(body["qty"])
    except (KeyError, ValueError, TypeError):
        return jsonify(error="product_id and qty must be numbers"), 400
    if qty < 0 or qty > 99:
        return jsonify(error="qty must be between 0 and 99"), 400
    init_db()
    with db() as conn, conn.cursor() as cur:
        err = _set_qty(cur, cid, pid, qty)
        if err:
            return err
        return jsonify(load_cart(cur, cid))


# ---------- checkout and orders ----------
@app.post("/api/checkout")
def checkout():
    cid = get_cart_id()
    body = request.get_json(silent=True) or {}
    name = str(body.get("name", "")).strip()
    email = str(body.get("email", "")).strip().lower()
    address = str(body.get("address", "")).strip()
    if not cid:
        return jsonify(error="missing or invalid X-Cart-Id"), 400
    if not name or not address:
        return jsonify(error="name and address are required"), 400
    if not valid_email(email):
        return jsonify(error="a valid email is required"), 400

    init_db()
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT product_id, qty FROM cart_items WHERE cart_id=%s ORDER BY product_id", (cid,))
        lines = cur.fetchall()
        if not lines:
            return jsonify(error="your cart is empty"), 400

        ids = [l["product_id"] for l in lines]
        marks = ",".join(["%s"] * len(ids))
        # Lock rows (in id order, to avoid deadlocks) so two buyers cannot oversell stock.
        cur.execute(f"SELECT id, name, price_cents, stock FROM products WHERE id IN ({marks}) ORDER BY id FOR UPDATE", ids)
        products = {p["id"]: p for p in cur.fetchall()}

        total = 0
        for l in lines:
            p = products.get(l["product_id"])
            if not p or p["stock"] < l["qty"]:
                label = p["name"] if p else "an item"
                return jsonify(error=f"Not enough stock for {label}"), 409
            total += p["price_cents"] * l["qty"]

        cur.execute(
            "INSERT INTO orders (customer_name, email, address, total_cents) VALUES (%s,%s,%s,%s)",
            (name[:120], email[:200], address[:300], total),
        )
        order_id = cur.lastrowid
        for l in lines:
            p = products[l["product_id"]]
            cur.execute(
                "INSERT INTO order_items (order_id, product_id, name, price_cents, qty) VALUES (%s,%s,%s,%s,%s)",
                (order_id, p["id"], p["name"], p["price_cents"], l["qty"]),
            )
            cur.execute("UPDATE products SET stock = stock - %s WHERE id = %s", (l["qty"], p["id"]))
        cur.execute("DELETE FROM cart_items WHERE cart_id=%s", (cid,))
    return jsonify(order_id=order_id, total_cents=total), 201


def _order_with_items(cur, order_id):
    cur.execute("SELECT * FROM orders WHERE id=%s", (order_id,))
    order = cur.fetchone()
    if not order:
        return None
    order["created_at"] = order["created_at"].isoformat()
    cur.execute("SELECT name, price_cents, qty FROM order_items WHERE order_id=%s", (order_id,))
    order["items"] = cur.fetchall()
    return order


@app.get("/api/orders/<int:order_id>")
def get_order(order_id):
    email = request.args.get("email", "").strip().lower()
    init_db()
    with db() as conn, conn.cursor() as cur:
        order = _order_with_items(cur, order_id)
    # Same message for "no such order" and "wrong email" so IDs cannot be probed.
    if not order or order["email"] != email:
        return jsonify(error="order not found for that email"), 404
    return jsonify(order)


# ---------- admin (protected by ADMIN_TOKEN secret) ----------
@app.post("/api/admin/products")
def admin_add_product():
    if not is_admin():
        return jsonify(error="forbidden"), 403
    b = request.get_json(silent=True) or {}
    try:
        name = str(b["name"]).strip()
        price, stock = int(b["price_cents"]), int(b["stock"])
    except (KeyError, ValueError, TypeError):
        return jsonify(error="name, price_cents and stock are required"), 400
    if not name or price < 0 or stock < 0:
        return jsonify(error="invalid values"), 400
    init_db()
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO products (name, description, price_cents, stock, category, emoji) VALUES (%s,%s,%s,%s,%s,%s)",
            (name[:120], str(b.get("description", ""))[:500], price, stock,
             str(b.get("category", "General"))[:50] or "General", str(b.get("emoji", "📦"))[:16] or "📦"),
        )
        return jsonify(id=cur.lastrowid), 201


@app.put("/api/admin/products/<int:pid>/stock")
def admin_set_stock(pid):
    if not is_admin():
        return jsonify(error="forbidden"), 403
    try:
        stock = int((request.get_json(silent=True) or {})["stock"])
    except (KeyError, ValueError, TypeError):
        return jsonify(error="stock must be a number"), 400
    if stock < 0:
        return jsonify(error="stock cannot be negative"), 400
    init_db()
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE products SET stock=%s WHERE id=%s", (stock, pid))
    return jsonify(id=pid, stock=stock)


@app.get("/api/admin/orders")
def admin_orders():
    if not is_admin():
        return jsonify(error="forbidden"), 403
    init_db()
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, customer_name, email, total_cents, status, created_at FROM orders ORDER BY id DESC LIMIT 50")
        rows = cur.fetchall()
    for r in rows:
        r["created_at"] = r["created_at"].isoformat()
    return jsonify(rows)

@app.errorhandler(pymysql.err.OperationalError)
def db_down(e):
    app.logger.error("Database error: %s", e)
    return jsonify(error="database unavailable"), 503