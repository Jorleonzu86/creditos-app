# app.py (login robusto + truncado a 72 chars string + logs claros)
import os
from datetime import datetime
from io import BytesIO

from flask import Flask, render_template, request, redirect, url_for, flash, send_file
from flask_login import LoginManager, login_user, logout_user, login_required, UserMixin
from passlib.hash import bcrypt
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
import psycopg
from psycopg.rows import dict_row

# ----------------- Configuración base -----------------
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "clave-super-segura")

DB_URL = os.getenv("DATABASE_URL")
if not DB_URL:
    raise RuntimeError("⚠️ No se encontró DATABASE_URL en las variables de entorno.")

# ----------------- Login Manager -----------------
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

# ----------------- Modelo de usuario -----------------
class Usuario(UserMixin):
    def __init__(self, id, username, password_hash, role):
        self.id = id
        self.username = username
        self.password_hash = password_hash
        self.role = role

    def check_password(self, password: str) -> bool:
        """
        Trunca a 72 *caracteres* (bcrypt considera máx 72 bytes; para ASCII simple
        72 chars == 72 bytes; para unicode complejo passlib igual internamente
        normaliza. Lo importante es NO pasar secretos de >72).
        """
        try:
            pw = (password or "")[:72]  # string truncado
            ok = bcrypt.verify(pw, self.password_hash)
            if not ok:
                print(f"[Usuario.check_password] Hash no coincide para user={self.username}")
            return ok
        except Exception as e:
            print(f"[Usuario.check_password] Error verificando hash: {e}")
            return False

@login_manager.user_loader
def load_user(user_id):
    try:
        with psycopg.connect(DB_URL, row_factory=dict_row) as conn:
            user = conn.execute(
                "SELECT * FROM usuarios WHERE id = %s", (user_id,)
            ).fetchone()
            if user:
                return Usuario(user["id"], user["username"], user["password_hash"], user["role"])
    except Exception as e:
        print(f"[user_loader] Error cargando user_id={user_id}: {e}")
    return None

# ----------------- Rutas -----------------
@app.route("/health")
def health():
    return "OK", 200

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        # Acepta name="username" o name="usuario"; y "password" o "contrasena"
        username = (request.form.get("username") or request.form.get("usuario") or "").strip()
        password = request.form.get("password") or request.form.get("contrasena") or ""

        try:
            with psycopg.connect(DB_URL, row_factory=dict_row) as conn:
                user = conn.execute(
                    "SELECT * FROM usuarios WHERE username = %s",
                    (username,)
                ).fetchone()

            if not user:
                print(f"[LOGIN] Usuario no encontrado: '{username}'")
                flash("Usuario o contraseña incorrectos.", "error")
                return render_template("login.html")

            user_obj = Usuario(user["id"], user["username"], user["password_hash"], user["role"])
            if user_obj.check_password(password):
                login_user(user_obj)
                flash("Inicio de sesión exitoso.", "success")
                return redirect(url_for("index"))
            else:
                print(f"[LOGIN] Password inválido para usuario: '{username}'")
                flash("Usuario o contraseña incorrectos.", "error")
        except Exception as e:
            print(f"[LOGIN route] Excepción en POST /login para '{username}': {e}")
            flash("Ocurrió un error interno al iniciar sesión.", "error")

    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Sesión cerrada.", "info")
    return redirect(url_for("login"))

