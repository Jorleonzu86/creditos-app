import os
from datetime import datetime
from io import BytesIO, StringIO

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    send_file,
    Response,
)
from flask_login import (
    LoginManager,
    login_user,
    login_required,
    logout_user,
    current_user,
    UserMixin,
)
import psycopg
from psycopg.rows import dict_row
from passlib.hash import bcrypt
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
import csv

# -------------------------------------------------
#  Configuración básica
# -------------------------------------------------

app = Flask(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("Falta la variable de entorno DATABASE_URL")

app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "super-secret-key")
app.config["ENV"] = os.getenv("FLASK_ENV", "production")

login_manager = LoginManager(app)
login_manager.login_view = "login"


# -------------------------------------------------
#  Conexión a la base de datos
# -------------------------------------------------

def get_conn():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


# -------------------------------------------------
#  Utilidades de contraseña
# -------------------------------------------------

def truncate_password(password: str) -> str:
    """Bcrypt solo acepta hasta 72 bytes. Cortamos de forma segura."""
    if password is None:
        return ""
    return password[:72]


def hash_password(plain_password: str) -> str:
    return bcrypt.hash(truncate_password(plain_password))


def verify_password(plain_password: str, password_hash: str) -> bool:
    try:
        return bcrypt.verify(truncate_password(plain_password), password_hash)
    except Exception:
        # Si el hash estuviera corrupto o con formato raro
        app.logger.warning("[Usuario.check_password] Error: Invalid hash method ''.")
        return False


# -------------------------------------------------
#  Modelo de usuario para Flask-Login
# -------------------------------------------------

class Usuario(UserMixin):
    def __init__(self, id, username, password_hash, role):
        self.id = id
        self.username = username
        self.password_hash = password_hash
        self.role = role

    @property
    def is_admin(self):
        return self.role == "admin"

    # --------- Métodos de acceso a BD ----------

    @staticmethod
    def from_row(row):
        return Usuario(
            id=row["id"],
            username=row["username"],
            password_hash=row["password_hash"],
            role=row.get("role", "user"),
        )

    @staticmethod
    def get_by_id(user_id):
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, username, password_hash, role FROM usuarios WHERE id = %s",
                    (user_id,),
                )
                row = cur.fetchone()
                return Usuario.from_row(row) if row else None

    @staticmethod
    def get_by_username(username):
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, username, password_hash, role FROM usuarios WHERE username = %s",
                    (username,),
                )
                row = cur.fetchone()
                return Usuario.from_row(row) if row else None

    @staticmethod
    def create(username, password, role="user"):
        with get_conn() as conn:
            with conn.cursor() as cur:
                pw_hash = hash_password(password)
                cur.execute(
                    """
                    INSERT INTO usuarios (username, password_hash, role)
                    VALUES (%s, %s, %s)
                    RETURNING id, username, password_hash, role
                    """,
                    (username, pw_hash, role),
                )
                row = cur.fetchone()
                conn.commit()
                return Usuario.from_row(row)

    def check_password(self, password) -> bool:
        ok = verify_password(password, self.password_hash)
        if not ok:
            app.logger.info(
                "[Usuario.check_password] Hash no coincide para user=%s",
                self.username,
            )
        return ok


@login_manager.user_loader
def load_user(user_id):
    return Usuario.get_by_id(user_id)


# -------------------------------------------------
#  Creación de tablas (si no existen)
# -------------------------------------------------

