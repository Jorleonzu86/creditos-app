import os
from flask import Flask, render_template, request, redirect, url_for, flash, send_file
from flask_login import LoginManager, login_user, logout_user, login_required, UserMixin, current_user
from passlib.hash import bcrypt
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from datetime import datetime
import psycopg
from psycopg.rows import dict_row

# -------------------------------------------------
# CONFIGURACIÓN BÁSICA
# -------------------------------------------------
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "clave-super-segura")

DB_URL = os.getenv("DATABASE_URL")
if not DB_URL:
    raise RuntimeError("No se encontró DATABASE_URL en las variables de entorno.")

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"


def get_conn(dict_rows: bool = False):
    """Devuelve una conexión a Postgres."""
    if dict_rows:
        return psycopg.connect(DB_URL, row_factory=dict_row)
    return psycopg.connect(DB_URL)


# -------------------------------------------------
# MODELO DE USUARIO PARA FLASK-LOGIN
# -------------------------------------------------
class Usuario(UserMixin):
    def __init__(self, id, username, password_hash, role):
        self.id = id
        self.username = username
        self.password_hash = password_hash or ""
        self.role = role or "user"

    def check_password(self, password: str) -> bool:
        """Verifica el password usando bcrypt (truncando a 72 caracteres)."""
        try:
            if not self.password_hash:
                app.logger.warning(
                    "[Usuario.check_password] password_hash vacío para user=%s",
                    self.username,
                )
                return False
            password = (password or "")[:72]
            return bcrypt.verify(password, self.password_hash)
        except Exception as e:
            app.logger.error(
                "[Usuario.check_password] Error verificando hash para user=%s: %s",
                self.username,
                e,
            )
            return False


@login_manager.user_loader
def load_user(user_id):
    """Carga un usuario por id desde la base de datos."""
    try:
        with get_conn(dict_rows=True) as conn:
            user = conn.execute(
                "SELECT * FROM usuarios WHERE id = %s",
                (user_id,),
            ).fetchone()
        if user:
            return Usuario(
                user["id"],
                user["username"],
                user["password_hash"],
                user["role"],
            )
    except Exception as e:
        app.logger.error("[load_user] Error cargando usuario id=%s: %s", user_id, e)
    return None


# -------------------------------------------------
# INICIALIZACIÓN DE BASE DE DATOS
# -------------------------------------------------
def inicializar_bd():
    """Crea tablas si no existen y garantiza un usuario admin: admin / admin123."""
    # Crear tablas
    with get_conn() as conn:
        with conn.cursor() as cur:
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

    # Crear o reparar usuario admin
    with get_conn(dict_rows=True) as conn2:
        admin = conn2.execute(
            "SELECT id, password_hash FROM usuarios WHERE username = %s",
            ("admin",),
        ).fetchone()

        if not admin:
            # Crear admin con contraseña admin123
            pwd = bcrypt.hash("admin123"[:72])
            conn2.execute(
                "INSERT INTO usuarios (username, password_hash, role) VALUES (%s, %s, %s)",
                ("admin", pwd, "admin"),
            )
            conn2.commit()
            app.logger.info("[inicializar_bd] Usuario admin creado con password admin123")
        elif not admin["password_hash"]:
            # Reparar admin sin hash
            pwd = bcrypt.hash("admin123"[:72])
            conn2.execute(
                "UPDATE usuarios SET password_hash = %s WHERE id = %s",
                (pwd, admin["id"]),
            )
            conn2.commit()
            app.logger.info(
                "[inicializar_bd] Hash de usuario admin reparado con password admin123"
            )


# -------------------------------------------------
# RUTAS DE AUTENTICACIÓN
# -------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password:
            flash("Debe ingresar usuario y contraseña.", "error")
            return render_template("login.html")

        try:
            with get_conn(dict_rows=True) as conn:
                user = conn.execute(
                    "SELECT * FROM usuarios WHERE username = %s",
                    (username,),
                ).fetchone()
        except Exception as e:
            app.logger.error("[LOGIN] Error buscando usuario '%s': %s", username, e)
            flash("Error interno al buscar usuario.", "error")
            return render_template("login.html")

        if not user:
            flash("Usuario o contraseña incorrectos.", "error")
            return render_template("login.html")

        user_obj = Usuario(
            user["id"],
            user["username"],
            user["password_hash"],
            user["role"],
        )

        if user_obj.check_password(password):
            login_user(user_obj)
            flash("Inicio de sesión exitoso.", "success")
            return redirect(url_for("index"))
        else:
            flash("Usuario o contraseña incorrectos.", "error")
            return render_template("login.html")

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Sesión cerrada.", "info")
    return redirect(url_for("login"))