@app.route("/", methods=["GET", "POST"])
@login_required
def index():
    if request.method == "POST":
        try:
            usuario = request.form["usuario"].strip()
            fecha = request.form["fecha"]
            producto = request.form["producto"].strip()
            tipo = request.form["tipo"]
            monto = float(request.form["monto"])

            with psycopg.connect(DB_URL) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT id FROM clientes WHERE nombre = %s", (usuario,))
                    if not cur.fetchone():
                        cur.execute("INSERT INTO clientes (nombre) VALUES (%s)", (usuario,))

                    cur.execute(
                        """
                        INSERT INTO movimientos (usuario, fecha, producto, tipo, monto)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (usuario, fecha, producto, tipo, monto),
                    )
                    conn.commit()

            flash("Movimiento registrado correctamente.", "success")
            return redirect(url_for("index"))
        except Exception as e:
            print(f"[INDEX POST] Error guardando movimiento: {e}")
            flash("Ocurrió un error guardando el movimiento.", "error")

    with psycopg.connect(DB_URL, row_factory=dict_row) as conn:
        ultimos = conn.execute(
            """
            SELECT usuario, fecha, producto, tipo, monto
            FROM movimientos
            ORDER BY id DESC
            LIMIT 10
            """
        ).fetchall()

    return render_template("form.html", ultimos=ultimos, now=datetime.today().strftime("%Y-%m-%d"))

@app.route("/usuario/<name>")
@login_required
def detalle_usuario(name):
    with psycopg.connect(DB_URL, row_factory=dict_row) as conn:
        registros = conn.execute(
            """
            SELECT fecha, producto, tipo, monto
            FROM movimientos
            WHERE usuario = %s
            ORDER BY fecha ASC
            """,
            (name,),
        ).fetchall()

        saldo = 0
        tabla = []
        for r in registros:
            saldo += r["monto"] if r["tipo"] == "Compra" else -r["monto"]
            tabla.append((r["fecha"], r["producto"], r["tipo"], r["monto"], saldo))

    return render_template("usuario.html", user=name, tabla=tabla, saldo=saldo)

@app.route("/usuario/<name>/pdf")
@login_required
def usuario_pdf(name):
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    c.setTitle(f"Estado de cuenta - {name}")

    logo_path = os.path.join("static", "ibafuco_logo.jpg")
    if os.path.exists(logo_path):
        c.drawImage(logo_path, 50, 750, width=80, height=80)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(150, 800, f"Estado de cuenta - {name}")
    c.setFont("Helvetica", 12)
    c.drawString(150, 785, "Cocina - Iglesia Bautista Fundamental de Costa Rica")

    with psycopg.connect(DB_URL, row_factory=dict_row) as conn:
        movimientos = conn.execute(
            """
            SELECT fecha, producto, tipo, monto
            FROM movimientos
            WHERE usuario = %s
            ORDER BY fecha ASC
            """,
            (name,),
        ).fetchall()

    y = 740
    saldo = 0
    for m in movimientos:
        y -= 20
        if y < 100:
            c.showPage()
            y = 800
        saldo += m["monto"] if m["tipo"] == "Compra" else -m["monto"]
        c.drawString(60, y, m["fecha"].strftime("%d/%m/%Y"))
        c.drawString(150, y, m["producto"])
        c.drawString(350, y, m["tipo"])
        c.drawRightString(500, y, f"₡{m['monto']:.2f}")
        c.drawRightString(560, y, f"₡{saldo:.2f}")

    c.setFont("Helvetica-Bold", 14)
    c.drawString(60, 80, f"Saldo final: ₡{saldo:.2f}")
    c.save()
    buffer.seek(0)
    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"{name}_estado_cuenta.pdf",
        mimetype="application/pdf",
    )

# ----------------- Init DB -----------------
def inicializar_bd():
    with psycopg.connect(DB_URL) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS usuarios (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT DEFAULT 'user'
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS clientes (
                id SERIAL PRIMARY KEY,
                nombre TEXT UNIQUE NOT NULL
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS movimientos (
                id SERIAL PRIMARY KEY,
                usuario TEXT NOT NULL,
                fecha DATE NOT NULL,
                producto TEXT,
                tipo TEXT CHECK (tipo IN ('Compra', 'Abono')),
                monto NUMERIC NOT NULL
            );
            """
        )
        conn.commit()
        print("✅ Tablas verificadas o creadas correctamente.")

# ----------------- Local -----------------
if __name__ == "__main__":
    inicializar_bd()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)