def create_tables():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS usuarios (
                    id SERIAL PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user'
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS movimientos (
                    id SERIAL PRIMARY KEY,
                    usuario TEXT NOT NULL,
                    fecha DATE NOT NULL,
                    producto TEXT NOT NULL,
                    tipo TEXT NOT NULL CHECK (tipo IN ('Compra','Abono')),
                    monto NUMERIC(10,2) NOT NULL
                );
                """
            )

        conn.commit()
        app.logger.info("✅ Tablas verificadas o creadas correctamente.")


with app.app_context():
    create_tables()
    # NO tocamos el usuario admin existente para no romper tu login actual.


# -------------------------------------------------
#  Rutas de autenticación
# -------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("registro_movimientos"))

    error = None

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = Usuario.get_by_username(username)
        if user and user.check_password(password):
            login_user(user)
            flash("Sesión iniciada correctamente.", "success")
            next_page = request.args.get("next") or url_for("registro_movimientos")
            return redirect(next_page)
        else:
            error = "Usuario o contraseña incorrectos."
            flash(error, "danger")

    return render_template("login.html", error=error)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Sesión cerrada.", "info")
    return redirect(url_for("login"))


# -------------------------------------------------
#  Vista principal: registro de movimientos
# -------------------------------------------------

@app.route("/", methods=["GET", "POST"])
@app.route("/form", methods=["GET", "POST"])
@app.route("/movimientos", methods=["GET", "POST"])
@login_required
def registro_movimientos():
    # ----- Guardar movimiento -----
    if request.method == "POST":
        fecha_str = request.form.get("fecha")
        producto = request.form.get("producto", "").strip()
        tipo = request.form.get("tipo", "Compra")
        monto_str = request.form.get("monto", "0").replace(",", ".")

        try:
            fecha = datetime.strptime(fecha_str, "%Y-%m-%d").date()
        except ValueError:
            try:
                fecha = datetime.strptime(fecha_str, "%d/%m/%Y").date()
            except ValueError:
                flash("Fecha inválida.", "danger")
                return redirect(url_for("registro_movimientos"))

        try:
            monto = float(monto_str)
        except ValueError:
            flash("Monto inválido.", "danger")
            return redirect(url_for("registro_movimientos"))

        if monto <= 0:
            flash("El monto debe ser mayor a cero.", "danger")
            return redirect(url_for("registro_movimientos"))

        if tipo not in ("Compra", "Abono"):
            flash("Tipo inválido.", "danger")
            return redirect(url_for("registro_movimientos"))

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO movimientos (usuario, fecha, producto, tipo, monto)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (current_user.username, fecha, producto, tipo, monto),
                )
            conn.commit()

        flash("Movimiento guardado correctamente.", "success")
        return redirect(url_for("registro_movimientos"))

    # ----- Filtros de búsqueda y paginación -----
    page = int(request.args.get("page", 1))
    per_page = 10
    offset = (page - 1) * per_page

    search = request.args.get("search", "").strip()

    movimientos = []
    total = 0
    saldo_actual = 0.0

    with get_conn() as conn:
        with conn.cursor() as cur:
            # Suma de saldo del usuario actual
            cur.execute(
                """
                SELECT
                    SUM(
                        CASE
                            WHEN tipo = 'Abono' THEN monto
                            WHEN tipo = 'Compra' THEN -monto
                            ELSE 0
                        END
                    ) AS saldo
                FROM movimientos
                WHERE usuario = %s
                """,
                (current_user.username,),
            )
            row = cur.fetchone()
            saldo_actual = float(row["saldo"] or 0)

            # Conteo total con filtro
            if search:
                cur.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM movimientos
                    WHERE usuario = %s
                    AND (producto ILIKE %s OR tipo ILIKE %s)
                    """,
                    (current_user.username, f"%{search}%", f"%{search}%"),
                )
            else:
                cur.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM movimientos
                    WHERE usuario = %s
                    """,
                    (current_user.username,),
                )
            total = cur.fetchone()["total"]

            # Lista paginada
            if search:
                cur.execute(
                    """
                    SELECT id, usuario, fecha, producto, tipo, monto
                    FROM movimientos
                    WHERE usuario = %s
                    AND (producto ILIKE %s OR tipo ILIKE %s)
                    ORDER BY fecha DESC, id DESC
                    LIMIT %s OFFSET %s
                    """,
                    (
                        current_user.username,
                        f"%{search}%",
                        f"%{search}%",
                        per_page,
                        offset,
                    ),
                )
            else:
                cur.execute(
                    """
                    SELECT id, usuario, fecha, producto, tipo, monto
                    FROM movimientos
                    WHERE usuario = %s
                    ORDER BY fecha DESC, id DESC
                    LIMIT %s OFFSET %s
                    """,
                    (current_user.username, per_page, offset),
                )
            movimientos = cur.fetchall()

    total_pages = max((total - 1) // per_page + 1, 1)

    return render_template(
        "form.html",
        movimientos=movimientos,
        saldo_actual=saldo_actual,
        page=page,
        total_pages=total_pages,
        search=search,
    )


# -------------------------------------------------
#  Saldos por usuario (vista admin)
# -------------------------------------------------

@app.route("/saldos")
@login_required
def saldos_usuarios():
    if not current_user.is_admin:
        flash("No tienes permisos para ver esta sección.", "danger")
        return redirect(url_for("registro_movimientos"))

    saldos = []
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    usuario,
                    SUM(
                        CASE
                            WHEN tipo = 'Abono' THEN monto
                            WHEN tipo = 'Compra' THEN -monto
                            ELSE 0
                        END
                    ) AS saldo
                FROM movimientos
                GROUP BY usuario
                ORDER BY usuario;
                """
            )
            saldos = cur.fetchall()

    return render_template("usuario.html", saldos=saldos)


