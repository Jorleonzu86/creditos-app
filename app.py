import os
from flask import Flask, render_template, request, redirect, url_for, flash, send_file
from flask_login import LoginManager, login_user, logout_user, login_required, UserMixin
from passlib.hash import bcrypt
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from datetime import datetime
import psycopg
from psycopg.rows import dict_row

# --- CONFIGURACIÓN PRINCIPAL ---
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "clave-super-segura")

DB_URL = os.getenv("DATABASE_URL")
if not DB_URL:
    raise RuntimeError("⚠️ No se encontró DATABASE_URL en las variables de entorno.")

# --- LOGIN ---
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"


# --- MODELO DE USUARIO ---
class Usuario(UserMixin):
    def __init__(self, id, username, password_hash, role):
        self.id = id
        self.username = username
        self.password_hash = password_hash
        self.role = role

    def check_password(self, password):
        return bcrypt.verify(password, self.password_hash)


@login_manager.user_loader
def load_user(user_id):
    with psycopg.connect(DB_URL, row_factory=dict_row) as conn:
        user = conn.execute("SELECT * FROM usuarios WHERE id = %s", (user_id,)).fetchone()
        if user:
            return Usuario(user["id"], user["username"], user["password_hash"], user["role"])
    return None


# --- RUTA: LOGIN ---
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"].strip()

        with psycopg.connect(DB_URL, row_factory=dict_row) as conn:
            user = conn.execute("SELECT * FROM usuarios WHERE username = %s", (username,)).fetchone()

        if user and bcrypt.verify(password, user["password_hash"]):
            user_obj = Usuario(user["id"], user["username"], user["password_hash"], user["role"])
            login_user(user_obj)
            flash("Inicio de sesión exitoso.", "success")
            return redirect(url_for("index"))
        else:
            flash("Usuario o contraseña incorrectos.", "error")

    return render_template("login.html")


# --- RUTA: LOGOUT ---
@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Sesión cerrada.", "info")
    return redirect(url_for("login"))


# --- RUTA PRINCIPAL: FORMULARIO ---
@app.route("/", methods=["GET", "POST"])
@login_required
def index():
    if request.method == "POST":
        usuario = request.form["usuario"].strip()
        fecha = request.form["fecha"]
        producto = request.form["producto"].strip()
        tipo = request.form["tipo"]
        monto = float(request.form["monto"])

        with psycopg.connect(DB_URL) as conn:
            with conn.cursor() as cur:
                # Crear usuario si no existe
                cur.execute("SELECT id FROM clientes WHERE nombre = %s", (usuario,))
                user_exists = cur.fetchone()
                if not user_exists:
                    cur.execute("INSERT INTO clientes (nombre) VALUES (%s)", (usuario,))
                # Registrar movimiento
                cur.execute("""
                    INSERT INTO movimientos (usuario, fecha, producto, tipo, monto)
                    VALUES (%s, %s, %s, %s, %s)
                """, (usuario, fecha, producto, tipo, monto))
                conn.commit()

        flash("Movimiento registrado correctamente.", "success")
        return redirect(url_for("index"))

    # Mostrar los últimos movimientos
    with psycopg.connect(DB_URL, row_factory=dict_row) as conn:
        ultimos = conn.execute("""
            SELECT usuario, fecha, producto, tipo, monto 
            FROM movimientos ORDER BY id DESC LIMIT 10
        """).fetchall()

    return render_template("form.html", ultimos=ultimos, now=datetime.today().strftime("%Y-%m-%d"))


# --- RUTA: DETALLE POR USUARIO ---
@app.route("/usuario/<name>")
@login_required
def detalle_usuario(name):
    with psycopg.connect(DB_URL, row_factory=dict_row) as conn:
        registros = conn.execute("""
            SELECT fecha, producto, tipo, monto
            FROM movimientos WHERE usuario = %s ORDER BY fecha ASC
        """, (name,)).fetchall()

        saldo = sum(r["monto"] if r["tipo"] == "Compra" else -r["monto"] for r in registros)
        tabla = []
        acumulado = 0
        for r in registros:
            acumulado += r["monto"] if r["tipo"] == "Compra" else -r["monto"]
            tabla.append((r["fecha"], r["producto"], r["tipo"], r["monto"], acumulado))

    return render_template("usuario.html", user=name, tabla=tabla, saldo=saldo)


# --- RUTA: GENERAR PDF ---
@app.route("/usuario/<name>/pdf")
@login_required
def usuario_pdf(name):
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    c.setTitle(f"Estado de cuenta - {name}")

    # Logo y encabezado
    logo_path = os.path.join("static", "ibafuco_logo.jpg")
    if os.path.exists(logo_path):
        c.drawImage(logo_path, 50, 750, width=80, height=80)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(150, 800, f"Estado de cuenta - {name}")
    c.setFont("Helvetica", 12)
    c.drawString(150, 785, "Cocina - Iglesia Bautista Fundamental de Costa Rica")

    # Consultar datos
    with psycopg.connect(DB_URL, row_factory=dict_row) as conn:
        movimientos = conn.execute("""
            SELECT fecha, producto, tipo, monto
            FROM movimientos WHERE usuario = %s ORDER BY fecha ASC
        """, (name,)).fetchall()

    # Imprimir tabla
    y = 740
    saldo = 0
    for m in movimientos:
        y -= 20
        if y < 100:
            c.showPage()
            y = 800
        if m["tipo"] == "Compra":
            saldo += m["monto"]
        else:
            saldo -= m["monto"]
        c.drawString(60, y, m["fecha"].strftime("%d/%m/%Y"))
        c.drawString(150, y, m["producto"])
        c.drawString(350, y, m["tipo"])
        c.drawRightString(500, y, f"₡{m['monto']:.2f}")
        c.drawRightString(560, y, f"₡{saldo:.2f}")

    # Total
    c.setFont("Helvetica-Bold", 14)
    c.drawString(60, 80, f"Saldo final: ₡{saldo:.2f}")

    c.save()
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name=f"{name}_estado_cuenta.pdf", mimetype="application/pdf")


# --- CREAR TABLAS SI NO EXISTEN ---
def inicializar_bd():
    with psycopg.connect(DB_URL) as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT DEFAULT 'user'
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS clientes (
                id SERIAL PRIMARY KEY,
                nombre TEXT UNIQUE NOT NULL
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS movimientos (
                id SERIAL PRIMARY KEY,
                usuario TEXT NOT NULL,
                fecha DATE NOT NULL,
                producto TEXT,
                tipo TEXT CHECK (tipo IN ('Compra', 'Abono')),
                monto NUMERIC NOT NULL
            );
        """)
        conn.commit()
        print("✅ Tablas verificadas o creadas correctamente.")

# 🔸 ¡OJO!: Ejecutamos la inicialización SIEMPRE (también cuando arranca Gunicorn)
inicializar_bd()


# --- EJECUCIÓN LOCAL (Gunicorn ignora esta sección) ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