# -------------------------------------------------
# RUTA PRINCIPAL: FORMULARIO Y ÚLTIMOS MOVIMIENTOS
# -------------------------------------------------
@app.route("/", methods=["GET", "POST"])
@login_required
def index():
    if request.method == "POST":
        usuario = request.form.get("usuario", "").strip()
        fecha = request.form.get("fecha")
        producto = request.form.get("producto", "").strip()
        tipo = request.form.get("tipo")
        monto_str = request.form.get("monto", "").strip()

        if not usuario or not fecha or not tipo or not monto_str:
            flash("Todos los campos son obligatorios.", "error")
            return redirect(url_for("index"))

        try:
            monto = float(monto_str)
        except ValueError:
            flash("El monto debe ser un número válido.", "error")
            return redirect(url_for("index"))

        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    # Asegurar que el cliente exista
                    cur.execute(
                        """
                        INSERT INTO clientes (nombre)
                        VALUES (%s)
                        ON CONFLICT (nombre) DO NOTHING;
                        """,
                        (usuario,),
                    )
                    # Insertar movimiento
                    cur.execute(
                        """
                        INSERT INTO movimientos (usuario, fecha, producto, tipo, monto)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (usuario, fecha, producto, tipo, monto),
                    )
                    conn.commit()
            flash("Movimiento registrado correctamente.", "success")
        except Exception as e:
            app.logger.error("[INDEX POST] Error insertando movimiento: %s", e)
            flash("Error al registrar el movimiento.", "error")

        return redirect(url_for("index"))

    # GET: mostrar últimos movimientos
    try:
        with get_conn(dict_rows=True) as conn:
            ultimos = conn.execute(
                """
                SELECT usuario, fecha, producto, tipo, monto
                FROM movimientos
                ORDER BY id DESC
                LIMIT 10
                """
            ).fetchall()
    except Exception as e:
        app.logger.error("[INDEX GET] Error consultando últimos movimientos: %s", e)
        ultimos = []

    hoy = datetime.today().strftime("%Y-%m-%d")
    return render_template("form.html", ultimos=ultimos, now=hoy)


# -------------------------------------------------
# DETALLE POR USUARIO
# -------------------------------------------------
@app.route("/usuario/<name>")
@login_required
def detalle_usuario(name):
    try:
        with get_conn(dict_rows=True) as conn:
            registros = conn.execute(
                """
                SELECT fecha, producto, tipo, monto
                FROM movimientos
                WHERE usuario = %s
                ORDER BY fecha ASC, id ASC
                """,
                (name,),
            ).fetchall()
    except Exception as e:
        app.logger.error("[detalle_usuario] Error consultando usuario '%s': %s", name, e)
        registros = []

    saldo = 0.0
    tabla = []

    for r in registros:
        monto = float(r["monto"])
        if r["tipo"] == "Compra":
            saldo += monto
        else:
            saldo -= monto

        fecha_val = r["fecha"]
        if hasattr(fecha_val, "strftime"):
            fecha_str = fecha_val.strftime("%d/%m/%Y")
        else:
            fecha_str = str(fecha_val)

        tabla.append(
            {
                "fecha": fecha_str,
                "producto": r.get("producto", ""),
                "tipo": r.get("tipo", ""),
                "monto": monto,
                "saldo": saldo,
            }
        )

    return render_template("usuario.html", user=name, tabla=tabla, saldo=saldo)


# -------------------------------------------------
# PDF POR USUARIO
# -------------------------------------------------
@app.route("/usuario/<name>/pdf")
@login_required
def usuario_pdf(name):
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    c.setTitle(f"Estado de cuenta - {name}")

    # Logo
    logo_path = os.path.join(app.root_path, "static", "ibafuco_logo.jpg")
    if os.path.exists(logo_path):
        c.drawImage(logo_path, 50, 750, width=80, height=80)

    c.setFont("Helvetica-Bold", 16)
    c.drawString(150, 800, f"Estado de cuenta - {name}")
    c.setFont("Helvetica", 12)
    c.drawString(150, 785, "Cocina - Iglesia Bautista Fundamental de Costa Rica")

    # Datos
    try:
        with get_conn(dict_rows=True) as conn:
            movimientos = conn.execute(
                """
                SELECT fecha, producto, tipo, monto
                FROM movimientos
                WHERE usuario = %s
                ORDER BY fecha ASC, id ASC
                """,
                (name,),
            ).fetchall()
    except Exception as e:
        app.logger.error("[usuario_pdf] Error consultando usuario '%s': %s", name, e)
        movimientos = []

    y = 740
    saldo = 0.0

    # Encabezados
    c.setFont("Helvetica-Bold", 11)
    c.drawString(60, y, "Fecha")
    c.drawString(130, y, "Producto")
    c.drawString(320, y, "Tipo")
    c.drawRightString(430, y, "Monto")
    c.drawRightString(520, y, "Saldo")
    c.setFont("Helvetica", 10)
    y -= 20

    for m in movimientos:
        if y < 80:
            c.showPage()
            y = 800
            c.setFont("Helvetica-Bold", 11)
            c.drawString(60, y, "Fecha")
            c.drawString(130, y, "Producto")
            c.drawString(320, y, "Tipo")
            c.drawRightString(430, y, "Monto")
            c.drawRightString(520, y, "Saldo")
            c.setFont("Helvetica", 10)
            y -= 20

        monto = float(m["monto"])
        if m["tipo"] == "Compra":
            saldo += monto
        else:
            saldo -= monto

        fecha_val = m["fecha"]
        if hasattr(fecha_val, "strftime"):
            fecha_str = fecha_val.strftime("%d/%m/%Y")
        else:
            fecha_str = str(fecha_val)

        c.drawString(60, y, fecha_str)
        c.drawString(130, y, m.get("producto", ""))
        c.drawString(320, y, m.get("tipo", ""))
        c.drawRightString(430, y, f"₡{monto:,.2f}")
        c.drawRightString(520, y, f"₡{saldo:,.2f}")
        y -= 18

    c.setFont("Helvetica-Bold", 12)
    c.drawString(60, 60, f"Saldo final: ₡{saldo:,.2f}")

    c.save()
    buffer.seek(0)
    filename = f"{name}_estado_cuenta.pdf"
    return send_file(buffer, as_attachment=True, download_name=filename, mimetype="application/pdf")


# -------------------------------------------------
# INICIALIZAR BD AL ARRANCAR
# -------------------------------------------------
with app.app_context():
    inicializar_bd()


# -------------------------------------------------
# MAIN LOCAL (no afecta a Render)
# -------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)