# -------------------------------------------------
#  Exportar PDF de movimientos
# -------------------------------------------------

@app.route("/reportes/pdf")
@login_required
def reporte_pdf():
    usuario = request.args.get("usuario")

    # Usuarios normales solo pueden ver su proprio reporte
    if not current_user.is_admin:
        usuario = current_user.username

    movimientos = []
    with get_conn() as conn:
        with conn.cursor() as cur:
            if usuario:
                cur.execute(
                    """
                    SELECT usuario, fecha, producto, tipo, monto
                    FROM movimientos
                    WHERE usuario = %s
                    ORDER BY fecha ASC, id ASC
                    """,
                    (usuario,),
                )
            else:
                cur.execute(
                    """
                    SELECT usuario, fecha, producto, tipo, monto
                    FROM movimientos
                    ORDER BY usuario ASC, fecha ASC, id ASC
                    """
                )
            movimientos = cur.fetchall()

    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter

    y = height - 50
    title = "Reporte de Movimientos"
    p.setFont("Helvetica-Bold", 16)
    p.drawString(50, y, title)
    y -= 30

    p.setFont("Helvetica", 10)
    for mov in movimientos:
        linea = (
            f"Usuario: {mov['usuario']} | "
            f"Fecha: {mov['fecha']} | "
            f"Prod: {mov['producto']} | "
            f"Tipo: {mov['tipo']} | "
            f"Monto: {mov['monto']}"
        )
        p.drawString(50, y, linea)
        y -= 15
        if y < 50:
            p.showPage()
            y = height - 50
            p.setFont("Helvetica", 10)

    p.showPage()
    p.save()
    buffer.seek(0)

    filename = "reporte_movimientos.pdf"
    return send_file(
        buffer,
        as_attachment=True,
        download_name=filename,
        mimetype="application/pdf",
    )


# -------------------------------------------------
#  Exportar a Excel (CSV)
# -------------------------------------------------

@app.route("/reportes/excel")
@login_required
def reporte_excel():
    usuario = request.args.get("usuario")

    if not current_user.is_admin:
        usuario = current_user.username

    movimientos = []
    with get_conn() as conn:
        with conn.cursor() as cur:
            if usuario:
                cur.execute(
                    """
                    SELECT usuario, fecha, producto, tipo, monto
                    FROM movimientos
                    WHERE usuario = %s
                    ORDER BY fecha ASC, id ASC
                    """,
                    (usuario,),
                )
            else:
                cur.execute(
                    """
                    SELECT usuario, fecha, producto, tipo, monto
                    FROM movimientos
                    ORDER BY usuario ASC, fecha ASC, id ASC
                    """
                )
            movimientos = cur.fetchall()

    si = StringIO()
    writer = csv.writer(si, delimiter=";", lineterminator="\n")
    writer.writerow(["Usuario", "Fecha", "Producto", "Tipo", "Monto"])
    for mov in movimientos:
        writer.writerow(
            [
                mov["usuario"],
                mov["fecha"].strftime("%Y-%m-%d"),
                mov["producto"],
                mov["tipo"],
                str(mov["monto"]),
            ]
        )

    output = si.getvalue()
    filename = "movimientos.csv"
    headers = {
        "Content-Disposition": f"attachment; filename={filename}",
        "Content-Type": "text/csv; charset=utf-8",
    }
    return Response(output, headers=headers)


# -------------------------------------------------
#  Gestión de usuarios (solo admin)
# -------------------------------------------------

@app.route("/usuarios")
@login_required
def lista_usuarios():
    if not current_user.is_admin:
        flash("No tienes permisos para ver esta sección.", "danger")
        return redirect(url_for("registro_movimientos"))

    usuarios = []
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, role FROM usuarios ORDER BY username ASC"
            )
            usuarios = cur.fetchall()

    return render_template("usuarios_list.html", usuarios=usuarios)


@app.route("/usuarios/nuevo", methods=["GET", "POST"])
@login_required
def nuevo_usuario():
    if not current_user.is_admin:
        flash("No tienes permisos para crear usuarios.", "danger")
        return redirect(url_for("registro_movimientos"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "user")

        if not username or not password:
            flash("Usuario y contraseña son obligatorios.", "danger")
            return redirect(url_for("nuevo_usuario"))

        # Crear usuario
        try:
            Usuario.create(username=username, password=password, role=role)
            flash("Usuario creado correctamente.", "success")
            return redirect(url_for("lista_usuarios"))
        except psycopg.errors.UniqueViolation:
            flash("El usuario ya existe.", "danger")
        except Exception as e:
            app.logger.error("Error al crear usuario: %s", e)
            flash("Ocurrió un error al crear el usuario.", "danger")

    return render_template("usuario_nuevo.html")


# -------------------------------------------------
#  Dashboard con gráficas (solo admin)
# -------------------------------------------------

@app.route("/dashboard")
@login_required
def dashboard():
    if not current_user.is_admin:
        flash("No tienes permisos para ver el dashboard.", "danger")
        return redirect(url_for("registro_movimientos"))

    data = {}

    with get_conn() as conn:
        with conn.cursor() as cur:
            # Totales
            cur.execute("SELECT COUNT(*) AS total_usuarios FROM usuarios;")
            data["total_usuarios"] = cur.fetchone()["total_usuarios"]

            cur.execute("SELECT COUNT(*) AS total_movs FROM movimientos;")
            data["total_movs"] = cur.fetchone()["total_movs"]

            cur.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN tipo = 'Compra' THEN monto ELSE 0 END), 0) AS total_compras,
                    COALESCE(SUM(CASE WHEN tipo = 'Abono' THEN monto ELSE 0 END), 0) AS total_abonos
                FROM movimientos;
                """
            )
            row = cur.fetchone()
            data["total_compras"] = float(row["total_compras"])
            data["total_abonos"] = float(row["total_abonos"])
            data["saldo_global"] = data["total_abonos"] - data["total_compras"]

            # Saldos por usuario (para gráfica)
            cur.execute(
                """
                SELECT
                    usuario,
                    SUM(
                        CASE
                            WHEN tipo = 'Abono' THEN monto
                            WHEN tipo = 'Compra' THEN -monto
                            ELSE 0
                        END
                    ) AS saldo
                FROM movimientos
                GROUP BY usuario
                ORDER BY usuario;
                """
            )
            saldos = cur.fetchall()
            data["saldos_labels"] = [s["usuario"] for s in saldos]
            data["saldos_values"] = [float(s["saldo"] or 0) for s in saldos]

    return render_template("dashboard.html", data=data)


# -------------------------------------------------
#  Run local (para pruebas)
# -------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)


